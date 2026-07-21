#!/usr/bin/env python3
"""One manifest-driven r006 local-gate/upload/readback/promotion transaction."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import uuid

import promote_step5d_autotune_v3_delivery as promote
import upload_ur_tp_package as upload
from ur10e_mutation_lock import acquire_controller_mutation_locks, release_controller_mutation_locks


ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve(strict=True)
    local_dir = root / promote.PACKAGE_DIR
    upload_args = [
        promote.PROGRAM,
        "--local-dir", str(local_dir),
        "--readback-root", str(root / "runs"),
        "--target-dir", promote.TARGET_DIR,
        "--override-table",
        "--override-reason", "manifest-driven r006 delivery and automatic promotion",
    ]
    if args.dry_run:
        return upload._main([*upload_args, "--dry-run"])
    handles = acquire_controller_mutation_locks()
    try:
        transaction_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="step5d-v3-r006-upload-result-") as temporary:
            result_path = Path(temporary) / "result.json"
            rc = upload._main(
                [
                    *upload_args,
                    "--force-upload-readback",
                    "--upload-transaction-id", transaction_id,
                    "--manifest-path-output", str(result_path),
                ]
            )
            if rc != 0:
                return rc
            result = json.loads(result_path.read_text(encoding="utf-8"))
            manifest = Path(str(result.get("manifest_path") or "")).resolve(strict=True)
            if (
                result.get("schema_version") != "ur10e_upload_result_v1"
                or result.get("upload_transaction_id") != transaction_id
                or hashlib.sha256(manifest.read_bytes()).hexdigest()
                != result.get("manifest_sha256")
                or not manifest.is_relative_to((root / "runs").resolve())
            ):
                raise RuntimeError("r006 upload-result manifest handoff differs")
            promote.promote(root, manifest)
            return 0
    finally:
        release_controller_mutation_locks(handles)


if __name__ == "__main__":
    raise SystemExit(main())
