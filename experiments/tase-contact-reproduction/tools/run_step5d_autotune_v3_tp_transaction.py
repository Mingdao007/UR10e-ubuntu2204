#!/usr/bin/python3.10
"""Run one manifest-bound TP upload/fresh-GET/atomic-promotion transaction."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

import promote_step5d_r009_atomic_release as promote
import upload_ur_tp_package as upload
from step5d_autotune_v3.dashboard import (
    DashboardProgramLoadError,
    ensure_exact_loaded_program,
)
from step5d_autotune_v3.delivery_observation import build_delivery_observation
from step5d_autotune_v3.profile import ContractViolation, load_contract
from step5d_autotune_v3.release_identity import (
    SAFETY_ENVELOPE_PATH,
    ReleaseIdentity,
    ReleaseIdentityError,
    load_current_release,
    load_local_release_candidate,
    release_payload_path,
)
from step5d_autotune_v3.runtime_identity import (
    RuntimeIdentityError,
    identity_from_manifest,
)
from step5d_autotune_v3.runtime_installation import (
    RuntimeInstallationError,
    owner_dependency,
    require_runtime_profile,
)
from step5d_autotune_v3.runtime_functional_gates import (
    RuntimeFunctionalGateError,
    load_gpu_functional_attestation,
)
from step5d_autotune_v3.qualification import (
    QualificationError,
    capture_content_binding,
    validate_qualification_result,
)
from step5d_autotune_v3.state import StateError, atomic_json, read_strict_json
from ur10e_mutation_lock import (
    acquire_controller_mutation_locks,
    release_controller_mutation_locks,
)


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_LAUNCH_ENV = "STEP5D_V3_CANONICAL_LAUNCHER"
SHELL_PID_ENV = "STEP5D_V3_SHELL_PID"


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


def _require_canonical_shell() -> None:
    launcher = (ROOT / "scripts/step5d-autotune-v3.sh").resolve()
    if os.environ.get(CANONICAL_LAUNCH_ENV) != str(launcher):
        raise RuntimeError(f"use {launcher} bridge")
    if os.environ.get(SHELL_PID_ENV) != str(os.getppid()):
        raise RuntimeError(f"use {launcher} bridge")


def _program_load_host(root: Path, release: ReleaseIdentity) -> str:
    contract_path = release_payload_path(root, release, SAFETY_ENVELOPE_PATH)
    contract = load_contract(contract_path)
    try:
        robot_host = contract["effective_fields"]["runtime_identity"]["robot_host"]
    except (KeyError, TypeError) as exc:
        raise ContractViolation("immutable release contract lacks robot_host") from exc
    if not isinstance(robot_host, str) or not robot_host or any(
        character in robot_host for character in ("\x00", "\r", "\n")
    ):
        raise ContractViolation("immutable release robot_host is unsafe")
    return robot_host


def _internal_program_load_observation(
    release: ReleaseIdentity,
    error: BaseException,
) -> dict[str, object]:
    return {
        "schema": "step5d.autotune-v3/program-load-observation-v1",
        "ok": False,
        "reason_code": "PROGRAM_LOAD_BINDING_INVALID",
        "blocker_class": "INTERNAL",
        "observed_at_unix_ns": time.time_ns(),
        "release_binding": {
            "manifest_sha256": release.manifest_sha256,
            "program_id": release.program_id,
            "controller_target": release.controller_target,
        },
        "dashboard_endpoint": None,
        "expected_program": release.controller_target,
        "before_get_loaded_program": None,
        "load_attempted": False,
        "load_response": None,
        "after_get_loaded_program": None,
        "detail": f"{type(error).__name__}:{error}",
    }


def _load_release_program(
    root: Path,
    release: ReleaseIdentity,
    evidence_path: Path,
) -> tuple[int, dict[str, object]]:
    try:
        robot_host = _program_load_host(root, release)
        observation = ensure_exact_loaded_program(
            robot_host,
            release.controller_target,
        )
    except DashboardProgramLoadError as exc:
        observation = exc.observation
        return_code = 69
    except (ContractViolation, ReleaseIdentityError) as exc:
        observation = _internal_program_load_observation(release, exc)
        return_code = 70
    else:
        return_code = 0
    observation = {
        **observation,
        "release_binding": {
            "manifest_sha256": release.manifest_sha256,
            "program_id": release.program_id,
            "controller_target": release.controller_target,
        },
    }
    atomic_json(evidence_path, observation)
    return return_code, observation


def _validate_candidate_and_qualification(
    root: Path,
    artifact_dir: Path,
    candidate_path: Path,
    qualification_path: Path,
) -> ReleaseIdentity:
    release, _descriptor = load_local_release_candidate(root, candidate_path)
    manifest, _bundle, _targets = promote.compose_local_release(root, artifact_dir)
    recomposed_sha256 = hashlib.sha256(promote.canonical_bytes(manifest)).hexdigest()
    if recomposed_sha256 != release.manifest_sha256:
        raise RuntimeError("local release candidate digest differs before delivery")

    wrapper = read_strict_json(qualification_path, role="qualification worker result")
    required = {
        "schema",
        "ok",
        "release_manifest_sha256",
        "reason_code",
        "remaining_integration_seam",
        "evidence",
    }
    if (
        not isinstance(wrapper, dict)
        or set(wrapper) != required
        or wrapper.get("schema")
        != "step5d.autotune-v3/qualification-worker-result-v1"
        or wrapper.get("ok") is not True
        or wrapper.get("release_manifest_sha256") != release.manifest_sha256
        or wrapper.get("reason_code") != "QUALIFIED"
        or wrapper.get("remaining_integration_seam") is not None
    ):
        raise RuntimeError("production qualification is absent or not candidate-bound")
    evidence = wrapper.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != {"schema", "path", "sha256"}:
        raise RuntimeError("production qualification evidence reference differs")
    evidence_path = Path(str(evidence.get("path", ""))).expanduser()
    if evidence_path.is_symlink() or not evidence_path.is_file():
        raise RuntimeError("production qualification evidence is missing or unsafe")
    evidence_sha256 = str(evidence.get("sha256", ""))
    if _sha256(evidence_path) != evidence_sha256:
        raise RuntimeError("production qualification evidence SHA differs")
    payload = read_strict_json(evidence_path, role="production qualification evidence")
    binding = payload.get("binding") if isinstance(payload, dict) else None
    if not isinstance(binding, dict):
        raise RuntimeError("production qualification binding is missing")
    current = capture_content_binding(
        root,
        manifest_sha256=release.manifest_sha256,
        release_identity=release,
    )
    validate_qualification_result(
        payload,
        experiment_root=root,
        manifest_sha256=release.manifest_sha256,
        source_fingerprint=current["source"]["fingerprint"],
        launcher_sha256=current["launcher"]["sha256"],
        release_identity=release,
    )
    return release


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--release-candidate", type=Path)
    parser.add_argument("--qualification-result", type=Path)
    parser.add_argument("--evidence-output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve(strict=True)
    unresolved_local_dir = args.artifact_dir.expanduser()
    if unresolved_local_dir.is_symlink():
        raise RuntimeError("pending artifact directory is unsafe")
    local_dir = unresolved_local_dir.resolve(strict=True)
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
    if args.evidence_output is None:
        raise RuntimeError("--evidence-output is required for a live delivery transaction")
    unresolved_evidence_output = args.evidence_output.expanduser()
    if unresolved_evidence_output.is_symlink():
        raise RuntimeError("delivery evidence output is unsafe")
    evidence_output = unresolved_evidence_output.resolve(strict=False)
    unresolved_evidence_root = root / "runs"
    if unresolved_evidence_root.is_symlink():
        raise RuntimeError("delivery evidence root is unsafe")
    evidence_root = unresolved_evidence_root.resolve()
    try:
        evidence_output.relative_to(evidence_root)
    except ValueError as exc:
        raise RuntimeError("delivery evidence output escapes runs evidence root") from exc
    program_load_evidence = evidence_output.with_name("program-load-observation.json")
    if program_load_evidence.is_symlink():
        raise RuntimeError("program-load evidence output is unsafe")
    if args.release_candidate is None or args.qualification_result is None:
        raise RuntimeError(
            "--release-candidate and --qualification-result are required before live delivery"
        )
    try:
        runtime_pointer = require_runtime_profile("control")
        load_gpu_functional_attestation(runtime_pointer=runtime_pointer)
    except (RuntimeInstallationError, RuntimeFunctionalGateError) as exc:
        raise RuntimeError(f"production environment gate failed: {exc}") from exc
    try:
        candidate_release = _validate_candidate_and_qualification(
            root,
            local_dir,
            args.release_candidate,
            args.qualification_result,
        )
    except (QualificationError, ReleaseIdentityError, StateError) as exc:
        raise RuntimeError(f"candidate qualification gate failed: {exc}") from exc
    try:
        controller_helper = owner_dependency("controller_helper")
    except RuntimeInstallationError as exc:
        raise RuntimeError(
            f"controller owner dependency gate failed: {exc.reason_code}: {exc.detail}"
        ) from exc
    upload_args.extend(
        [
            "--controller-helper",
            controller_helper["path"],
            "--controller-helper-sha256",
            controller_helper["sha256"],
        ]
    )
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
            unresolved_manifest = Path(
                str(result.get("manifest_path") or "")
            ).expanduser()
            if unresolved_manifest.is_symlink():
                raise RuntimeError("upload-result manifest handoff differs")
            manifest = unresolved_manifest.resolve(strict=True)
            if (
                result.get("schema_version") != "ur10e_upload_result_v1"
                or result.get("upload_transaction_id") != transaction_id
                or _sha256(manifest) != result.get("manifest_sha256")
                or not manifest.is_relative_to((root / "runs").resolve())
            ):
                raise RuntimeError("upload-result manifest handoff differs")
            receipt_sha256 = str(result["manifest_sha256"])
            promotion = promote.promote(
                root,
                manifest,
                local_dir,
                expected_transaction_id=transaction_id,
                expected_manifest_sha256=receipt_sha256,
                expected_candidate_manifest_sha256=candidate_release.manifest_sha256,
            )
            release = load_current_release(root)
            if (
                promotion.get("manifest_sha256") != release.manifest_sha256
                or release.program_id != program_id
            ):
                raise RuntimeError("promoted release pointer identity differs")
            load_rc, program_load = _load_release_program(
                root,
                release,
                program_load_evidence,
            )
            if load_rc != 0:
                print(json.dumps(program_load, sort_keys=True), file=sys.stderr)
                return load_rc
            observation = build_delivery_observation(
                root,
                receipt_path=manifest,
                receipt_sha256=receipt_sha256,
                transaction_id=transaction_id,
                release=release,
            )
            atomic_json(evidence_output, observation)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "release_manifest_sha256": promotion["manifest_sha256"],
                        "delivery_observation": str(evidence_output),
                        "program_load_observation": str(program_load_evidence),
                    },
                    sort_keys=True,
                )
            )
            return 0
    finally:
        release_controller_mutation_locks(handles)


if __name__ == "__main__":
    try:
        _require_canonical_shell()
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"refusing internal TP transaction: {exc}", file=sys.stderr)
        raise SystemExit(64)
