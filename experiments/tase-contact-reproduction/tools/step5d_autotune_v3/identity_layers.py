"""Non-recursive release identity layers for Step5d autotune V3.

The subject identities in this module deliberately contain only canonical
repository-relative source names, source content, and explicit semantic
inputs.  Operational metadata (checkout paths, timestamps, process/host
identity, output locations, selectors, promotion state, artifact self-hashes,
and evidence builder/verifier provenance) is excluded before hashing.

Evidence verifier provenance has its own fingerprint.  It is useful when
auditing an evaluation, but it is never an input to a timing subject, release
basis, or final release fingerprint.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from ur10e_experiment_runtime.identity import canonical_sha256


RELEASE_STAGE_ID = "step5d_strict_rnn_autotune_v3"
CONTROL_PROFILE_ID = "step5d_strict_rnn_autotune_v1"
TP_PROGRAM_ID = "step5d_strict_rnn_autotune_v3_r003"

EXPERIMENT_REPO_PREFIX = "experiments/tase-contact-reproduction"


def _experiment_path(relative: str) -> str:
    return f"{EXPERIMENT_REPO_PREFIX}/{relative}"


TICK_SEMANTICS_PATHS = tuple(
    _experiment_path(relative)
    for relative in (
        "tools/contact_semantics.py",
        "tools/kunwei_rtde_bridge.py",
        "tools/step5c_calibrated_kinematics_audit.py",
        "tools/step5c_strict_rnn.py",
        "tools/step5d_control_contract.py",
        "tools/step5d_paper_outer_loop.py",
        "tools/step5d_runtime_interface.py",
    )
) + (
    "src/ur10e_bringup/config/ur10e_calibration.yaml",
    "src/ur10e_experiment_runtime/ur10e_experiment_runtime/identity.py",
    "src/ur10e_experiment_runtime/ur10e_experiment_runtime/moving_sphere.py",
    "src/ur10e_experiment_runtime/ur10e_experiment_runtime/physical_prior.py",
    "src/ur10e_experiment_runtime/ur10e_experiment_runtime/stage_adapters.py",
)

# The timing subject includes the code that delivers, measures, paces, and
# computes statistics for the samples.  Readiness builders and acceptance
# verifiers remain provenance and are intentionally absent.
TIMING_MEASUREMENT_PATHS = (
    _experiment_path("tools/run_step5d_v30_remote_timing.py"),
    _experiment_path("tools/build_step5d_v30_remote_timing_bundle.py"),
    _experiment_path("tools/step5d_v30_timing.py"),
)

ORCHESTRATION_PATHS = tuple(
    _experiment_path(relative)
    for relative in (
        "tools/run_step5d_autotune_campaign.py",
        "tools/prepare_step5d_autotune_launch.py",
        "tools/step5d_autotune_backend.py",
        "tools/step5d_autotune_contract.py",
        "tools/step5d_autotune_coordinator.py",
        "tools/step5d_autotune_journal.py",
        "tools/step5d_autotune_state_machine.py",
        "tools/step5d_autotune_store.py",
        "tools/step5d_autotune_supervisor.py",
        "tools/step5d_autotune_live_driver.py",
        "tools/step5d_autotune_runtime_lifecycle.py",
        "tools/step5d_autotune_batch_plan.py",
        "tools/run_step5d_autotune_v3_live.py",
        "tools/run_step5d_autotune_v3_bridge.py",
        "tools/build_step5d_autotune_v3_bridge_start_context.py",
        "tools/preflight_step5d_autotune_v3.py",
        "tools/step5d_autotune_v3/arming.py",
        "tools/step5d_autotune_v3/certification.py",
        "tools/step5d_autotune_v3/cli.py",
        "tools/step5d_autotune_v3/launcher.py",
        "tools/step5d_autotune_v3/postprocess.py",
        "tools/step5d_autotune_v3/runtime_calibration.py",
        "tools/step5d_autotune_v3/runtime_profile.py",
        "tools/step5d_autotune_v3/readiness.py",
        "tools/step5d_autotune_v3/service.py",
        "tools/step5d_autotune_v3/state.py",
        "tools/step5d_workflow_state.py",
        "scripts/step5d-autotune-v3.sh",
        "config/systemd/step5d-autotune-v3.service",
        "config/step5/step5d_autotune_v3_launch_profile.json",
        "config/step5d/manifests/step5d_strict_rnn_autotune_v3/runtime_calibration.json",
    )
) + (
    "src/ur10e_experiment_runtime/ur10e_experiment_runtime/authorization.py",
    "src/ur10e_experiment_runtime/ur10e_experiment_runtime/batch.py",
    "src/ur10e_experiment_runtime/ur10e_experiment_runtime/contracts.py",
    "src/ur10e_experiment_runtime/ur10e_experiment_runtime/evidence.py",
    "src/ur10e_experiment_runtime/ur10e_experiment_runtime/failure_to_guard.py",
    "src/ur10e_experiment_runtime/ur10e_experiment_runtime/identity.py",
    "src/ur10e_experiment_runtime/ur10e_experiment_runtime/registry.py",
    "src/ur10e_experiment_runtime/ur10e_experiment_runtime/return_route.py",
    "src/ur10e_experiment_runtime/ur10e_experiment_runtime/rollout.py",
    "src/ur10e_experiment_runtime/ur10e_experiment_runtime/runtime.py",
)

EVIDENCE_VERIFIER_PATHS = (
    _experiment_path("tools/step5d_autotune_v3/identity_layers.py"),
    _experiment_path("tools/step5d_timing_acceptance.py"),
    _experiment_path("tools/verify_step5d_autotune_v3_execution_readiness.py"),
    _experiment_path("tools/rebuild_step5d_autotune_v3_pre_live_evidence.py"),
    _experiment_path("tools/build_step5d_autotune_v3_stopping_bound_evidence.py"),
    _experiment_path("tools/promote_step5d_autotune_v3_stopping_bound_evidence.py"),
    _experiment_path("tools/extract_step5d_autotune_v3_certification.py"),
    _experiment_path("tools/issue_step5d_autotune_v3_certification_authorization.py"),
    _experiment_path("tools/finalize_step5d_autotune_v3_certification.py"),
    _experiment_path("tools/build_step5d_autotune_v3_return_route_evidence.py"),
    _experiment_path("tools/promote_step5d_autotune_v3_return_route_evidence.py"),
    _experiment_path("tools/run_step5d_autotune_v3_ursim_return_gate.py"),
)

SELECTOR_AND_DOCUMENT_PATHS = (
    _experiment_path("STEP5_FLOW.md"),
    _experiment_path("config/current_stage.json"),
    _experiment_path("config/step5_stage_table.json"),
    _experiment_path("config/tase_protocol_table.json"),
    _experiment_path("config/step5d/current.json"),
)

BOUNDED_HOLD_TIMING_CONTRACT: Mapping[str, Any] = {
    "schema": "step5d.autotune-v3/bounded-hold-timing-contract-v1",
    "scheduler": {"policy": "SCHED_OTHER", "priority": 0, "nice": 0},
    "pacing": {
        "clock": "time.perf_counter",
        "control_hz": 500.0,
        "period_s": 0.002,
        "full_tick_release_policy": "absolute",
        "safe_hold_release_policy": "independent_absolute",
    },
    "statistics": {
        "percentile": "numpy_percentile_linear_v1",
        "deadline_comparison": "elapsed_ms_greater_than_or_equal_deadline",
        "all_samples_retained": True,
        "ordered_zero_based_indices": True,
    },
    "solver": {
        "samples": 10_000,
        "p99_max_ms": 1.5,
        "max_exclusive_ms": 2.0,
        "deadline_miss_max": 0,
    },
    "full_tick": {
        "samples": 30_000,
        "p99_max_ms": 1.8,
        "compute_miss_max": 300,
        "schedule_miss_max": 300,
        "max_consecutive_miss": 10,
        "schedule_max_lateness_ms": 1.5,
    },
    "safe_hold": {
        "samples": 30_000,
        "p99_max_ms": 1.8,
        "compute_miss_max": 300,
        "schedule_miss_max": 300,
        "max_consecutive_miss": 10,
        "schedule_max_lateness_ms": 1.5,
    },
    "late_candidate_policy": "discard_and_hold_last_guard_approved_command",
    "exact_stop_priority": "always_dominates_hold",
    "tp_stale_watchdog_s": 1.000,
}

DEFAULT_TICK_SEMANTICS: Mapping[str, Any] = {
    "release_stage_id": RELEASE_STAGE_ID,
    "control_profile_id": CONTROL_PROFILE_ID,
    "tp_program_id": TP_PROGRAM_ID,
    "control_hz": 500.0,
    "moving_sphere": {
        "active_stage": 25,
        "radius_m": 0.015,
        "legacy_aabb_enforced": False,
        "fail_closed": True,
    },
    "stop_transport": "same_tick_exact_stop_v1",
    "fixed_execution_shape": {
        "backend": "cupy",
        "inner_iterations": 512,
        "epsilon": 0.010,
        "sigr_exponent_r": 0.8,
        "qdot_cap_rad_s": 0.05,
    },
}

DEFAULT_ORCHESTRATION_SEMANTICS: Mapping[str, Any] = {
    "release_stage_id": RELEASE_STAGE_ID,
    "control_profile_id": CONTROL_PROFILE_ID,
    "tp_program_id": TP_PROGRAM_ID,
    "batch_fates": ["unattempted", "attempted_incomplete", "ack_completed"],
    "resume_policy": "resume_only_non_ack_completed_rows",
    "authorization_types": [
        "CertificationMotionAuthorization",
        "CampaignAuthorization",
    ],
}


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_NON_SUBJECT_EXACT_KEYS = frozenset(
    {
        "absolute_path",
        "artifact_self_hash",
        "artifact_self_sha256",
        "builder_fingerprint",
        "builder_sha256",
        "current",
        "current_pointer",
        "current_stage",
        "evidence_verifier_fingerprint",
        "host",
        "hostname",
        "latest",
        "latest_pointer",
        "observed_at",
        "output",
        "output_dir",
        "output_path",
        "output_root",
        "parent_pid",
        "path",
        "pid",
        "process_id",
        "promotion_status",
        "self_sha256",
        "timestamp",
        "verifier_fingerprint",
        "verifier_sha256",
    }
)


class IdentityLayerError(ValueError):
    """An identity input is ambiguous, unsafe, or incomplete."""


def _sha256(name: str, value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise IdentityLayerError(f"{name} must be a lowercase SHA256")
    return value


def canonical_repo_relative_path(value: str) -> str:
    """Validate and return one canonical POSIX repository-relative path."""

    if not isinstance(value, str) or not value or "\\" in value:
        raise IdentityLayerError("identity source path must be canonical POSIX text")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise IdentityLayerError(f"identity source path is not repository-relative: {value!r}")
    return value


def _is_absolute_text(value: str) -> bool:
    return value.startswith("/") or _WINDOWS_ABSOLUTE.match(value) is not None


def _non_subject_key(key: str) -> bool:
    lowered = key.lower()
    return bool(
        lowered in _NON_SUBJECT_EXACT_KEYS
        or lowered.endswith("_at")
        or lowered.endswith("_path")
        or lowered.endswith("_timestamp")
        or lowered.endswith("_timestamp_ns")
        or lowered.endswith("_pid")
        or lowered.endswith("_output_path")
        or lowered.endswith("_output_root")
        or lowered.endswith("_absolute_path")
        or lowered.endswith("_self_sha256")
        or lowered.startswith("verifier_")
        or lowered.startswith("builder_")
    )


def subject_semantics(value: Any) -> Any:
    """Remove operational/provenance-only values from semantic material.

    The function returns a fresh JSON value.  Absolute path values and keys are
    dropped rather than normalized, so relocating a byte-identical checkout
    cannot alter a subject fingerprint.
    """

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise IdentityLayerError("identity semantic keys must be strings")
            if _is_absolute_text(raw_key) or _non_subject_key(raw_key):
                continue
            if isinstance(item, str) and _is_absolute_text(item):
                continue
            result[raw_key] = subject_semantics(item)
        return result
    if isinstance(value, (list, tuple)):
        return [
            subject_semantics(item)
            for item in value
            if not (isinstance(item, str) and _is_absolute_text(item))
        ]
    return value


def source_sha256_manifest(
    repository_root: Path,
    relative_paths: Sequence[str],
) -> dict[str, str]:
    """Hash regular files under ``repository_root`` using canonical names."""

    if not isinstance(repository_root, Path):
        raise IdentityLayerError("repository_root must be pathlib.Path")
    root = repository_root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise IdentityLayerError("repository_root must be a directory")
    canonical = tuple(canonical_repo_relative_path(path) for path in relative_paths)
    if len(canonical) != len(set(canonical)):
        raise IdentityLayerError("identity source paths must be unique")
    manifest: dict[str, str] = {}
    for relative in sorted(canonical):
        source = root / relative
        if source.is_symlink() or not source.is_file():
            raise IdentityLayerError(f"identity source must be a regular file: {relative}")
        try:
            source.resolve(strict=True).relative_to(root)
        except ValueError as exc:
            raise IdentityLayerError(f"identity source escapes repository: {relative}") from exc
        manifest[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
    return manifest


def _external_input_manifest(values: Mapping[str, str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for role, digest in (values or {}).items():
        if (
            not isinstance(role, str)
            or not role
            or _is_absolute_text(role)
            or "/" in role
            or "\\" in role
            or _non_subject_key(role)
        ):
            raise IdentityLayerError(f"external input role is not canonical: {role!r}")
        result[role] = _sha256(f"external_inputs.{role}", digest)
    return dict(sorted(result.items()))


def _merge_semantics(base: Mapping[str, Any], extra: Mapping[str, Any] | None) -> dict[str, Any]:
    material = dict(subject_semantics(base))
    for key, value in subject_semantics(extra or {}).items():
        material[key] = value
    return material


def tick_semantics_manifest(
    repository_root: Path,
    *,
    source_paths: Sequence[str] = TICK_SEMANTICS_PATHS,
    semantic_inputs: Mapping[str, Any] | None = None,
    external_inputs: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "step5d.autotune-v3/tick-semantics-identity-v1",
        "sources": source_sha256_manifest(repository_root, source_paths),
        "external_inputs": _external_input_manifest(external_inputs),
        "semantics": _merge_semantics(DEFAULT_TICK_SEMANTICS, semantic_inputs),
    }


def tick_semantics_fingerprint(
    repository_root: Path,
    **kwargs: Any,
) -> str:
    return canonical_sha256(tick_semantics_manifest(repository_root, **kwargs))


def timing_harness_manifest(
    repository_root: Path,
    *,
    source_paths: Sequence[str] = TIMING_MEASUREMENT_PATHS,
    timing_contract: Mapping[str, Any] = BOUNDED_HOLD_TIMING_CONTRACT,
) -> dict[str, Any]:
    return {
        "schema": "step5d.autotune-v3/timing-harness-identity-v1",
        "measurement_sources": source_sha256_manifest(repository_root, source_paths),
        "contract": subject_semantics(timing_contract),
    }


def timing_harness_fingerprint(
    repository_root: Path,
    **kwargs: Any,
) -> str:
    return canonical_sha256(timing_harness_manifest(repository_root, **kwargs))


def runtime_environment_manifest(environment: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(environment, Mapping):
        raise IdentityLayerError("runtime environment must be a mapping")
    return {
        "schema": "step5d.autotune-v3/runtime-environment-identity-v1",
        "environment": subject_semantics(environment),
    }


def runtime_environment_fingerprint(environment: Mapping[str, Any]) -> str:
    return canonical_sha256(runtime_environment_manifest(environment))


def deployment_manifest(
    *,
    triplet_sha256: Mapping[str, str],
    controller_readback_identity: Mapping[str, Any],
    release_stage_id: str = RELEASE_STAGE_ID,
    tp_program_id: str = TP_PROGRAM_ID,
) -> dict[str, Any]:
    if set(triplet_sha256) != {".script", ".txt", ".urp"}:
        raise IdentityLayerError("deployment triplet digest fields differ")
    triplet = {
        suffix: _sha256(f"triplet_sha256.{suffix}", digest)
        for suffix, digest in sorted(triplet_sha256.items())
    }
    readback_required = {
        "schema",
        "verified",
        "program",
        "control_profile_id",
        "controller_target",
        "triplet_sha256",
        "tp_fingerprint",
    }
    missing = readback_required - set(controller_readback_identity)
    if missing:
        raise IdentityLayerError(
            "controller readback subject identity is missing: "
            + ", ".join(sorted(missing))
        )
    readback_triplet = controller_readback_identity["triplet_sha256"]
    if not isinstance(readback_triplet, Mapping) or set(readback_triplet) != {
        ".script",
        ".txt",
        ".urp",
    }:
        raise IdentityLayerError("controller readback triplet digest fields differ")
    readback = {
        "schema": controller_readback_identity["schema"],
        "verified": controller_readback_identity["verified"],
        "program": controller_readback_identity["program"],
        "control_profile_id": controller_readback_identity["control_profile_id"],
        # This is the controller-side program location, not a host checkout or
        # output path, so it is an intentional deployment-semantic value.
        "controller_target": controller_readback_identity["controller_target"],
        "triplet_sha256": {
            suffix: _sha256(
                f"controller_readback_identity.triplet_sha256.{suffix}",
                digest,
            )
            for suffix, digest in sorted(readback_triplet.items())
        },
        "tp_fingerprint": _sha256(
            "controller_readback_identity.tp_fingerprint",
            controller_readback_identity["tp_fingerprint"],
        ),
    }
    if readback["schema"] != "step5d.autotune.controller-readback/v3":
        raise IdentityLayerError("controller readback schema differs")
    if readback["verified"] is not True:
        raise IdentityLayerError("controller readback must be verified")
    if readback["program"] != tp_program_id:
        raise IdentityLayerError("controller readback program differs")
    if readback["control_profile_id"] != CONTROL_PROFILE_ID:
        raise IdentityLayerError("controller readback control profile differs")
    target = readback["controller_target"]
    if (
        not isinstance(target, str)
        or not target.startswith("/programs/")
        or not target.endswith(f"/{tp_program_id}.urp")
        or ".." in PurePosixPath(target).parts
    ):
        raise IdentityLayerError("controller readback target differs")
    return {
        "schema": "step5d.autotune-v3/deployment-identity-v1",
        "release_stage_id": release_stage_id,
        "tp_program_id": tp_program_id,
        "triplet_sha256": triplet,
        "controller_readback": readback,
    }


def deployment_fingerprint(**kwargs: Any) -> str:
    return canonical_sha256(deployment_manifest(**kwargs))


def orchestration_manifest(
    repository_root: Path,
    *,
    source_paths: Sequence[str] = ORCHESTRATION_PATHS,
    semantic_inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "step5d.autotune-v3/orchestration-identity-v1",
        "sources": source_sha256_manifest(repository_root, source_paths),
        "semantics": _merge_semantics(
            DEFAULT_ORCHESTRATION_SEMANTICS,
            semantic_inputs,
        ),
    }


def orchestration_fingerprint(repository_root: Path, **kwargs: Any) -> str:
    return canonical_sha256(orchestration_manifest(repository_root, **kwargs))


def release_basis_manifest(
    *,
    tick_semantics_fingerprint: str,
    timing_harness_fingerprint: str,
    runtime_environment_fingerprint: str,
    deployment_fingerprint: str,
    orchestration_fingerprint: str,
    plant_epoch: int,
    release_stage_id: str = RELEASE_STAGE_ID,
) -> dict[str, Any]:
    if isinstance(plant_epoch, bool) or not isinstance(plant_epoch, int) or plant_epoch < 1:
        raise IdentityLayerError("plant_epoch must be a positive integer")
    return {
        "schema": "step5d.autotune-v3/release-basis-identity-v1",
        "release_stage_id": release_stage_id,
        "plant_epoch": plant_epoch,
        "components": {
            "tick_semantics_fingerprint": _sha256(
                "tick_semantics_fingerprint", tick_semantics_fingerprint
            ),
            "timing_harness_fingerprint": _sha256(
                "timing_harness_fingerprint", timing_harness_fingerprint
            ),
            "runtime_environment_fingerprint": _sha256(
                "runtime_environment_fingerprint", runtime_environment_fingerprint
            ),
            "deployment_fingerprint": _sha256(
                "deployment_fingerprint", deployment_fingerprint
            ),
            "orchestration_fingerprint": _sha256(
                "orchestration_fingerprint", orchestration_fingerprint
            ),
        },
    }


def release_basis_fingerprint(**kwargs: Any) -> str:
    return canonical_sha256(release_basis_manifest(**kwargs))


def release_manifest(
    *,
    release_basis_fingerprint: str,
    stopping_bound_fingerprint: str,
    return_evidence_fingerprint: str,
) -> dict[str, Any]:
    """Build the final release without feeding the result back into its basis."""

    return {
        "schema": "step5d.autotune-v3/release-identity-v1",
        "release_basis_fingerprint": _sha256(
            "release_basis_fingerprint", release_basis_fingerprint
        ),
        "certified_evidence": {
            "stopping_bound_fingerprint": _sha256(
                "stopping_bound_fingerprint", stopping_bound_fingerprint
            ),
            "return_evidence_fingerprint": _sha256(
                "return_evidence_fingerprint", return_evidence_fingerprint
            ),
        },
    }


def release_fingerprint(**kwargs: Any) -> str:
    return canonical_sha256(release_manifest(**kwargs))


def evidence_verifier_manifest(
    repository_root: Path,
    *,
    source_paths: Sequence[str] = EVIDENCE_VERIFIER_PATHS,
) -> dict[str, Any]:
    return {
        "schema": "step5d.autotune-v3/evidence-verifier-provenance-v1",
        "provenance_only": True,
        "sources": source_sha256_manifest(repository_root, source_paths),
    }


def evidence_verifier_fingerprint(
    repository_root: Path,
    **kwargs: Any,
) -> str:
    return canonical_sha256(evidence_verifier_manifest(repository_root, **kwargs))


__all__ = [
    "BOUNDED_HOLD_TIMING_CONTRACT",
    "CONTROL_PROFILE_ID",
    "EVIDENCE_VERIFIER_PATHS",
    "IdentityLayerError",
    "ORCHESTRATION_PATHS",
    "RELEASE_STAGE_ID",
    "SELECTOR_AND_DOCUMENT_PATHS",
    "TICK_SEMANTICS_PATHS",
    "TIMING_MEASUREMENT_PATHS",
    "TP_PROGRAM_ID",
    "canonical_repo_relative_path",
    "deployment_fingerprint",
    "deployment_manifest",
    "evidence_verifier_fingerprint",
    "evidence_verifier_manifest",
    "orchestration_fingerprint",
    "orchestration_manifest",
    "release_basis_fingerprint",
    "release_basis_manifest",
    "release_fingerprint",
    "release_manifest",
    "runtime_environment_fingerprint",
    "runtime_environment_manifest",
    "source_sha256_manifest",
    "subject_semantics",
    "tick_semantics_fingerprint",
    "tick_semantics_manifest",
    "timing_harness_fingerprint",
    "timing_harness_manifest",
]
