#!/usr/bin/env python3
"""Run one manifest-bound TP upload/fresh-GET/atomic-promotion transaction."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import uuid

import promote_step5d_r009_atomic_release as promote
import upload_ur_tp_package as upload
from step5d_autotune_v3.runtime_identity import (
    RuntimeIdentityError,
    identity_from_manifest,
)
from ur10e_mutation_lock import (
    acquire_controller_mutation_locks,
    release_controller_mutation_locks,
)


ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_program(local_dir: Path) -> str:
    candidates: list[tuple[Path, dict[str, object]]] = []
    for path in sorted(local_dir.glob("*.deploy-manifest.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"TP deploy manifest is invalid: {path.name}: {exc}"
            ) from exc
        if isinstance(payload, dict) and payload.get("schema_version") == 2:
            candidates.append((path, payload))
    if len(candidates) != 1:
        raise RuntimeError(
            "artifact directory must contain exactly one schema-v2 TP deploy manifest"
        )
    _manifest_path, manifest = candidates[0]
    program_id = manifest.get("basename")
    runtime_payload = manifest.get("tp_runtime_identity")
    if not isinstance(program_id, str) or not isinstance(runtime_payload, dict):
        raise RuntimeError("TP deploy manifest identity is incomplete")
    try:
        runtime_identity, script_sha256 = identity_from_manifest(runtime_payload)
    except RuntimeIdentityError as exc:
        raise RuntimeError(f"TP deploy runtime identity is invalid: {exc}") from exc
    if runtime_identity.program_id != program_id:
        raise RuntimeError("TP deploy basename and runtime identity program differ")
    if program_id != promote.PROGRAM:
        raise RuntimeError(
            "TP deploy identity does not match the atomic promotion owner"
        )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 3:
        raise RuntimeError("TP deploy artifact set differs")
    observed_extensions: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise RuntimeError("TP deploy artifact row is invalid")
        filename = artifact.get("filename")
        source = artifact.get("source")
        expected_sha = artifact.get("sha256")
        if (
            not isinstance(filename, str)
            or filename != source
            or not filename.startswith(program_id)
            or not isinstance(expected_sha, str)
        ):
            raise RuntimeError("TP deploy artifact identity differs")
        extension = filename.removeprefix(program_id)
        if extension not in promote.EXTENSIONS or extension in observed_extensions:
            raise RuntimeError("TP deploy artifact extension set differs")
        artifact_path = local_dir / filename
        if artifact_path.is_symlink() or not artifact_path.is_file():
            raise RuntimeError(f"TP deploy artifact is missing or unsafe: {filename}")
        if _sha256(artifact_path) != expected_sha:
            raise RuntimeError(f"TP deploy artifact SHA differs: {filename}")
        observed_extensions.add(extension)
        if extension == ".script" and expected_sha != script_sha256:
            raise RuntimeError("TP deploy runtime identity script SHA differs")
    if observed_extensions != set(promote.EXTENSIONS):
        raise RuntimeError("TP deploy artifact extension set differs")
    return program_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve(strict=True)
    local_dir = args.artifact_dir.expanduser().resolve(strict=True)
    try:
        local_dir.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("pending artifact directory escapes experiment root") from exc
    if local_dir.is_symlink() or not local_dir.is_dir():
        raise RuntimeError("pending artifact directory is unsafe")
    program_id = _artifact_program(local_dir)
    upload_args = [
        program_id,
        "--local-dir", str(local_dir),
        "--readback-root", str(root / "runs"),
        "--target-dir", promote.TARGET_DIR,
        "--override-table",
        "--override-reason",
        f"manifest-driven {program_id} upload, fresh GET, and atomic promotion",
    ]
    if args.dry_run:
        return upload._main([*upload_args, "--dry-run"])
    handles = acquire_controller_mutation_locks()
    try:
        transaction_id = uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="step5d-v3-upload-result-") as temporary:
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
                or _sha256(manifest) != result.get("manifest_sha256")
                or not manifest.is_relative_to((root / "runs").resolve())
            ):
                raise RuntimeError("upload-result manifest handoff differs")
            promote.promote(root, manifest, local_dir)
            return 0
    finally:
        release_controller_mutation_locks(handles)


if __name__ == "__main__":
    raise SystemExit(main())
