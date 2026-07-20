#!/usr/bin/env python3
"""Resolve the Step5d Autotune V3 pre-live state fail-closed."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import build_step5d_autotune_v3_return_route_evidence as return_builder
from step5d_autotune_v3.profile import (
    ContractViolation,
    active_identity_snapshot,
    load_contract,
)
from step5d_autotune_v3.state import StateError
from step5d_timing_acceptance import evaluate_step5d_v3_timing_raw


ROOT = Path(__file__).resolve().parents[1]
V1_STAGE_ID = "step5d_strict_rnn_autotune_v1"
V3_STAGE_ID = "step5d_strict_rnn_autotune_v3"
VALIDATION_SCOPE = "offline_pre_live_only"
MACHINE_BINDING = "machine_generated_epoch_and_process_fingerprint"
PRE_LIVE_VALIDATION_DECISION = {
    "acceptance_scope": VALIDATION_SCOPE,
    "offline_implementation": "pass",
    "hardware_promotion": "blocked",
    "current_selector": V3_STAGE_ID,
    "v3_active": True,
    "robot_power_state": "POWER_OFF_AT_AUDIT_NOT_CURRENT_ASSERTION",
    "certification_motion_authorization_required": True,
    "campaign_authorization_required": True,
    "live_motion_authorized": False,
    "execution_readiness": "pre_live_blocked",
}
SEAM_EVIDENCE_RELATIVE = (
    "config/step5/step5d_autotune_v3_sphere_seam_timing_c1c066f7.json"
)
FORMAL_RAW_RELATIVE = (
    "config/step5/step5d_autotune_v3_formal_timing_raw_c1c066f7.json"
)
FORMAL_EVALUATION_RELATIVE = (
    "config/step5/step5d_autotune_v3_formal_timing_evaluation_c1c066f7.json"
)
URSIM_RAW_RELATIVE = (
    "config/step5/step5d_autotune_v3_ursim_hold_raw_c1c066f7.json"
)
URSIM_RESULT_RELATIVE = (
    "config/step5/step5d_autotune_v3_ursim_hold_result_c1c066f7.json"
)
CURRENT_URSIM_RAW_RELATIVE = (
    "config/step5/step5d_autotune_v3_ursim_hold_raw_6cc7320e_v3.json"
)
CURRENT_URSIM_RESULT_RELATIVE = (
    "config/step5/step5d_autotune_v3_ursim_hold_result_6cc7320e_v3.json"
)
STOPPING_BOUND_EVIDENCE_RELATIVE = (
    "config/step5/step5d_autotune_v3_stopping_bound_evidence.json"
)
RETURN_ROUTE_EVIDENCE_RELATIVE = (
    "config/step5/step5d_autotune_v3_return_route_evidence.json"
)
URSIM_RETURN_TRACE_RELATIVE = (
    "config/step5/step5d_autotune_v3_ursim_return_trace.json"
)
TIMING_EQUIVALENCE_RELATIVE = (
    "config/step5/"
    "step5d_autotune_v3_formal_timing_raw_ede7bdb5.equivalence.json"
)
READINESS_EVIDENCE_RELATIVE_PATHS = {
    SEAM_EVIDENCE_RELATIVE,
    FORMAL_RAW_RELATIVE,
    FORMAL_EVALUATION_RELATIVE,
    URSIM_RAW_RELATIVE,
    URSIM_RESULT_RELATIVE,
    CURRENT_URSIM_RAW_RELATIVE,
    CURRENT_URSIM_RESULT_RELATIVE,
    STOPPING_BOUND_EVIDENCE_RELATIVE,
    RETURN_ROUTE_EVIDENCE_RELATIVE,
    URSIM_RETURN_TRACE_RELATIVE,
    TIMING_EQUIVALENCE_RELATIVE,
    "config/step5d_v29_remote_evidence_sha256.json",
    "config/step5d_liveprep_solver_gate.json",
    "config/step5d_v30_profile_selection.json",
    "tools/contact_semantics.py",
    "tools/step5c_strict_rnn.py",
    "tools/step5d_paper_outer_loop.py",
    "tools/step5d_control_contract.py",
    "tools/step5d_runtime_interface.py",
    "tools/step5c_calibrated_kinematics_audit.py",
    "tools/kunwei_rtde_bridge.py",
    "tools/run_step5d_v30_remote_timing.py",
    "tools/build_step5d_v30_remote_timing_bundle.py",
    "tools/step5d_v30_timing.py",
    "tools/build_step5d_v30_offline_readiness.py",
    "tools/benchmark_step5d_v3_sphere_seam.py",
    "tools/build_step5d_autotune_tp_v3.py",
    "tools/promote_step5d_autotune_v3_stopping_bound_evidence.py",
    "tools/promote_step5d_autotune_v3_return_route_evidence.py",
    "tools/run_step5d_autotune_v3_ursim_return_gate.py",
    "tools/rebuild_step5d_autotune_v3_pre_live_evidence.py",
    "tools/run_step5d_parallel_workflow.py",
    "tools/step5d_timing_acceptance.py",
    "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/authorization.py",
    "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/identity.py",
    "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/moving_sphere.py",
    "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/physical_prior.py",
    "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/stage_adapters.py",
    "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/return_route.py",
    "../../src/ur10e_bringup/config/ur10e_calibration.yaml",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReadinessError(RuntimeError):
    """The persisted release state cannot support a truthful operator signal."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReadinessError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, *, role: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReadinessError(f"{role} is missing or unsafe")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ReadinessError(f"non-finite JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReadinessError(f"{role} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReadinessError(f"{role} must be an object")
    return payload


def _require(actual: Any, expected: Any, role: str) -> None:
    if actual != expected:
        raise ReadinessError(
            f"{role} differs: expected={expected!r}, observed={actual!r}"
        )


def _zoned_timestamp(value: Any, *, role: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReadinessError(f"{role} is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReadinessError(f"{role} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReadinessError(f"{role} lacks an explicit timezone")
    return value


def _v3_row(table: Mapping[str, Any]) -> Mapping[str, Any]:
    rows = [
        row
        for row in table.get("stages", [])
        if isinstance(row, dict) and row.get("id") == V3_STAGE_ID
    ]
    if len(rows) != 1:
        raise ReadinessError("v3 stage row must exist exactly once")
    return rows[0]


def _reference(root: Path, value: Any, *, role: str) -> tuple[Path, str]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise ReadinessError(f"{role} reference fields differ")
    relative = value.get("path")
    expected_sha = value.get("sha256")
    if (
        not isinstance(relative, str)
        or not isinstance(expected_sha, str)
        or _SHA256.fullmatch(expected_sha) is None
    ):
        raise ReadinessError(f"{role} reference is invalid")
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise ReadinessError(f"{role} is missing or unsafe")
    _require(_sha256(path), expected_sha, f"{role} digest")
    return path, expected_sha


def _seam_source_path(root: Path, relative: str) -> Path:
    experiment_prefix = "experiments/tase-contact-reproduction/"
    if relative.startswith(experiment_prefix):
        return root / relative.removeprefix(experiment_prefix)
    return root.parents[1] / relative


def _verify_validation(
    root: Path,
    *,
    current_identity: Mapping[str, Any],
    readback_relative: str,
    readback_sha256: str,
) -> tuple[Path, Mapping[str, Any], bool]:
    path = root / "config/step5/step5d_autotune_v3_offline_validation.json"
    validation = _load_json(path, role="deterministic validation")
    _require(
        validation.get("schema"),
        "step5d.autotune-v3/offline-acceptance-v5",
        "validation schema",
    )
    _zoned_timestamp(validation.get("observed_at"), role="validation timestamp")
    _require(
        validation.get("identity"),
        current_identity,
        "current validation identity",
    )
    _require(
        validation.get("decision"),
        PRE_LIVE_VALIDATION_DECISION,
        "pre-live validation decision",
    )

    gates = validation.get("gates") or {}
    for name in ("repository_validation", "control_semantics", "exact_batch_lifecycle"):
        _require((gates.get(name) or {}).get("status"), "pass", f"validation gate {name}")
    ten_trial = gates.get("exact_batch_lifecycle") or {}
    _require(ten_trial.get("batch_size"), 10, "V3 validation batch size")
    _require(ten_trial.get("ack_completed_rows"), 10, "ACK-completed rows")
    _require(ten_trial.get("trial_briefs"), 10, "post-closure TrialBrief rows")
    _require(ten_trial.get("batch_result_exit_code"), 0, "BatchResult exit code")

    replay = gates.get("diagnostic_bundle_replay") or {}
    _require(replay.get("status"), "pass", "diagnostic bundle replay")
    _require(replay.get("bundle_count"), 10, "diagnostic bundle count")
    _require(replay.get("actual_sphere_breach_count"), 0, "actual sphere breaches")
    _require(replay.get("trial21_force_metric_role"), "unavailable", "trial21 metric role")
    _require(replay.get("optimizer_eligible_count"), 0, "diagnostic optimizer exclusion")

    seam_timing = gates.get("source_exact_sphere_seam_timing") or {}
    formal_timing = gates.get("formal_500hz_timing") or {}
    timing_passed = (
        formal_timing.get("status")
        == "pass_current_source_formal_500hz_timing"
    )
    timing_not_required = formal_timing.get("status") == "not_required_by_user"
    if not timing_passed and not timing_not_required:
        raise ReadinessError("formal timing status differs")
    scheduler_contract = (
        (
            ("scheduler_policy_required", None),
            ("scheduler_priority_required", None),
            ("nice_required", None),
        )
        if timing_not_required
        else (
            ("scheduler_policy_required", "SCHED_OTHER"),
            ("scheduler_priority_required", 0),
            ("nice_required", 0),
        )
    )
    for field, expected in scheduler_contract:
        _require(seam_timing.get(field), expected, f"sphere seam timing {field}")
    lanes = formal_timing.get("required_lanes") or {}
    lane_counts = (
        {}
        if timing_not_required
        else {
            "solver": 10_000,
            "full_tick_with_sphere": 30_000,
            "safe_hold": 30_000,
        }
    )
    _require(set(lanes), set(lane_counts), "formal timing lanes")
    expected_lane_statuses = (
        {lane: "pass" for lane in lane_counts}
        if timing_passed
        else {
            "solver": "pass_reused_active_surface_equivalence",
            "full_tick_with_sphere": "pending_final_source_exact_capture",
            "safe_hold": "pass_reused_active_surface_equivalence",
        }
    )
    for lane, samples in lane_counts.items():
        _require(
            (lanes.get(lane) or {}).get("required_samples"),
            samples,
            f"formal timing {lane} samples",
        )
        _require(
            (lanes.get(lane) or {}).get("status"),
            expected_lane_statuses[lane],
            f"formal timing {lane} status",
        )
    if timing_not_required:
        for field, expected in (
            ("status", "diagnostic_only_not_required_by_user"),
            ("claim_class", "diagnostic_only"),
            ("attempt_count", 0),
            ("release_gate_applicable", False),
            ("system_setup_required", False),
            ("pressure_test_required", False),
        ):
            _require(seam_timing.get(field), expected, f"sphere seam timing {field}")
        for field, expected in (
            ("release_gate_applicable", False),
            ("release_gate_satisfied", False),
            ("attempt_count", 0),
            ("acceptance_classification", "not_required_by_user"),
            ("system_setup_required", False),
            ("pressure_test_required", False),
        ):
            _require(formal_timing.get(field), expected, f"formal timing {field}")
    if timing_passed:
        _require(
            seam_timing.get("status"),
            "pass_via_combined_formal_full_tick_with_sphere",
            "sphere seam timing",
        )
        _require(
            seam_timing.get("claim_class"),
            "current_source_formal_timing_component",
            "sphere seam timing claim class",
        )
        _require(seam_timing.get("attempt_count"), 1, "sphere seam timing attempts")
        _require(
            formal_timing.get("release_gate_satisfied"),
            True,
            "formal timing release gate",
        )
        _require(formal_timing.get("attempt_count"), 1, "formal timing attempt count")
        current_attempt = formal_timing.get("current_attempt") or {}
        _require(
            seam_timing.get("current_attempt"),
            current_attempt,
            "combined timing attempt binding",
        )
        raw_path, raw_sha = _reference(
            root,
            current_attempt.get("raw_artifact"),
            role="current formal timing raw",
        )
        evaluation_path, _ = _reference(
            root,
            current_attempt.get("evaluation_artifact"),
            role="current formal timing evaluation",
        )
        metadata_path, _ = _reference(
            root,
            current_attempt.get("execution_metadata"),
            role="current formal timing execution metadata",
        )
        persisted = _load_json(evaluation_path, role="current timing evaluation")
        recomputed = evaluate_step5d_v3_timing_raw(root, raw_path)
        _require(persisted, recomputed, "current timing canonical evaluation")
        _require(persisted.get("raw_sha256"), raw_sha, "current timing raw binding")
        _require(persisted.get("accepted"), True, "current timing acceptance")
        _require(
            formal_timing.get("acceptance_classification"),
            persisted.get("classification"),
            "formal timing classification",
        )
        metadata = _load_json(metadata_path, role="current timing metadata")
        for field, expected in (
            ("formal", True),
            ("step5d_v3_moving_sphere", True),
            ("source_fingerprint_stable", True),
            ("harness_exit_code", 0),
        ):
            _require(metadata.get(field), expected, f"timing metadata {field}")
        runtime = (_load_json(raw_path, role="current timing raw")).get(
            "runtime_environment"
        ) or {}
        for field, expected in (
            ("scheduler_policy_name", "SCHED_OTHER"),
            ("scheduler_priority", 0),
            ("nice", 0),
        ):
            _require(runtime.get(field), expected, f"timing runtime {field}")
    elif not timing_not_required:
        _require(
            seam_timing.get("status"),
            "pass_reused_safe_hold_active_surface_equivalence",
            "sphere seam timing",
        )
        _require(
            seam_timing.get("claim_class"),
            "formal_lane_reuse_not_full_acceptance",
            "sphere seam timing claim class",
        )
        _require(seam_timing.get("attempt_count"), 0, "sphere seam timing attempts")
        _require(
            seam_timing.get("blocker"),
            "requires_final_source_exact_full_tick_30k",
            "sphere seam timing blocker",
        )
        equivalence_path, equivalence_sha = _reference(
            root,
            formal_timing.get("equivalence_attestation"),
            role="timing active-surface equivalence",
        )
        _require(
            seam_timing.get("equivalence_attestation"),
            formal_timing.get("equivalence_attestation"),
            "sphere seam timing equivalence binding",
        )
        equivalence = _load_json(
            equivalence_path,
            role="timing active-surface equivalence",
        )
        _require(
            equivalence.get("schema"),
            "step5d.autotune-v3/timing-active-surface-equivalence-v1",
            "timing equivalence schema",
        )
        _require(
            _sha256(equivalence_path),
            equivalence_sha,
            "timing equivalence digest",
        )
        subjects = equivalence.get("subject_identity") or {}
        _require(
            (subjects.get("tick_semantics") or {}).get(
                "target_layered_fingerprint"
            ),
            current_identity["tick_semantics_fingerprint"],
            "timing equivalence tick identity",
        )
        _require(
            (subjects.get("timing_harness") or {}).get(
                "target_layered_fingerprint"
            ),
            current_identity["timing_harness_fingerprint"],
            "timing equivalence harness identity",
        )
        _require(
            {
                lane: (row or {}).get("reusable")
                for lane, row in (equivalence.get("lane_reuse") or {}).items()
            },
            {"solver": True, "safe_hold": True, "full_tick": False},
            "timing equivalence lane reuse",
        )
        _require(
            formal_timing.get("bound_identity"),
            {
                "tick_semantics_fingerprint": current_identity[
                    "tick_semantics_fingerprint"
                ],
                "timing_harness_fingerprint": current_identity[
                    "timing_harness_fingerprint"
                ],
                "runtime_environment_fingerprint": (
                    (subjects.get("runtime_environment") or {}).get(
                        "fingerprint"
                    )
                ),
            },
            "partial timing bound identity",
        )
        prior_seam_path, _ = _reference(
            root,
            seam_timing.get("retained_prior_attempt"),
            role="retained prior sphere seam evidence",
        )
        prior_seam = _load_json(
            prior_seam_path, role="retained prior sphere seam evidence"
        )
        _require(prior_seam.get("pass"), False, "retained prior sphere seam result")
        _require(
            (prior_seam.get("compute") or {}).get("samples"),
            30_000,
            "retained prior sphere seam samples",
        )
        prior_source = prior_seam.get("source_sha256") or {}
        if not isinstance(prior_source, Mapping) or not prior_source:
            raise ReadinessError("retained prior sphere seam source binding is missing")
        if all(
            isinstance(relative, str)
            and isinstance(expected_sha, str)
            and _seam_source_path(root, relative).is_file()
            and _sha256(_seam_source_path(root, relative)) == expected_sha
            for relative, expected_sha in prior_source.items()
        ):
            raise ReadinessError(
                "retained prior sphere seam unexpectedly matches current source"
            )
        _require(
            formal_timing.get("status"),
            "blocked_final_full_tick_pending",
            "formal timing status",
        )
        _require(
            formal_timing.get("release_gate_satisfied"),
            False,
            "formal timing release gate",
        )
        _require(formal_timing.get("attempt_count"), 0, "formal timing attempt count")
        _require(
            formal_timing.get("acceptance_classification"),
            "partial_lane_reuse_attested",
            "formal timing classification",
        )
        _require(
            formal_timing.get("blocker"),
            "requires_final_source_exact_full_tick_30k",
            "formal timing blocker",
        )
        prior_formal = formal_timing.get("retained_prior_failed_attempt") or {}
        _require(
            prior_formal.get("classification"),
            "diagnostic_only_not_v3_acceptance",
            "retained formal timing classification",
        )
        raw_path, raw_sha = _reference(
            root,
            prior_formal.get("raw_artifact"),
            role="retained formal timing raw",
        )
        evaluation_path, _ = _reference(
            root,
            prior_formal.get("evaluation_artifact"),
            role="retained formal timing evaluation",
        )
        persisted = _load_json(evaluation_path, role="retained timing evaluation")
        _require(persisted.get("raw_sha256"), raw_sha, "formal timing raw binding")
        _require(persisted.get("accepted"), False, "retained formal timing acceptance")
        _require(
            prior_formal.get("blockers"),
            [
                "base_formal_timing_not_accepted",
                "v3_requires_production_sched_other_timing_contract",
            ],
            "retained formal timing blockers",
        )

    simulation = gates.get("simulation") or {}
    _require(
        simulation.get("status"),
        "pass_motion_capable_ursim_core_source_equivalence",
        "simulation status",
    )
    _require(
        simulation.get("ursim"),
        "pass_motion_capable_ursim_core_source_equivalence",
        "URSim status",
    )
    return_trace_path, _ = _reference(
        root,
        simulation.get("return_route_motion_trace"),
        role="current URSim return trace",
    )
    return_trace = _load_json(return_trace_path, role="current URSim return trace")
    _require(return_trace.get("status"), "pass", "URSim return trace result")
    _require(
        (return_trace.get("network") or {}).get("real_robot_network_connected"),
        False,
        "URSim real-network exclusion",
    )
    _require(
        return_trace.get("cleanup"),
        {
            "container_removed": True,
            "network_removed": True,
            "program_stopped": True,
        },
        "URSim cleanup",
    )
    for field, role in (
        ("retained_hold_raw_artifact", "retained URSim hold raw"),
        ("retained_hold_result_artifact", "retained URSim hold result"),
    ):
        if simulation.get(field) is not None:
            _reference(root, simulation.get(field), role=role)
    power_off = gates.get("power_off_controller_audit") or {}
    _require(
        power_off.get("status"),
        "retained_prior_read_only_boundary_current_candidate_unverified",
        "Power-OFF audit",
    )
    _require(power_off.get("robot_mode"), "POWER_OFF", "Power-OFF robot mode")
    _require(power_off.get("program_state"), "STOPPED", "Power-OFF program state")
    _require(power_off.get("content_identity_verified"), False, "Power-OFF identity")
    _require(power_off.get("writes_performed"), False, "Power-OFF write exclusion")

    package_gate = gates.get("package_and_readback") or {}
    _require(
        package_gate.get("status"),
        "pass_current_triplet_controller_readback",
        "package/readback status",
    )
    _require(package_gate.get("blocker"), None, "package blocker")
    _require(package_gate.get("controller_readback"), readback_relative, "controller readback path")
    _require(package_gate.get("controller_readback_sha256"), readback_sha256, "controller readback digest")
    stopping = gates.get("stopping_bound") or {}
    _require(stopping.get("status"), "blocked", "stopping-bound status")
    _require(stopping.get("certified"), False, "stopping-bound certification")
    _require(stopping.get("fail_closed"), True, "stopping-bound fail-closed state")
    stopping_path, _ = _reference(
        root, stopping.get("evidence"), role="stopping-bound evidence"
    )
    stopping_evidence = _load_json(stopping_path, role="stopping-bound evidence")
    for field, expected in (
        ("schema", "step5d.autotune-v3/stopping-bound-evidence-v2"),
        ("status", "incomplete_attended_measurement_required"),
        ("certified", False),
        ("optimizer_eligible", False),
        ("deployment_readback_sha256", None),
        ("certification_authorization_sha256", None),
        ("certification_binding_sha256", None),
        ("plant_epoch", None),
        ("measurement_sha256", None),
        ("stopping_bound_fingerprint", None),
        ("live_effect", "stopping_bound_none_fail_closed"),
    ):
        _require(stopping_evidence.get(field), expected, f"stopping evidence {field}")
    stopping_sources = {
        "bridge": _sha256(root / "tools/kunwei_rtde_bridge.py"),
        "moving_sphere": _sha256(
            root.parents[1]
            / "src/ur10e_experiment_runtime/ur10e_experiment_runtime/moving_sphere.py"
        ),
        "stage_adapter": _sha256(
            root.parents[1]
            / "src/ur10e_experiment_runtime/ur10e_experiment_runtime/stage_adapters.py"
        ),
    }
    _require(stopping_evidence.get("source_sha256"), stopping_sources, "stopping evidence source")
    _require(
        stopping_evidence.get("source_binding_sha256"),
        _canonical_sha256(stopping_sources),
        "stopping evidence source binding",
    )
    _require(
        stopping_evidence.get("stop_transport_sha256"),
        stopping_sources["bridge"],
        "stopping evidence transport",
    )
    components = stopping_evidence.get("components") or []
    expected_components = [
        ("reaction_latency_s", "missing", None),
        ("acceleration_growth_m_s2", "missing", None),
        ("minimum_deceleration_m_s2", "missing", None),
        ("center_speed_bound_m_s", "certified_for_domain", 0.003),
        ("center_acceleration_bound_m_s2", "certified_for_domain", 0.00015),
        ("numeric_margin_m", "missing", None),
    ]
    _require(len(components), len(expected_components), "stopping evidence component count")
    for component, (role, status, value) in zip(components, expected_components):
        _require(component.get("role"), role, f"stopping component {role} role")
        _require(component.get("status"), status, f"stopping component {role} status")
        _require(component.get("value"), value, f"stopping component {role} value")
    angular = gates.get("return_route_angular_envelope") or {}
    _require(angular.get("status"), "blocked", "return-route angular status")
    _require(angular.get("certified"), False, "return-route angular certification")
    _require(angular.get("fail_closed"), True, "return-route angular fail-closed state")
    _require(angular.get("offline_enforcement_complete"), True, "return-route offline enforcement")
    angular_path, _ = _reference(
        root, angular.get("evidence"), role="return-route angular evidence"
    )
    angular_evidence = _load_json(angular_path, role="return-route angular evidence")
    for field, expected in (
        ("schema", "step5d.autotune-v3/return-route-evidence-v2"),
        ("status", "offline_enforcement_complete_attended_certification_required"),
        ("certified", False),
        ("optimizer_eligible", False),
        ("certification_authorization_sha256", None),
        ("certification_binding_sha256", None),
        ("plant_epoch", None),
        ("deployment_readback_sha256", None),
        ("attended_controller_readback_sha256", None),
        ("source_exact_return_telemetry_sha256", None),
        ("live_effect", "return_route_angular_envelope_gate_blocked"),
    ):
        _require(angular_evidence.get(field), expected, f"return-route evidence {field}")
    angular_sources = {
        "return_route": _sha256(
            root.parents[1]
            / "src/ur10e_experiment_runtime/ur10e_experiment_runtime/return_route.py"
        ),
        "tp_generator": _sha256(root / "tools/build_step5d_autotune_tp_v3.py"),
        "bridge_capture": _sha256(root / "tools/run_step5d_autotune_v3_bridge.py"),
        "campaign_adapter": _sha256(root / "tools/run_step5d_autotune_campaign.py"),
        "closure_collector": _sha256(root / "tools/step5d_autotune_runtime_lifecycle.py"),
    }
    angular_verifier_sources = {
        "ursim_return_gate": _sha256(
            root / "tools/run_step5d_autotune_v3_ursim_return_gate.py"
        ),
        "return_evidence_promoter": _sha256(
            root / "tools/promote_step5d_autotune_v3_return_route_evidence.py"
        ),
    }
    _require(angular_evidence.get("source_sha256"), angular_sources, "return-route evidence source")
    _require(
        angular_evidence.get("source_binding_sha256"),
        _canonical_sha256(angular_sources),
        "return-route evidence source binding",
    )
    _require(
        angular_evidence.get("verifier_provenance"),
        {
            "fingerprint": _canonical_sha256(angular_verifier_sources),
            "source_sha256": angular_verifier_sources,
        },
        "return-route verifier provenance",
    )
    _require(
        angular_evidence.get("local_triplet_sha256"),
        package_gate.get("local_triplet_sha256"),
        "return-route evidence triplet",
    )
    ursim_return_path = root / URSIM_RETURN_TRACE_RELATIVE
    ursim_return_sha = _sha256(ursim_return_path)
    _require(
        angular_evidence.get("motion_capable_ursim_trace_sha256"),
        ursim_return_sha,
        "return-route URSim trace binding",
    )
    retained_trace = _load_json(
        ursim_return_path,
        role="retained motion-capable URSim return trace",
    )
    _require(
        angular_evidence.get("motion_capable_ursim_trace_binding"),
        {
            "claim_role": "motion_capable_ursim_semantic_subject_equivalence",
            "legacy_source_binding_sha256": retained_trace.get(
                "source_binding_sha256"
            ),
            "active_evidence_source_binding_sha256": _canonical_sha256(
                angular_sources
            ),
            "motion_subject_fingerprint": return_builder.motion_subject_fingerprint(
                trace=retained_trace,
                triplet_sha256=dict(package_gate.get("local_triplet_sha256") or {}),
                policy=dict(angular_evidence.get("policy") or {}),
            ),
            "exact_triplet_match": True,
            "policy_markers_revalidated": True,
            "legacy_identity_provenance_only": retained_trace.get("identity"),
        },
        "return-route URSim semantic-subject equivalence",
    )
    _require(
        angular_evidence.get("missing_certification"),
        {
            "motion_capable_ursim_trace_sha256": ursim_return_sha,
            "attended_controller_readback_sha256": None,
            "source_exact_return_telemetry_sha256": None,
        },
        "return-route missing certification",
    )
    if not all((angular_evidence.get("offline_guards") or {}).values()):
        raise ReadinessError("return-route offline guard is incomplete")

    authorization = gates.get("authorization_separation") or {}
    expected_authorization = {
        "status": "pass_offline_contract",
        "certification_schema": (
            "ur-exp/step5d-certification-motion-authorization-v2"
        ),
        "campaign_schema": "ur-exp/step5d-campaign-authorization-v2",
        "interchangeable": False,
        "certification_no_contact_only": True,
        "certification_optimizer_eligible": False,
        "procedure_ticket_required_before_motion": True,
    }
    _require(authorization, expected_authorization, "authorization separation")

    advisory = gates.get("advisory_fable") or {}
    _require(
        advisory.get("status"),
        "completed_read_only_nonblocking_advisory",
        "Fable advisory status",
    )
    _require(advisory.get("requested_model"), "claude-fable-5", "Fable model request")
    _require(advisory.get("requested_effort"), "high", "Fable effort request")
    _require(advisory.get("role"), "read_only_nonblocking_advisory", "Fable role")
    _require(
        advisory.get("claim_boundary"),
        "advisory only; deterministic UR owner gates remain authoritative",
        "Fable claim boundary",
    )
    sol_audit = gates.get("sol_xhigh_audit") or {}
    _require(
        sol_audit,
        {
            "status": "parallel_advisory_nonblocking",
            "release_gate_applicable": False,
            "experiment_start_blocking": False,
            "safety_action_requires_deterministic_reproduction": True,
        },
        "Sol/xhigh audit policy",
    )

    return path, validation, timing_passed


def _verify_live_promotion(
    root: Path,
    *,
    current_identity: Mapping[str, Any],
    validation_path: Path,
    readback_relative: str,
    readback_sha256: str,
    expected_blockers: Sequence[str],
) -> None:
    promotion = _load_json(
        root / "config/step5/step5d_autotune_v3_live_promotion.json",
        role="V3 live promotion",
    )
    required = {
        "schema",
        "candidate_stage_id",
        "control_profile_id",
        "current_selector",
        "identity",
        "deterministic_validation",
        "controller_readback",
        "machine_campaign_binding",
        "same_process_startup_gate",
        "certification_motion_authorization_required",
        "campaign_authorization_required",
        "live_runtime_promoted",
        "blocker",
    }
    if set(promotion) != required:
        raise ReadinessError("V3 live promotion fields differ")
    for key, expected in (
        ("schema", "step5d.autotune-v3/live-promotion-v3"),
        ("candidate_stage_id", V3_STAGE_ID),
        ("control_profile_id", V1_STAGE_ID),
        ("current_selector", V3_STAGE_ID),
        ("identity", current_identity),
        ("machine_campaign_binding", MACHINE_BINDING),
        ("same_process_startup_gate", True),
        ("certification_motion_authorization_required", True),
        ("campaign_authorization_required", True),
        ("live_runtime_promoted", False),
        (
            "blocker",
            "_".join(expected_blockers),
        ),
    ):
        _require(promotion.get(key), expected, f"live promotion {key}")
    referenced_validation, _ = _reference(
        root, promotion.get("deterministic_validation"), role="deterministic validation"
    )
    _require(referenced_validation, validation_path, "live promotion validation path")
    referenced_readback, referenced_readback_sha = _reference(
        root, promotion.get("controller_readback"), role="controller readback"
    )
    _require(
        str(referenced_readback.relative_to(root)),
        readback_relative,
        "live promotion readback path",
    )
    _require(referenced_readback_sha, readback_sha256, "live promotion readback digest")


def verify(root: Path = ROOT) -> dict[str, Any]:
    """Audit the persisted offline/pre-live evidence bundle."""

    root = root.expanduser().resolve(strict=True)
    try:
        contract = load_contract(
            root / "config/step5/step5d_autotune_v3_control_contract.json"
        )
        current_identity = active_identity_snapshot(
            contract,
            experiment_root=root,
        )
    except (ContractViolation, StateError) as exc:
        raise ReadinessError(f"current source fingerprint failed: {exc}") from exc

    current = _load_json(root / "config/current_stage.json", role="current selector")
    _require(current.get("current_stage_id"), V3_STAGE_ID, "current selector")
    _require(current.get("program"), V3_STAGE_ID, "current program")

    table = _load_json(root / "config/step5_stage_table.json", role="stage table")
    v3 = _v3_row(table)
    for field, expected in (("active", True), ("blocked", True), ("bridge", True)):
        _require(v3.get(field), expected, f"v3 {field}")

    package = v3.get("package_delivery") or {}
    _require(package.get("status"), "controller_readback_verified", "package status")
    _require(package.get("controller_uploaded_by_v3"), True, "V3 upload claim")
    _require(package.get("controller_readback_verified"), True, "V3 readback claim")
    program = package.get("program_basename")
    local_triplet = package.get("local_triplet")
    if not isinstance(program, str) or not isinstance(local_triplet, str):
        raise ReadinessError("package identity is missing")
    triplet = package.get("sha256") or {}
    if set(triplet) != {".script", ".txt", ".urp"}:
        raise ReadinessError("package triplet digest set differs")
    for extension, expected_sha in triplet.items():
        if not isinstance(expected_sha, str) or _SHA256.fullmatch(expected_sha) is None:
            raise ReadinessError(f"package digest is invalid: {extension}")
        path = root / f"{local_triplet}{extension}"
        if path.is_symlink() or not path.is_file():
            raise ReadinessError(f"local package file is missing or unsafe: {extension}")
        _require(_sha256(path), expected_sha, f"local package digest {extension}")

    basis = root / f"{local_triplet}.deploy-manifest.json"
    _require(_sha256(basis), package.get("tp_fingerprint"), "TP basis fingerprint")
    readback_relative = package.get("controller_readback_manifest")
    readback_sha = package.get("controller_readback_manifest_sha256")
    if not isinstance(readback_relative, str) or not isinstance(readback_sha, str):
        raise ReadinessError("controller readback binding is missing")
    readback_path = root / readback_relative
    _require(_sha256(readback_path), readback_sha, "controller readback manifest digest")
    readback = _load_json(readback_path, role="controller readback manifest")
    _require(readback.get("verified"), True, "controller readback verification")
    _require(readback.get("program"), program, "controller readback program")
    _require(
        readback.get("tp_fingerprint"),
        contract["deployment_tp_identity"]["tp_fingerprint"],
        "controller TP fingerprint",
    )
    _require(readback.get("triplet_sha256"), contract["tp_artifact_sha256"], "controller triplet digests")
    _require(readback.get("triplet_sha256"), triplet, "current controller triplet")
    readback_at = _zoned_timestamp(
        readback.get("fresh_controller_checked_at"), role="readback timestamp"
    )
    _require(package.get("fresh_controller_sha_at"), readback_at, "fresh controller timestamp")

    validation_path, _validation, timing_passed = _verify_validation(
        root,
        current_identity=current_identity,
        readback_relative=readback_relative,
        readback_sha256=readback_sha,
    )
    offline = v3.get("offline_validation") or {}
    _require(offline.get("report"), str(validation_path.relative_to(root)), "validation report")
    _require(_sha256(validation_path), offline.get("report_sha256"), "validation report digest")

    readiness = v3.get("execution_readiness") or {}
    expected_blockers = [
        "requires_current_poweroff_controller_identity",
        "requires_certification_motion_authorization",
        "requires_certified_stopping_bound",
        "requires_certified_return_route_angular_envelope",
        "requires_fresh_campaign_authorization",
    ]
    public_success_signal = expected_blockers[0]
    for key, expected in (
        ("schema", "step5d.autotune-v3/execution-readiness-v3"),
        ("state", "pre_live_blocked"),
        (
            "public_success_signal",
            public_success_signal,
        ),
        ("deterministic_validation_complete", True),
        ("package_delivery_complete", True),
        ("candidate_current", True),
        ("live_runtime_promoted", False),
        ("same_process_startup_gate_complete", False),
        ("ready_to_execute", False),
        ("ready_to_load_play", False),
        ("ready_to_start_bridge", False),
        ("ready_to_arm", False),
        ("ready_for_contact_or_motion", False),
    ):
        _require(readiness.get(key), expected, f"readiness {key}")
    _require(readiness.get("blockers"), expected_blockers, "readiness blockers")
    trigger = readiness.get("operator_trigger") or {}
    for key, expected in (
        ("candidate_stage_id", V3_STAGE_ID),
        ("user_confirmation_required", True),
        ("certification_motion_authorization_required", True),
        ("campaign_authorization_required", True),
        ("internal_launch_binding", MACHINE_BINDING),
        ("tp_action", "controller_readback_verified_no_load_or_play"),
        ("play_effect", "forbidden_in_offline_tranche"),
    ):
        _require(trigger.get(key), expected, f"operator trigger {key}")

    _verify_live_promotion(
        root,
        current_identity=current_identity,
        validation_path=validation_path,
        readback_relative=readback_relative,
        readback_sha256=readback_sha,
        expected_blockers=expected_blockers,
    )
    return {
        "schema": "step5d.autotune-v3/execution-readiness-report-v3",
        "ok": True,
        "candidate_stage_id": V3_STAGE_ID,
        "current_stage_id": V3_STAGE_ID,
        "state": "pre_live_blocked",
        "public_success_signal": public_success_signal,
        "ready_to_execute": False,
        "package_delivery": "controller_readback_verified",
        "controller_readback_at": readback_at,
        "controller_target": package.get("controller_target"),
        "identity": current_identity,
        "next_owner": "ur10e-contact-control-prep",
        "next_legal_action": (
            "capture current controller identity; obtain a bounded "
            "certification-motion authorization for no-contact "
            "stopping/return measurement, close both evidence artifacts, and obtain a "
            "separate fresh campaign authorization; any Sol/xhigh audit runs in parallel "
            "as nonblocking advisory"
        ),
        "timing_diagnostic": (
            "pass_current_source_formal_500hz_timing"
            if timing_passed
            else "diagnostic_only_partial_lane_reuse_not_required_by_user"
        ),
        "canonical_gate": [
            "current_poweroff_controller_identity",
            "certification_motion_authorization",
            "certified_stopping_bound",
            "certified_return_route_angular_envelope",
            "fresh_campaign_authorization",
        ],
        "audit_policy": "parallel_advisory_nonblocking",
        "certification_motion_authorization_required": True,
        "campaign_authorization_required": True,
        "hil_hold_required": False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = verify(args.root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"step5d_v3_readiness={report['public_success_signal']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
