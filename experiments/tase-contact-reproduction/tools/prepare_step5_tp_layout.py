#!/usr/bin/env python3
"""Prepare relocated Step5 TP triplets and SHA-bound controller manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath

from relocate_ur_tp_triplet import SUFFIXES, relocate


RELEASE_PATTERN = re.compile(r"^step5d_strict_rnn_autotune_v3_r(\d{3})$")
ALLOWED_DISPOSITIONS = {"archive", "delete", "visible"}
ALLOWED_PACKAGE_FIELDS = {
    "basename",
    "controller_present",
    "source_controller_directory",
    "destination_controller_directory",
    "disposition",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_plan(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected_fields = {
        "schema_version",
        "controller_root",
        "protected_autotune_release_min",
        "protected_root_basename",
        "packages",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise ValueError("layout plan contains missing or unsupported fields")
    if payload["schema_version"] != 1:
        raise ValueError("layout plan schema_version must be 1")

    root = PurePosixPath(str(payload["controller_root"]))
    protected_min = payload["protected_autotune_release_min"]
    protected_root = payload["protected_root_basename"]
    packages = payload["packages"]
    if not isinstance(protected_min, int) or protected_min < 1:
        raise ValueError("protected_autotune_release_min must be a positive integer")
    if not isinstance(protected_root, str) or not RELEASE_PATTERN.fullmatch(protected_root):
        raise ValueError("protected_root_basename must be a release basename")
    if not isinstance(packages, list) or not packages:
        raise ValueError("layout plan packages must be a non-empty list")

    seen: set[str] = set()
    for item in packages:
        if not isinstance(item, dict) or set(item) != ALLOWED_PACKAGE_FIELDS:
            raise ValueError("package contains missing or unsupported fields")
        basename = item["basename"]
        if not isinstance(basename, str) or not basename or "/" in basename or "." in basename:
            raise ValueError("package basename must be an extension-free path component")
        if basename in seen:
            raise ValueError(f"duplicate package basename: {basename}")
        match = RELEASE_PATTERN.fullmatch(basename)
        if match and int(match.group(1)) >= protected_min:
            raise ValueError(f"protected release cannot enter relocation plan: {basename}")
        if basename == protected_root:
            raise ValueError(f"protected root package cannot enter relocation plan: {basename}")
        if not isinstance(item["controller_present"], bool):
            raise ValueError(f"controller_present must be boolean: {basename}")
        if item["disposition"] not in ALLOWED_DISPOSITIONS:
            raise ValueError(f"unsupported disposition: {basename}")

        source = PurePosixPath(str(item["source_controller_directory"]))
        if root != source and root not in source.parents:
            raise ValueError(f"source escapes controller root: {basename}")
        if item["disposition"] == "delete":
            if not item["controller_present"]:
                raise ValueError(f"delete package must exist on controller: {basename}")
            if item["destination_controller_directory"] is not None:
                raise ValueError(f"delete package cannot name a destination: {basename}")
            seen.add(basename)
            continue
        if not isinstance(item["destination_controller_directory"], str):
            raise ValueError(f"relocated package destination must be a string: {basename}")
        destination = PurePosixPath(item["destination_controller_directory"])
        if root not in destination.parents:
            raise ValueError(f"destination must be below controller root: {basename}")
        expected_tail = "archive" if item["disposition"] == "archive" else None
        if expected_tail and destination.name != expected_tail:
            raise ValueError(f"archive package destination must end in archive: {basename}")
        if not expected_tail and destination.name == "archive":
            raise ValueError(f"visible package cannot target archive: {basename}")
        if source == destination:
            raise ValueError(f"source and destination must differ: {basename}")
        seen.add(basename)
    return payload


def local_relative_dir(controller_root: PurePosixPath, destination: PurePosixPath) -> Path:
    return Path(*destination.relative_to(controller_root).parts)


def deployment_manifest(
    basename: str, controller_dir: str, package_dir: Path
) -> dict[str, object]:
    artifacts = []
    for suffix in (".urp", ".txt", ".script"):
        path = package_dir / f"{basename}{suffix}"
        artifacts.append(
            {
                "filename": path.name,
                "source": str(path.resolve()),
                "sha256": sha256(path),
            }
        )
    return {
        "schema_version": 1,
        "basename": basename,
        "controller_directory": controller_dir,
        "artifacts": artifacts,
    }


def prepare(
    plan_path: Path, source_root: Path, output_root: Path, manifests_dir: Path
) -> dict[str, object]:
    plan = load_plan(plan_path)
    controller_root = PurePosixPath(str(plan["controller_root"]))
    output_root.mkdir(parents=True, exist_ok=True)
    manifests_dir.mkdir(parents=True, exist_ok=True)

    packages: list[dict[str, object]] = []
    removals: dict[str, list[dict[str, object]]] = defaultdict(list)
    for item in plan["packages"]:
        basename = str(item["basename"])
        source_dir = source_root / basename
        source_hashes = {
            suffix: sha256(source_dir / f"{basename}{suffix}") for suffix in SUFFIXES
        }
        if item["controller_present"]:
            removals[str(item["source_controller_directory"])].append(
                {"basename": basename, "sha256": source_hashes}
            )
        if item["disposition"] == "delete":
            packages.append(
                {
                    **item,
                    "local_relative_directory": None,
                    "source_sha256": source_hashes,
                    "destination_sha256": None,
                    "deploy_manifest": None,
                }
            )
            continue
        destination = PurePosixPath(str(item["destination_controller_directory"]))
        package_dir = output_root / local_relative_dir(controller_root, destination)
        result = relocate(source_dir, package_dir, basename, str(destination))
        manifest = deployment_manifest(basename, str(destination), package_dir)
        manifest_path = manifests_dir / f"deploy-{basename}.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        packages.append(
            {
                **item,
                "local_relative_directory": str(
                    local_relative_dir(controller_root, destination)
                ),
                "source_sha256": source_hashes,
                "destination_sha256": result["sha256"],
                "deploy_manifest": str(manifest_path.resolve()),
            }
        )

    removal_manifests = []
    for index, (controller_dir, triplets) in enumerate(sorted(removals.items()), start=1):
        payload = {
            "schema_version": 1,
            "controller_directory": controller_dir,
            "triplets": sorted(triplets, key=lambda row: row["basename"]),
        }
        path = manifests_dir / f"remove-source-{index}.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        removal_manifests.append(str(path.resolve()))

    summary = {
        "schema_version": 1,
        "controller_root": str(controller_root),
        "protected_autotune_release_min": plan["protected_autotune_release_min"],
        "protected_root_basename": plan["protected_root_basename"],
        "package_count": len(packages),
        "deploy_package_count": sum(
            1 for item in packages if item["disposition"] != "delete"
        ),
        "delete_package_count": sum(
            1 for item in packages if item["disposition"] == "delete"
        ),
        "controller_source_count": sum(
            1 for item in packages if item["controller_present"]
        ),
        "packages": packages,
        "removal_manifests": removal_manifests,
    }
    summary_path = manifests_dir / "migration-manifest.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--manifests-dir", type=Path, required=True)
    args = parser.parse_args()
    result = prepare(args.plan, args.source_root, args.output_root, args.manifests_dir)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
