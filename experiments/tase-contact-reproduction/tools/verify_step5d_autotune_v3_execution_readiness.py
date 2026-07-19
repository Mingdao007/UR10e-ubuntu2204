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

from step5d_autotune_v3.profile import (
    ContractViolation,
    contract_sha256,
    control_fingerprint,
    load_contract,
)
from step5d_autotune_v3.state import StateError, orchestration_fingerprint
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
    "current_selector": V1_STAGE_ID,
    "v3_active": False,
    "robot_power_state": "POWER_OFF_AT_AUDIT_NOT_CURRENT_ASSERTION",
    "fresh_live_authorization_required": True,
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
READINESS_EVIDENCE_RELATIVE_PATHS = {
    SEAM_EVIDENCE_RELATIVE,
    FORMAL_RAW_RELATIVE,
    FORMAL_EVALUATION_RELATIVE,
    URSIM_RAW_RELATIVE,
    URSIM_RESULT_RELATIVE,
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
    "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/identity.py",
    "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/moving_sphere.py",
    "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/physical_prior.py",
    "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/stage_adapters.py",
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
    current_identity: Mapping[str, str],
    readback_relative: str,
    readback_sha256: str,
) -> tuple[Path, Mapping[str, Any]]:
    path = root / "config/step5/step5d_autotune_v3_offline_validation.json"
    validation = _load_json(path, role="deterministic validation")
    _require(
        validation.get("schema"),
        "step5d.autotune-v3/offline-acceptance-v3",
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
    _require(
        seam_timing.get("status"),
        "diagnostic_failed_host_schedule",
        "sphere seam timing",
    )
    _require(
        seam_timing.get("claim_class"),
        "diagnostic_only_not_formal_three_lane_timing",
        "sphere seam timing claim class",
    )
    _require(seam_timing.get("scheduler_policy"), "SCHED_OTHER", "timing scheduler")
    _require(seam_timing.get("scheduler_priority"), 0, "timing priority")
    _require(seam_timing.get("cpu_affinity"), [11, 13, 14, 15], "timing affinity")
    _require(seam_timing.get("samples"), 30_000, "sphere seam timing samples")
    _require(seam_timing.get("attempt_count"), 2, "sphere seam timing attempts")
    _require(seam_timing.get("compute_deadline_miss_count"), 0, "timing compute misses")
    _require(seam_timing.get("absolute_deadline_miss_count"), 2, "timing absolute misses")
    _require(
        seam_timing.get("attempt_absolute_deadline_miss_counts"),
        [1, 1],
        "timing per-attempt absolute misses",
    )
    _require(seam_timing.get("sphere_stop_count"), 0, "sphere seam stop count")
    _require(
        seam_timing.get("blocker"),
        "host_schedule_absolute_deadline_miss",
        "sphere seam timing blocker",
    )
    seam_path, _ = _reference(root, seam_timing.get("artifact"), role="sphere seam evidence")
    seam = _load_json(seam_path, role="sphere seam evidence")
    _require(seam.get("pass"), False, "sphere seam result")
    _require((seam.get("compute") or {}).get("samples"), 30_000, "sphere seam artifact samples")
    _require((seam.get("compute") or {}).get("deadline_miss_count"), 0, "sphere seam artifact compute misses")
    _require((seam.get("schedule") or {}).get("absolute_deadline_miss_count"), 1, "sphere seam artifact absolute misses")
    _require(seam.get("sphere_stop_count"), 0, "sphere seam artifact stops")
    source_binding = seam.get("source_sha256") or {}
    if not isinstance(source_binding, Mapping) or not source_binding:
        raise ReadinessError("sphere seam source binding is missing")
    for relative, expected_sha in source_binding.items():
        if not isinstance(relative, str) or not isinstance(expected_sha, str):
            raise ReadinessError("sphere seam source binding is invalid")
        _require(_sha256(_seam_source_path(root, relative)), expected_sha, f"sphere seam source {relative}")

    formal_timing = gates.get("formal_500hz_timing") or {}
    _require(
        formal_timing.get("status"),
        "blocked_current_production_fingerprint",
        "formal timing status",
    )
    _require(formal_timing.get("release_gate_satisfied"), False, "formal timing release gate")
    _require(formal_timing.get("attempt_count"), 2, "formal timing attempt count")
    _require(
        formal_timing.get("acceptance_classification"),
        "diagnostic_only_not_v3_acceptance",
        "formal timing classification",
    )
    raw_path, raw_sha = _reference(root, formal_timing.get("raw_artifact"), role="formal timing raw")
    evaluation_path, _ = _reference(root, formal_timing.get("evaluation_artifact"), role="formal timing evaluation")
    persisted_evaluation = _load_json(evaluation_path, role="formal timing evaluation")
    _require(persisted_evaluation.get("raw_sha256"), raw_sha, "formal timing raw binding")
    reevaluated = evaluate_step5d_v3_timing_raw(root, raw_path)
    for field, expected in (
        ("accepted", False),
        ("classification", formal_timing.get("acceptance_classification")),
        (
            "blockers",
            [
                "base_formal_timing_not_accepted",
                "v3_requires_production_sched_other_timing_contract",
            ],
        ),
        ("raw_sha256", raw_sha),
    ):
        _require(reevaluated.get(field), expected, f"formal timing reevaluation {field}")
    lanes = formal_timing.get("required_lanes") or {}
    expected_lanes = {
        "solver": (10_000, "pass"),
        "full_tick_with_sphere": (
            30_000,
            "fail_p99_and_schedule_robustness",
        ),
        "safe_hold": (30_000, "pass_bounded_hold_metrics"),
    }
    _require(set(lanes), set(expected_lanes), "formal timing lanes")
    for lane, (samples, status) in expected_lanes.items():
        _require(
            (lanes.get(lane) or {}).get("required_samples"),
            samples,
            f"formal timing {lane} samples",
        )
        _require(
            (lanes.get(lane) or {}).get("status"),
            status,
            f"formal timing {lane} status",
        )
    _require(
        formal_timing.get("blocker"),
        "current_source_full_tick_deadline_robustness_not_accepted",
        "formal timing blocker",
    )

    simulation = gates.get("simulation") or {}
    _require(simulation.get("status"), "pass_ursim_hold_current_fingerprint", "simulation status")
    _require(simulation.get("ursim"), "pass_current_fingerprint_hold", "URSim status")
    ursim_raw_path, ursim_raw_sha = _reference(root, simulation.get("raw_artifact"), role="URSim raw")
    ursim_result_path, _ = _reference(root, simulation.get("result_artifact"), role="URSim result")
    ursim_raw = _load_json(ursim_raw_path, role="URSim raw")
    ursim_result = _load_json(ursim_result_path, role="URSim result")
    _require(ursim_raw.get("ok"), True, "URSim raw result")
    _require(ursim_raw.get("forbidden_action_count"), 0, "URSim forbidden actions")
    _require((ursim_result.get("raw_evidence") or {}).get("sha256"), ursim_raw_sha, "URSim raw binding")
    _require(ursim_result.get("status"), "pass", "URSim promoted result")
    _require(ursim_result.get("identity"), current_identity, "URSim current identity")
    _require((ursim_result.get("hold") or {}).get("motion_count"), 0, "URSim motion exclusion")
    _require((ursim_result.get("hold") or {}).get("controller_write_count"), 0, "URSim write exclusion")
    power_off = gates.get("power_off_controller_audit") or {}
    _require(power_off.get("status"), "pass_read_only_boundary", "Power-OFF audit")
    _require(power_off.get("robot_mode"), "POWER_OFF", "Power-OFF robot mode")
    _require(power_off.get("program_state"), "STOPPED", "Power-OFF program state")
    _require(power_off.get("writes_performed"), False, "Power-OFF write exclusion")

    package_gate = gates.get("package_and_readback") or {}
    _require(package_gate.get("status"), "blocked", "package/readback status")
    _require(package_gate.get("blocker"), "requires_attended_tp_upload_readback", "package blocker")
    _require(package_gate.get("historical_controller_readback"), readback_relative, "historical readback path")
    _require(package_gate.get("historical_controller_readback_sha256"), readback_sha256, "historical readback digest")
    stopping = gates.get("stopping_bound") or {}
    _require(stopping.get("status"), "blocked", "stopping-bound status")
    _require(stopping.get("certified"), False, "stopping-bound certification")
    angular = gates.get("return_route_angular_envelope") or {}
    _require(angular.get("status"), "blocked", "return-route angular status")
    _require(angular.get("certified"), False, "return-route angular certification")

    return path, validation


def _verify_live_promotion(
    root: Path,
    *,
    current_identity: Mapping[str, str],
    validation_path: Path,
    readback_relative: str,
    readback_sha256: str,
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
        "user_authorization_required",
        "live_runtime_promoted",
        "blocker",
    }
    if set(promotion) != required:
        raise ReadinessError("V3 live promotion fields differ")
    for key, expected in (
        ("schema", "step5d.autotune-v3/live-promotion-v2"),
        ("candidate_stage_id", V3_STAGE_ID),
        ("control_profile_id", V1_STAGE_ID),
        ("current_selector", V1_STAGE_ID),
        ("identity", current_identity),
        ("machine_campaign_binding", MACHINE_BINDING),
        ("same_process_startup_gate", True),
        ("user_authorization_required", True),
        ("live_runtime_promoted", False),
        (
            "blocker",
            "requires_current_source_formal_500hz_timing_certified_stopping_bound_certified_return_route_angular_envelope_attended_tp_upload_readback_current_poweroff_identity_attended_sol_xhigh_audit_and_fresh_authorization",
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


def verify(root: Path = ROOT, *, require_live: bool = False) -> dict[str, Any]:
    """Verify direct-live readiness; ``require_live`` remains API-compatible."""

    root = root.expanduser().resolve(strict=True)
    try:
        contract = load_contract(
            root / "config/step5/step5d_autotune_v3_control_contract.json"
        )
        current_identity = {
            "contract_sha256": contract_sha256(contract),
            "control_fingerprint": control_fingerprint(contract),
            "orchestration_fingerprint": orchestration_fingerprint(root),
        }
    except (ContractViolation, StateError) as exc:
        raise ReadinessError(f"current source fingerprint failed: {exc}") from exc

    current = _load_json(root / "config/current_stage.json", role="current selector")
    _require(current.get("current_stage_id"), V1_STAGE_ID, "rollback selector")
    _require(current.get("program"), V1_STAGE_ID, "rollback program")

    table = _load_json(root / "config/step5_stage_table.json", role="stage table")
    v3 = _v3_row(table)
    for field, expected in (("active", False), ("blocked", True), ("bridge", False)):
        _require(v3.get(field), expected, f"v3 {field}")

    package = v3.get("package_delivery") or {}
    _require(package.get("status"), "requires_attended_tp_upload_readback", "package status")
    _require(package.get("controller_uploaded_by_v3"), False, "V3 upload claim")
    _require(package.get("controller_readback_verified"), False, "V3 readback claim")
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
    if readback.get("triplet_sha256") == triplet:
        raise ReadinessError("historical controller readback unexpectedly matches new local triplet")
    readback_at = _zoned_timestamp(
        readback.get("fresh_controller_checked_at"), role="readback timestamp"
    )
    _require(package.get("fresh_controller_sha_at"), readback_at, "fresh controller timestamp")

    validation_path, _validation = _verify_validation(
        root,
        current_identity=current_identity,
        readback_relative=readback_relative,
        readback_sha256=readback_sha,
    )
    offline = v3.get("offline_validation") or {}
    _require(offline.get("report"), str(validation_path.relative_to(root)), "validation report")
    _require(_sha256(validation_path), offline.get("report_sha256"), "validation report digest")

    readiness = v3.get("execution_readiness") or {}
    for key, expected in (
        ("schema", "step5d.autotune-v3/execution-readiness-v2"),
        ("state", "pre_live_blocked"),
        (
            "public_success_signal",
            "requires_current_source_formal_500hz_timing",
        ),
        ("deterministic_validation_complete", True),
        ("package_delivery_complete", False),
        ("live_runtime_promoted", False),
        ("same_process_startup_gate_complete", False),
        ("ready_to_execute", False),
        ("ready_to_start_bridge", False),
        ("ready_for_contact_or_motion", False),
    ):
        _require(readiness.get(key), expected, f"readiness {key}")
    trigger = readiness.get("operator_trigger") or {}
    for key, expected in (
        ("candidate_stage_id", V3_STAGE_ID),
        ("user_confirmation_required", True),
        ("user_authorization_required", True),
        ("internal_launch_binding", MACHINE_BINDING),
        ("tp_action", "attended_upload_readback_required"),
        ("play_effect", "forbidden_in_offline_tranche"),
    ):
        _require(trigger.get(key), expected, f"operator trigger {key}")

    _verify_live_promotion(
        root,
        current_identity=current_identity,
        validation_path=validation_path,
        readback_relative=readback_relative,
        readback_sha256=readback_sha,
    )
    if require_live:
        raise ReadinessError(
            "requires_current_source_formal_500hz_timing_certified_stopping_bound_certified_return_route_angular_envelope_attended_tp_upload_readback_current_poweroff_identity_attended_sol_xhigh_audit_and_fresh_authorization"
        )
    return {
        "schema": "step5d.autotune-v3/execution-readiness-report-v2",
        "ok": True,
        "candidate_stage_id": V3_STAGE_ID,
        "current_stage_id": V1_STAGE_ID,
        "state": "pre_live_blocked",
        "public_success_signal": "requires_current_source_formal_500hz_timing",
        "ready_to_execute": False,
        "package_delivery": "requires_attended_tp_upload_readback",
        "historical_controller_readback_at": readback_at,
        "controller_target": package.get("controller_target"),
        "identity": current_identity,
        "next_owner": "ur10e-contact-control-prep",
        "next_legal_action": "close current-source formal 500 Hz timing, certify the physical stopping bound and return-route angular envelope, then perform attended TP upload/readback, current Power-OFF identity checks, and an attended Sol/XHigh audit before fresh authorization",
        "timing_diagnostic": "blocked_current_source_full_tick_deadline_robustness",
        "canonical_gate": [
            "current_source_formal_500hz_timing",
            "certified_stopping_bound",
            "certified_return_route_angular_envelope",
            "attended_tp_upload_readback",
            "current_poweroff_controller_identity",
            "attended_sol_xhigh_pre_live_audit",
            "fresh_live_authorization",
        ],
        "user_authorization_required": True,
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
