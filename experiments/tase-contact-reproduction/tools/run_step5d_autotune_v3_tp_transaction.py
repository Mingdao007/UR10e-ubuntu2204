#!/usr/bin/python3.10
"""Run one manifest-bound TP delivery/readback/atomic-promotion transaction."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

import promote_step5d_r009_atomic_release as promote
import upload_ur_tp_package as upload
from step5d_autotune_v3.delivery_observation import (
    build_delivery_observation,
    write_indexed_delivery_observation,
)
from step5d_autotune_v3.release_identity import (
    ReleaseIdentity,
    ReleaseIdentityError,
    discover_candidate_artifact_identity,
    load_current_release,
    load_local_release_candidate,
    release_payload_path,
)
from step5d_autotune_v3.release_transition import (
    ReleaseTransitionError,
    create_delivery_basis,
    delivery_basis_reference,
    git_snapshot,
    load_delivery_basis,
    write_publication_lineage,
)
from step5d_autotune_v3.runtime_installation import (
    RuntimeInstallationError,
    owner_dependency,
    require_runtime_profile,
)
from step5d_autotune_v3.release_contract import (
    ReleaseContractError,
    release_contract_scope_for_release,
    validate_release_contract_result,
)
from step5d_autotune_v3.release_certificate import (
    ReleaseCertificateError,
    load_release_certificate,
)
from step5d_autotune_v3.state import StateError, atomic_json
from ur10e_mutation_lock import (
    acquire_controller_mutation_locks,
    release_controller_mutation_locks,
)


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_LAUNCH_ENV = "STEP5D_V3_CANONICAL_LAUNCHER"
SHELL_PID_ENV = "STEP5D_V3_SHELL_PID"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_program(root: Path, local_dir: Path) -> str:
    try:
        return discover_candidate_artifact_identity(root, local_dir).program_id
    except ReleaseIdentityError as exc:
        raise RuntimeError(f"TP deploy artifact identity is invalid: {exc}") from exc


def _require_canonical_shell() -> None:
    launcher = (ROOT / "scripts/step5d-autotune-v3.sh").resolve()
    if os.environ.get(CANONICAL_LAUNCH_ENV) != str(launcher):
        raise RuntimeError(f"use {launcher} tp-deliver")
    if os.environ.get(SHELL_PID_ENV) != str(os.getppid()):
        raise RuntimeError(f"use {launcher} tp-deliver")


def _validate_candidate_and_certificate(
    root: Path,
    artifact_dir: Path,
    candidate_path: Path,
    certificate_path: Path,
) -> ReleaseIdentity:
    release, _descriptor = load_local_release_candidate(root, candidate_path)
    manifest, _bundle, _targets = promote.compose_local_release(root, artifact_dir)
    recomposed_sha256 = hashlib.sha256(promote.canonical_bytes(manifest)).hexdigest()
    if recomposed_sha256 != release.manifest_sha256:
        raise RuntimeError("local release candidate digest differs before delivery")

    scope = release_contract_scope_for_release(
        root,
        release,
    )
    _certificate, _evidence_path, payload = load_release_certificate(
        root / "runs/step5d_autotune_v3",
        certificate_path,
        expected_scope=scope,
    )
    validate_release_contract_result(
        payload,
        expected_scope=scope,
    )
    return release


def _current_release_artifact_dir(root: Path) -> Path:
    release = load_current_release(root)
    directories = {
        release_payload_path(root, release, reference["path"]).parent
        for reference in release.artifacts.values()
    }
    if len(directories) != 1:
        raise RuntimeError(
            "current immutable release TP artifacts do not share one directory"
        )
    return directories.pop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--release-candidate", type=Path)
    parser.add_argument("--release-certificate", type=Path)
    parser.add_argument("--evidence-output", type=Path)
    parser.add_argument("--prior-full-readback-receipt", type=Path)
    parser.add_argument(
        "--revalidate-current",
        action="store_true",
        help="freshly revalidate the merged current release without promotion",
    )
    parser.add_argument(
        "--readback-only-existing",
        action="store_true",
        help=(
            "freshly GET and adopt the exact candidate only after triplet SHA "
            "closure; do not upload or Load"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.revalidate_current and (
        args.release_candidate is not None
        or args.release_certificate is not None
        or args.prior_full_readback_receipt is not None
        or not args.readback_only_existing
        or args.dry_run
    ):
        raise RuntimeError(
            "--revalidate-current requires readback-only mode and no candidate, "
            "certificate, prior receipt, or dry-run"
        )
    root = args.root.resolve(strict=True)
    if args.artifact_dir is None:
        if not args.revalidate_current:
            raise RuntimeError("--artifact-dir is required before TP delivery")
        local_dir = _current_release_artifact_dir(root)
    else:
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
    program_id = _artifact_program(root, local_dir)
    upload_args = [
        program_id,
        "--local-dir", str(local_dir),
        "--readback-root", str(root / "runs"),
        "--target-dir", promote.TARGET_DIR,
        "--override-table",
        "--override-reason",
        (
            f"manifest-driven existing {program_id} fresh GET and "
            + (
                "non-promoting runtime revalidation"
                if args.revalidate_current
                else "atomic promotion"
            )
            if args.readback_only_existing
            else f"manifest-driven {program_id} upload, fresh GET, and atomic promotion"
        ),
    ]
    if args.dry_run:
        if args.readback_only_existing:
            raise RuntimeError("--readback-only-existing does not support dry-run")
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
    if (
        not args.revalidate_current
        and (args.release_candidate is None or args.release_certificate is None)
    ):
        raise RuntimeError(
            "--release-candidate and --release-certificate are required before TP delivery"
        )
    try:
        require_runtime_profile("control")
    except RuntimeInstallationError as exc:
        raise RuntimeError(f"control environment gate failed: {exc}") from exc
    delivery_basis: dict[str, str] | None = None
    if args.revalidate_current:
        git_snapshot(root)
        candidate_release = load_current_release(root)
        manifest, _bundle, _targets = promote.compose_local_release(
            root,
            local_dir,
        )
        recomposed_sha256 = hashlib.sha256(
            promote.canonical_bytes(manifest)
        ).hexdigest()
        if recomposed_sha256 != candidate_release.manifest_sha256:
            raise RuntimeError(
                "merged current release does not recompose from deployed sources"
            )
        load_delivery_basis(root, release=candidate_release)
        delivery_basis = delivery_basis_reference(
            root,
            release=candidate_release,
        )
    else:
        assert args.release_candidate is not None
        assert args.release_certificate is not None
        try:
            candidate_release = _validate_candidate_and_certificate(
                root,
                local_dir,
                args.release_candidate,
                args.release_certificate,
            )
        except (
            ReleaseContractError,
            ReleaseCertificateError,
            ReleaseIdentityError,
            StateError,
        ) as exc:
            raise RuntimeError(f"candidate certificate gate failed: {exc}") from exc
        if args.readback_only_existing:
            prior_receipt = args.prior_full_readback_receipt
            if prior_receipt is not None:
                basis_release = candidate_release
            else:
                basis_release = load_current_release(root)
                _basis_path, prior_basis = load_delivery_basis(
                    root,
                    release=basis_release,
                )
                prior_receipt = root / prior_basis[
                    "prior_full_readback_receipt"
                ]["path"]
            create_delivery_basis(
                root,
                candidate_release=candidate_release,
                basis_release=basis_release,
                prior_full_receipt=prior_receipt,
            )
            delivery_basis = delivery_basis_reference(
                root,
                release=candidate_release,
            )
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
    if delivery_basis is not None:
        upload_args.extend(
            [
                "--delivery-basis-path",
                delivery_basis["path"],
                "--delivery-basis-sha256",
                delivery_basis["sha256"],
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
                    (
                        "--readback-only-existing"
                        if args.readback_only_existing
                        else "--force-upload-readback"
                    ),
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
            if not args.readback_only_existing:
                create_delivery_basis(
                    root,
                    candidate_release=candidate_release,
                    basis_release=candidate_release,
                    prior_full_receipt=manifest,
                )
            observation = build_delivery_observation(
                root,
                receipt_path=manifest,
                receipt_sha256=receipt_sha256,
                transaction_id=transaction_id,
                release=candidate_release,
            )
            atomic_json(evidence_output, observation)
            indexed_observation = write_indexed_delivery_observation(
                root,
                observation,
                release=candidate_release,
            )

            # Publishing current-release is the transaction commit point.  Once
            # that pointer changes, its immutable delivery receipt already
            # exists and can be resolved after any subsequent process crash.
            if args.revalidate_current:
                release = load_current_release(root)
                if (
                    release.manifest_sha256 != candidate_release.manifest_sha256
                    or release.program_id != program_id
                    or release.manifest_sha256
                    != observation["release_manifest_sha256"]
                ):
                    raise RuntimeError(
                        "revalidated current release identity differs"
                    )
                lineage_path, _lineage = write_publication_lineage(
                    root,
                    release=release,
                )
                promotion = {
                    "manifest_sha256": release.manifest_sha256,
                    "promoted": False,
                }
            else:
                promotion = promote.promote(
                    root,
                    manifest,
                    local_dir,
                    expected_transaction_id=transaction_id,
                    expected_manifest_sha256=receipt_sha256,
                    expected_candidate_manifest_sha256=candidate_release.manifest_sha256,
                    expected_delivery_basis=(
                        delivery_basis
                        if args.readback_only_existing
                        else None
                    ),
                )
                release = load_current_release(root)
                lineage_path = None
                if (
                    promotion.get("manifest_sha256") != release.manifest_sha256
                    or release.program_id != program_id
                    or release.manifest_sha256
                    != observation["release_manifest_sha256"]
                ):
                    raise RuntimeError("promoted release pointer identity differs")
            print(
                json.dumps(
                    {
                        "ok": True,
                        "release_manifest_sha256": promotion["manifest_sha256"],
                        "delivery_observation": str(evidence_output),
                        "indexed_delivery_observation": str(indexed_observation),
                        "publication_lineage": (
                            None if lineage_path is None else str(lineage_path)
                        ),
                        "promoted": not args.revalidate_current,
                        "dashboard_load_attempted": False,
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
