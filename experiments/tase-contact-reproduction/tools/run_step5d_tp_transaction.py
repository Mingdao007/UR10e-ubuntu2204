#!/usr/bin/env python3
"""Single-process Step5d upload, promotion, and fresh-readback coordinator."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import uuid
import promote_step5d_current as promote
import upload_ur_tp_package as upload
import verify_current_stage_readback as readback
import step5d_fast_gate as fast_gate
from ur10e_mutation_lock import acquire_controller_mutation_locks, release_controller_mutation_locks

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("program")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--local-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--readback-root", type=Path)
    parser.add_argument("--artifact-store", type=Path)
    parser.add_argument("--review-status", choices=("accepted_1+1", "skipped_unavailable_1+0"))
    args = parser.parse_args(argv)
    upload_args = [args.program, "--local-dir", str(args.local_dir)]
    readback_root = (args.readback_root or (args.root / "runs")).resolve()
    root = args.root.resolve()
    try:
        readback_root.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(
            "read-back root must remain inside the experiment root so legacy and canonical "
            "manifests share one durable namespace"
        ) from exc
    upload_args += ["--readback-root", str(readback_root)]
    if args.dry_run:
        upload_args.append("--dry-run")
        return upload._main(upload_args)
    candidate_preflight = fast_gate.preflight_controller_candidate(
        root=root,
        program=args.program,
        local_dir=args.local_dir,
        review_status=args.review_status,
    )
    handles = acquire_controller_mutation_locks()
    try:
        transaction_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="step5d-upload-result-") as td:
            result_path = Path(td) / "upload-result.json"
            upload_args += [
                "--upload-transaction-id", transaction_id,
                "--manifest-path-output", str(result_path),
            ]
            rc = upload._main(upload_args)
            if rc:
                return rc
            if not result_path.is_file():
                raise RuntimeError("upload transaction did not return an exact manifest path")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            manifest = Path(str(result.get("manifest_path", ""))).resolve()
            try:
                manifest.relative_to(readback_root)
            except ValueError as exc:
                raise RuntimeError("upload transaction manifest escaped the read-back root") from exc
            if (
                result.get("schema_version") != "ur10e_upload_result_v1"
                or result.get("upload_transaction_id") != transaction_id
                or not manifest.is_file()
                or manifest.name != "manifest.json"
                or manifest.parent.name.startswith(f"controller_readback_{args.program}_") is False
                or hashlib.sha256(manifest.read_bytes()).hexdigest() != result.get("manifest_sha256")
            ):
                raise RuntimeError("upload transaction manifest identity is invalid")
            manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
            if manifest_payload.get("upload_transaction_id") != transaction_id:
                raise RuntimeError("upload transaction token is not bound into the manifest")
            fast_gate.preflight_fresh_readback(
                program=args.program,
                candidate_sha=candidate_preflight.package_sha256,
                readback_manifest=manifest,
            )
            rc = promote._main([
                "--root", str(args.root), "--program", args.program,
                "--local-dir", str(args.local_dir), "--manifest", str(manifest), "--json",
            ])
            if rc:
                return rc
            rc = readback.main([
                "--root", str(args.root), "--program", args.program,
                "--manifest", str(manifest), "--json",
            ])
            if rc:
                return rc
            kwargs = {
                "root": root,
                "program": args.program,
                "readback_manifest": manifest,
                "store": fast_gate.artifact_store(root, args.artifact_store),
                "review_status": args.review_status,
                "candidate_preflight": candidate_preflight,
            }
            fast_gate.promote_controller_verified(**kwargs)
            return 0
    finally:
        release_controller_mutation_locks(handles)

if __name__ == "__main__":
    raise SystemExit(main())
