#!/usr/bin/env python3
"""Capture and materialize the bounded R013 source surface for R014.

The dirty R013 worktree is an immutable source for this operation.  The
manifest is created before any file is copied and can later be re-verified
against either the source or the materialized R014 tree.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Iterable


SCHEMA = "step5d.r014/source-capture-v1"
R013_CONFIG_RELATIVE = Path(
    "experiments/tase-contact-reproduction/tools/step5d_autotune_v4_r013/"
    "campaign_config.py"
)
EXPERIMENT_RELATIVE = Path("experiments/tase-contact-reproduction")


class SourceCaptureError(RuntimeError):
    """The declared R013 source surface is incomplete or has drifted."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_host_paths(config_path: Path) -> tuple[str, ...]:
    tree = ast.parse(config_path.read_text(encoding="utf-8"), filename=str(config_path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if "R013_HOST_SOURCE_PATHS" in names:
                value = ast.literal_eval(node.value)
                if not isinstance(value, tuple) or not all(isinstance(item, str) for item in value):
                    break
                return value
    raise SourceCaptureError("R013_HOST_SOURCE_PATHS is missing or is not a literal tuple")


def _git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


def _bounded_paths(repo_root: Path) -> list[Path]:
    experiment_root = repo_root / EXPERIMENT_RELATIVE
    host_paths = _extract_host_paths(repo_root / R013_CONFIG_RELATIVE)
    relatives = {EXPERIMENT_RELATIVE / relative for relative in host_paths}

    package_roots = (
        experiment_root / "tools/step5d_autotune_v3",
        experiment_root / "tools/step5d_autotune_v4_r004",
        experiment_root / "tools/step5d_autotune_v4_r005",
        experiment_root / "tools/step5d_autotune_v4_r006",
        experiment_root / "tools/step5d_autotune_v4_r008",
        experiment_root / "tools/step5d_autotune_v4_r012",
        experiment_root / "tools/step5d_autotune_v4_r013",
        experiment_root / "tools/step6_figure8_autotune_v1",
    )
    for package_root in package_roots:
        if not package_root.is_dir():
            raise SourceCaptureError(f"required package is missing: {package_root}")
        relatives.update(path.relative_to(repo_root) for path in package_root.rglob("*.py"))

    entrypoints = (
        "tools/run_step5d_autotune_v4_r013_live.py",
        "tools/run_step6_figure8_autotune_v1_live.py",
        "tools/run_step6_figure8_no_contact_canary_v1_live.py",
        "tools/run_step5d_autotune_v4_r013_demo.py",
        "tests/conftest.py",
    )
    relatives.update(EXPERIMENT_RELATIVE / relative for relative in entrypoints)

    test_patterns = (
        "test_step5d_autotune_v4_r013*.py",
        "test_step5d_autotune_v4_two_stage_campaign.py",
        "test_step5d_autotune_v4_recovery_supervisor.py",
        "test_step6_autotuner_v5*.py",
        "test_step6_figure8*.py",
        "test_step6_v5_entry_comparison.py",
    )
    tests_root = experiment_root / "tests"
    for pattern in test_patterns:
        relatives.update(path.relative_to(repo_root) for path in tests_root.glob(pattern))

    config_names = (
        "current.json",
        "r013_budgeted_floor_v1.json",
        "r013_0p31_bounded_bo_seed_v1.json",
        "r013_figure8_transfer_template_v1.json",
        "autotune_v4_r012_censoring.json",
        "autotune_v4_r012_observation_noise.json",
        "autotune_v4_r012_safety_filter.json",
        "autotune_v4_r012_wave_schedule.json",
        "v4_two_stage_campaign_v1.json",
    )
    relatives.update(
        EXPERIMENT_RELATIVE / "config/step5d" / name for name in config_names
    )
    release_inputs = (
        "config/step5/step5d_autotune_v4_r012_launch_profile.json",
        "config/step5/step5d_autotune_v4_r013_launch_profile.json",
        "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r012.script",
        "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r012.txt",
        "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r012.urp",
    )
    relatives.update(EXPERIMENT_RELATIVE / relative for relative in release_inputs)

    missing = [relative for relative in sorted(relatives) if not (repo_root / relative).is_file()]
    if missing:
        formatted = "\n".join(str(path) for path in missing)
        raise SourceCaptureError(f"declared source files are missing:\n{formatted}")
    return sorted(relatives)


def build_manifest(repo_root: Path) -> dict[str, object]:
    repo_root = repo_root.resolve()
    files: list[dict[str, object]] = []
    for relative in _bounded_paths(repo_root):
        path = repo_root / relative
        status = _git(repo_root, "status", "--porcelain=v1", "--", str(relative))
        files.append(
            {
                "path": relative.as_posix(),
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
                "git_status": status or "clean",
            }
        )
    basis = {
        "schema": SCHEMA,
        "version": 1,
        "source_repo": str(repo_root),
        "source_head": _git(repo_root, "rev-parse", "HEAD"),
        "files": files,
    }
    payload = json.dumps(basis, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {**basis, "manifest_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest()}


def verify_manifest(manifest: dict[str, object], root: Path) -> None:
    if manifest.get("schema") != SCHEMA or manifest.get("version") != 1:
        raise SourceCaptureError("source-capture schema/version mismatch")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise SourceCaptureError("source-capture file list is empty")
    for record in files:
        if not isinstance(record, dict):
            raise SourceCaptureError("source-capture file record is malformed")
        relative = Path(str(record.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise SourceCaptureError(f"unsafe source-capture path: {relative}")
        candidate = root / relative
        if not candidate.is_file():
            raise SourceCaptureError(f"captured file is missing: {relative}")
        actual = _sha256(candidate)
        if actual != record.get("sha256"):
            raise SourceCaptureError(
                f"captured file drifted: {relative}: expected={record.get('sha256')} actual={actual}"
            )


def materialize(
    manifest: dict[str, object],
    source_root: Path,
    target_root: Path,
    *,
    missing_only: bool = False,
) -> None:
    verify_manifest(manifest, source_root)
    files = manifest["files"]
    assert isinstance(files, list)
    for record in files:
        assert isinstance(record, dict)
        relative = Path(str(record["path"]))
        destination = target_root / relative
        if missing_only and destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / relative, destination)
    if not missing_only:
        verify_manifest(manifest, target_root)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--materialize-to", type=Path)
    parser.add_argument("--from-manifest", action="store_true")
    parser.add_argument("--missing-only", action="store_true")
    parser.add_argument("--verify-root", type=Path)
    args = parser.parse_args(argv)

    if args.verify_root is not None:
        manifest = json.loads(args.output.read_text(encoding="utf-8"))
        verify_manifest(manifest, args.verify_root.resolve())
        print(json.dumps({"ok": True, "root": str(args.verify_root.resolve())}))
        return 0

    if args.from_manifest:
        if args.materialize_to is None:
            raise SourceCaptureError("--from-manifest requires --materialize-to")
        manifest = json.loads(args.output.read_text(encoding="utf-8"))
        materialize(
            manifest,
            args.source_root.resolve(),
            args.materialize_to.resolve(),
            missing_only=args.missing_only,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "files": len(manifest["files"]),
                    "manifest_sha256": manifest["manifest_sha256"],
                    "materialized_to": str(args.materialize_to.resolve()),
                },
                sort_keys=True,
            )
        )
        return 0

    manifest = build_manifest(args.source_root)
    _write_json(args.output, manifest)
    if args.materialize_to is not None:
        materialize(manifest, args.source_root.resolve(), args.materialize_to.resolve())
    print(
        json.dumps(
            {
                "ok": True,
                "files": len(manifest["files"]),
                "manifest_sha256": manifest["manifest_sha256"],
                "output": str(args.output.resolve()),
                "materialized_to": (
                    str(args.materialize_to.resolve()) if args.materialize_to else None
                ),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
