#!/usr/bin/python3.10
"""Run one manifest-bound TP delivery/readback/atomic-promotion transaction."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import stat
import subprocess
import sys
import tempfile
import uuid
from typing import Any, Mapping

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
)
from step5d_autotune_v3.release_transition import (
    ReleaseTransitionError,
    build_post_promotion_publication_plan,
    create_delivery_basis,
    delivery_basis_reference,
    git_publication_snapshot as git_snapshot,
    load_delivery_basis,
    publication_plan_sha256,
    write_post_promotion_publication_plan,
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


def _safe_publication_file(root: Path, path: Path, role: str) -> tuple[str, Path]:
    experiment = Path(os.path.abspath(root))
    candidate = Path(os.path.abspath(path.expanduser()))
    if candidate.is_symlink():
        raise RuntimeError(f"{role} is unsafe")
    try:
        relative = candidate.relative_to(experiment)
        resolved = candidate.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{role} must be root-relative") from exc
    if resolved != candidate or not stat.S_ISREG(resolved.stat().st_mode):
        raise RuntimeError(f"{role} must be a regular non-symlink file")
    return PurePosixPath(relative).as_posix(), resolved


def _add_delivery_basis_outputs(
    root: Path,
    candidate_release: ReleaseIdentity,
    basis_path: Path,
    basis_payload: Mapping[str, Any],
    outputs: dict[str, Path],
) -> None:
    """Bind both the basis and its imported receipt to the publication plan."""

    parsed_path, parsed_basis = load_delivery_basis(root, release=candidate_release)
    basis_relative, basis_file = _safe_publication_file(
        root,
        basis_path,
        "delivery basis",
    )
    parsed_relative, parsed_file = _safe_publication_file(
        root,
        parsed_path,
        "parsed delivery basis",
    )
    if basis_relative != parsed_relative or dict(basis_payload) != parsed_basis:
        raise RuntimeError("delivery basis handoff differs")
    prior = parsed_basis.get("prior_full_readback_receipt")
    if not isinstance(prior, Mapping) or not isinstance(prior.get("path"), str):
        raise RuntimeError("delivery basis prior receipt reference is invalid")
    prior_path = root / Path(*PurePosixPath(prior["path"]).parts)
    prior_relative, prior_file = _safe_publication_file(
        root,
        prior_path,
        "imported prior full-readback receipt",
    )
    if basis_file != parsed_file:
        raise RuntimeError("delivery basis handoff resolves inconsistently")
    outputs[basis_relative] = basis_file
    outputs[prior_relative] = prior_file


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--release-candidate", type=Path)
    parser.add_argument("--release-certificate", type=Path)
    parser.add_argument("--evidence-output", type=Path)
    parser.add_argument("--prior-full-readback-receipt", type=Path)
    parser.add_argument("--publication-plan-output", type=Path)
    parser.add_argument(
        "--publish-and-revalidate",
        action="store_true",
        help="consume the fixed publication plan, commit its exact allowlist, then cleanly revalidate",
    )
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
        or args.publication_plan_output is not None
        or not args.readback_only_existing
        or args.dry_run
        ):
        raise RuntimeError(
            "--revalidate-current requires readback-only mode and no candidate, "
            "certificate, prior receipt, or dry-run"
        )
    if args.revalidate_current and args.publish_and_revalidate:
        raise RuntimeError("--publish-and-revalidate cannot be used with --revalidate-current")
    if args.dry_run and args.publish_and_revalidate:
        raise RuntimeError("--publish-and-revalidate cannot be used with --dry-run")
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
    if not args.revalidate_current and args.publication_plan_output is not None:
        unresolved_plan_output = args.publication_plan_output.expanduser()
        if unresolved_plan_output.is_symlink():
            raise RuntimeError("publication plan output is unsafe")
        plan_output = unresolved_plan_output.resolve(strict=False)
        try:
            plan_output.relative_to(evidence_root)
        except ValueError as exc:
            raise RuntimeError("publication plan output escapes runs evidence root") from exc
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
    publication_baseline: dict[str, Any] | None = None
    additional_publication_outputs: dict[str, Path] = {}
    publication_plan_path: Path | None = None
    final_payload: dict[str, object] | None = None
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
        try:
            publication_baseline = git_snapshot(root)
        except ReleaseTransitionError as exc:
            raise RuntimeError(f"TP publication baseline is not clean: {exc}") from exc
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
            basis_path, basis_payload = create_delivery_basis(
                root,
                candidate_release=candidate_release,
                basis_release=basis_release,
                prior_full_receipt=prior_receipt,
            )
            _add_delivery_basis_outputs(
                root,
                candidate_release,
                basis_path,
                basis_payload,
                additional_publication_outputs,
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
                basis_path, basis_payload = create_delivery_basis(
                    root,
                    candidate_release=candidate_release,
                    basis_release=candidate_release,
                    prior_full_receipt=manifest,
                )
                _add_delivery_basis_outputs(
                    root,
                    candidate_release,
                    basis_path,
                    basis_payload,
                    additional_publication_outputs,
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
                if publication_baseline is None:
                    raise RuntimeError("TP publication baseline is missing")
                compatibility_targets = promotion.get("compatibility_targets")
                if not isinstance(compatibility_targets, dict):
                    raise RuntimeError("promotion output compatibility binding is missing")
                publication_plan = build_post_promotion_publication_plan(
                    root,
                    release_manifest_sha256=release.manifest_sha256,
                    program_id=release.program_id,
                    transaction_id=transaction_id,
                    baseline=publication_baseline,
                    compatibility_targets=compatibility_targets,
                    additional_outputs=additional_publication_outputs,
                )
                publication_plan_output = (
                    args.publication_plan_output
                    if args.publication_plan_output is not None
                    else root
                    / "runs/step5d_autotune_v3/post-promotion-publication"
                    / f"{transaction_id}.json"
                )
                intended_plan_sha256 = publication_plan_sha256(publication_plan)
                publication_plan_path = write_post_promotion_publication_plan(
                    root,
                    publication_plan_output,
                    publication_plan,
                    expected_sha256=intended_plan_sha256,
                )
            final_payload = {
                "ok": True,
                "release_manifest_sha256": promotion["manifest_sha256"],
                "delivery_observation": str(evidence_output),
                "indexed_delivery_observation": str(indexed_observation),
                "publication_lineage": (
                    None if lineage_path is None else str(lineage_path)
                ),
                "post_promotion_publication_plan": (
                    None if args.revalidate_current else str(publication_plan_path)
                ),
                "promoted": not args.revalidate_current,
                "dashboard_load_attempted": False,
            }
    finally:
        release_controller_mutation_locks(handles)

    if args.publish_and_revalidate:
        if publication_plan_path is None or final_payload is None:
            raise RuntimeError("publication plan was not produced")
        consumer = root / "tools/finalize_step5d_autotune_v3_publication.py"
        if consumer.is_symlink() or not consumer.is_file():
            raise RuntimeError("publication consumer is missing from the experiment root")
        launcher = os.environ.get(CANONICAL_LAUNCH_ENV)
        if not launcher:
            raise RuntimeError("canonical shell launcher is missing")
        completed = subprocess.run(
            [
                sys.executable,
                str(consumer),
                "--root",
                str(root),
                "--plan",
                str(publication_plan_path),
                "--plan-sha256",
                intended_plan_sha256,
                "--canonical-shell",
                launcher,
            ],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if not completed.stdout.strip():
            raise RuntimeError(
                "publication consumer produced no final JSON: "
                + completed.stderr.strip()
            )
        try:
            publication_payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("publication consumer final JSON is not parseable") from exc
        if not isinstance(publication_payload, dict):
            raise RuntimeError("publication consumer final JSON is not an object")
        print(json.dumps(publication_payload, allow_nan=False, separators=(",", ":"), sort_keys=True))
        return completed.returncode

    assert final_payload is not None
    print(json.dumps(final_payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        _require_canonical_shell()
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"refusing internal TP transaction: {exc}", file=sys.stderr)
        raise SystemExit(64)
