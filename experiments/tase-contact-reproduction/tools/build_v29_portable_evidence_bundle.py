#!/usr/bin/env python3
"""Copy the immutable v29 control evidence into a portable offline bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


FILES = (
    "bridge_rtde_500hz.csv",
    "bridge_run_manifest.json",
    "stage_frequency_summary.json",
    "step5d_bridge_analysis.json",
    "summary.json",
)


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_bundle(source_dir: Path, output_dir: Path) -> dict[str, object]:
    source = source_dir.resolve()
    output = output_dir.resolve()
    if output.exists():
        raise FileExistsError(f"refusing to overwrite evidence bundle: {output}")
    missing = [name for name in FILES if not (source / name).is_file()]
    if missing:
        raise FileNotFoundError(f"v29 evidence files missing: {', '.join(missing)}")
    output.mkdir(parents=True)
    rows: list[dict[str, object]] = []
    for name in FILES:
        source_path = source / name
        destination = output / name
        shutil.copyfile(source_path, destination)
        source_hash = sha256_path(source_path)
        destination_hash = sha256_path(destination)
        if source_hash != destination_hash or source_path.stat().st_size != destination.stat().st_size:
            raise RuntimeError(f"portable evidence copy mismatch: {name}")
        rows.append(
            {
                "role": name.removesuffix(".json").removesuffix(".csv"),
                "source_path": str(source_path),
                "portable_path": name,
                "size_bytes": destination.stat().st_size,
                "sha256": destination_hash,
            }
        )
    manifest: dict[str, object] = {
        "schema": "step5d_v29_portable_evidence_bundle_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_run": str(source),
        "source_read_only": True,
        "files": rows,
        "claim_boundary": {
            "v29_frozen_fallback": True,
            "package_accepted": False,
            "live_accepted": False,
            "reproduction_complete": False,
            "portable_copy_does_not_refresh_readback_or_timing": True,
        },
        "safety_boundary": [
            "offline file copy only",
            "no controller access",
            "no bridge start",
            "no robot motion",
        ],
    }
    manifest_path = output / "portable_evidence_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def verify_bundle(bundle_dir: Path) -> list[str]:
    root = bundle_dir.resolve()
    manifest_path = root / "portable_evidence_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"manifest_unreadable:{type(exc).__name__}"]
    blockers: list[str] = []
    if manifest.get("schema") != "step5d_v29_portable_evidence_bundle_v1":
        blockers.append("manifest_schema_invalid")
    rows = manifest.get("files")
    if not isinstance(rows, list) or {row.get("portable_path") for row in rows if isinstance(row, dict)} != set(FILES):
        blockers.append("portable_file_set_invalid")
        rows = []
    for row in rows:
        if not isinstance(row, dict):
            blockers.append("portable_file_row_invalid")
            continue
        path = root / str(row.get("portable_path") or "")
        if not path.is_file():
            blockers.append(f"missing:{path.name}")
        elif path.stat().st_size != row.get("size_bytes"):
            blockers.append(f"size_mismatch:{path.name}")
        elif sha256_path(path) != row.get("sha256"):
            blockers.append(f"sha256_mismatch:{path.name}")
    boundary = manifest.get("claim_boundary") or {}
    if boundary.get("v29_frozen_fallback") is not True or any(
        boundary.get(field) is not False
        for field in ("package_accepted", "live_accepted", "reproduction_complete")
    ):
        blockers.append("claim_boundary_invalid")
    return sorted(set(blockers))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        blockers = verify_bundle(args.output_dir)
        print(json.dumps({"valid": not blockers, "blockers": blockers}, indent=2))
        return 0 if not blockers else 3
    if args.source_dir is None:
        parser.error("--source-dir is required unless --verify is used")
    print(json.dumps(build_bundle(args.source_dir, args.output_dir), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
