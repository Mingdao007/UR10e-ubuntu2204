#!/usr/bin/env python3
"""Single canonical evaluator for Step5d v30 formal timing evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from step5d_v30_timing import (
    SCHEDULER_CONTRACT_FIFO20,
    SCHEDULER_CONTRACT_OTHER0,
    SOURCE_BINDING_FILES,
    summarize_preaggregated,
)


V3_RUNTIME_SOURCE_BINDING_FILES = {
    "identity_sha256": "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/identity.py",
    "moving_sphere_sha256": "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/moving_sphere.py",
    "physical_prior_sha256": "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/physical_prior.py",
    "stage_adapters_sha256": "../../src/ur10e_experiment_runtime/ur10e_experiment_runtime/stage_adapters.py",
}


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate_timing_raw(
    root: Path,
    raw_path: Path,
    *,
    scheduler_contract: str = SCHEDULER_CONTRACT_FIFO20,
) -> dict[str, Any]:
    """Evaluate one raw capture against current canonical source bindings."""

    root = root.resolve()
    raw_path = raw_path.resolve()
    raw = _load(raw_path)
    remote = _load(root / "config/step5d_v29_remote_evidence_sha256.json")
    evaluation = summarize_preaggregated(
        raw,
        expected_source_binding={
            field: _sha256(root / relative)
            for field, relative in SOURCE_BINDING_FILES.items()
        },
        expected_replay_sha256=remote["sha256"]["bridge_rtde_500hz.csv"],
        expected_paper_truth_sha256=_sha256(
            root / "config/step5d_liveprep_solver_gate.json"
        ),
        scheduler_contract=scheduler_contract,
    )
    accepted = evaluation.get("acceptance_eligible") is True
    try:
        recorded_raw_path = str(raw_path.relative_to(root))
    except ValueError:
        recorded_raw_path = str(raw_path)
    return {
        "schema_version": "step5d_timing_acceptance_evaluation_v1",
        "evaluator": "tools/step5d_timing_acceptance.py:evaluate_timing_raw",
        "raw_path": recorded_raw_path,
        "raw_sha256": _sha256(raw_path),
        "input_claim_class": "formal_raw_capture",
        "output_claim_class": "formal_acceptance" if accepted else "diagnostic_only",
        "accepted": accepted,
        "classification": evaluation.get("classification"),
        "blockers": evaluation.get("blockers", []),
        "evaluation": evaluation,
    }


def evaluate_step5d_v3_timing_raw(root: Path, raw_path: Path) -> dict[str, Any]:
    """Apply the stricter V3 sphere/full-tick release contract."""

    root = root.resolve()
    raw_path = raw_path.resolve()
    raw = _load(raw_path)
    base = evaluate_timing_raw(
        root,
        raw_path,
        scheduler_contract=SCHEDULER_CONTRACT_OTHER0,
    )
    blockers: list[str] = []
    base_evaluation = base.get("evaluation")
    if not isinstance(base_evaluation, dict) or base.get("accepted") is not True:
        blockers.append("base_formal_timing_not_accepted")
    deadline = (
        base_evaluation.get("deadline_robustness")
        if isinstance(base_evaluation, dict)
        else None
    )
    production_zero_miss = bool(
        isinstance(deadline, dict)
        and deadline.get("production_scheduler_zero_miss_pass") is True
    )
    bounded_last_command_hold = bool(
        isinstance(deadline, dict)
        and deadline.get("bounded_last_command_hold_pass") is True
        and deadline.get("bounded_last_command_hold_contract_proven") is True
    )
    if (
        not isinstance(deadline, dict)
        or deadline.get("scheduler_contract") != SCHEDULER_CONTRACT_OTHER0
        or not (production_zero_miss or bounded_last_command_hold)
    ):
        blockers.append("v3_requires_production_sched_other_timing_contract")

    tp_script = root / "programs/step5/step5d/step5d_strict_rnn_autotune_v3.script"
    tp_script_sha256 = _sha256(tp_script)
    artifact_binding = raw.get("artifact_binding")
    v3_tp_binding = (
        artifact_binding.get("v3_tp_script")
        if isinstance(artifact_binding, dict)
        else None
    )
    if (
        not isinstance(v3_tp_binding, dict)
        or v3_tp_binding.get("sha256") != tp_script_sha256
    ):
        blockers.append("v3_tp_watchdog_artifact_binding_mismatch")
    watchdog_markers = [
        line.strip()
        for line in tp_script.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("if stale_s2 > ")
    ]
    if watchdog_markers != ["if stale_s2 > 0.020:"]:
        blockers.append("v3_tp_watchdog_not_exact_20ms")
    if isinstance(artifact_binding, dict) and "stage_table" in artifact_binding:
        blockers.append("v3_legacy_mutable_stage_table_binding_present")

    import step5c_calibrated_kinematics_audit as kinematics

    expected_artifacts = {
        "profile_selection": root / "config/step5d_v30_profile_selection.json",
        "calibration_yaml": Path(kinematics.DEFAULT_CALIBRATION_YAML),
        "ur_xacro": Path(kinematics.DEFAULT_XACRO_PATH),
    }
    for label, path in expected_artifacts.items():
        binding = (
            artifact_binding.get(label)
            if isinstance(artifact_binding, dict)
            else None
        )
        if not isinstance(binding, dict) or binding.get("sha256") != _sha256(path):
            blockers.append(f"v3_runtime_artifact_mismatch:{label}")

    transport = raw.get("controller_stale_hold_fault_evidence")
    if not isinstance(transport, dict):
        blockers.append("v3_transport_hold_evidence_missing")
        transport = {}
    from kunwei_rtde_bridge import (
        STEP5D_AUTOTUNE_STAGE_ID,
        step5d_publish_guard_approved_late_command,
    )

    expected_transport = {
        "production_profile_id": STEP5D_AUTOTUNE_STAGE_ID,
        "transport_publish_action": "hold_last",
        "publish_guard_approved_late_command": False,
        "tp_watchdog_script_sha256": tp_script_sha256,
        "tp_watchdog_threshold_s": 0.020,
        "continuous_stale_stop_s": 0.020,
    }
    for field, expected in expected_transport.items():
        if transport.get(field) != expected:
            blockers.append(f"v3_transport_hold_mismatch:{field}")
    if step5d_publish_guard_approved_late_command(STEP5D_AUTOTUNE_STAGE_ID):
        blockers.append("v3_production_profile_allows_late_candidate")

    sphere = raw.get("step5d_v3_moving_sphere")
    if not isinstance(sphere, dict):
        blockers.append("v3_moving_sphere_timing_missing")
        sphere = {}
    if sphere.get("schema") != "step5d.autotune-v3/formal-moving-sphere-timing-v1":
        blockers.append("v3_moving_sphere_schema_mismatch")
    if sphere.get("enabled") is not True:
        blockers.append("v3_moving_sphere_not_enabled")

    runtime_source = (root.parents[1] / "src/ur10e_experiment_runtime").resolve()
    import sys

    if str(runtime_source) not in sys.path:
        sys.path.insert(0, str(runtime_source))
    from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR
    from ur10e_experiment_runtime.stage_adapters import (
        Stage25ControllerProgressAdapter,
    )

    expected_reference = Stage25ControllerProgressAdapter(
        physical_prior_sha256=STEP5D_V3_PHYSICAL_PRIOR.fingerprint
    ).reference_sha256
    if sphere.get("physical_prior_fingerprint") != STEP5D_V3_PHYSICAL_PRIOR.fingerprint:
        blockers.append("v3_physical_prior_fingerprint_mismatch")
    if sphere.get("reference_sha256") != expected_reference:
        blockers.append("v3_reference_fingerprint_mismatch")

    source_binding = sphere.get("source_binding")
    if not isinstance(source_binding, dict):
        blockers.append("v3_runtime_source_binding_missing")
        source_binding = {}
    for field, relative in V3_RUNTIME_SOURCE_BINDING_FILES.items():
        if source_binding.get(field) != _sha256(root / relative):
            blockers.append(f"v3_runtime_source_mismatch:{field}")

    validity_domain = sphere.get("fixture_stopping_bound_validity_domain")
    if validity_domain != (
        "formal_timing_fixture_only_not_live_stopping_bound_certification"
    ):
        blockers.append("v3_timing_fixture_validity_domain_mismatch")
    fixture_fingerprint = sphere.get("fixture_stopping_bound_fingerprint")
    if not isinstance(fixture_fingerprint, str) or len(fixture_fingerprint) != 64:
        blockers.append("v3_timing_fixture_fingerprint_invalid")

    warmup = sphere.get("warmup")
    if not isinstance(warmup, dict):
        blockers.append("v3_sphere_warmup_missing")
        warmup = {}
    expected_warmup = {
        "execute_samples": 1000,
        "execute_ok_count": 1000,
        "execute_unexpected_stop_count": 0,
        "safe_hold_samples": 100,
        "safe_hold_predicted_stop_count": 100,
        "safe_hold_exact_stop_transport_count": 100,
        "safe_hold_unexpected_stop_count": 0,
    }
    for field, expected in expected_warmup.items():
        if warmup.get(field) != expected:
            blockers.append(f"v3_sphere_warmup_mismatch:{field}")

    full_tick = sphere.get("full_tick")
    if not isinstance(full_tick, dict):
        blockers.append("v3_sphere_full_tick_missing")
        full_tick = {}
    expected_full_tick = {
        "samples": 30_000,
        "sphere_ok_count": 30_000,
        "unexpected_stop_count": 0,
    }
    for field, expected in expected_full_tick.items():
        if full_tick.get(field) != expected:
            blockers.append(f"v3_sphere_full_tick_mismatch:{field}")

    safe_hold = sphere.get("safe_hold")
    if not isinstance(safe_hold, dict):
        blockers.append("v3_sphere_safe_hold_missing")
        safe_hold = {}
    expected_safe_hold = {
        "samples": 30_000,
        "predicted_stop_count": 30_000,
        "exact_stop_transport_count": 30_000,
        "unexpected_stop_count": 0,
    }
    for field, expected in expected_safe_hold.items():
        if safe_hold.get(field) != expected:
            blockers.append(f"v3_sphere_safe_hold_mismatch:{field}")

    runtime_path = raw.get("runtime_path")
    if not isinstance(runtime_path, str) or (
        "apply_step5d_moving_sphere_guard->ExactStopTransport" not in runtime_path
    ):
        blockers.append("v3_runtime_path_missing_sphere_exact_stop")

    accepted = not blockers
    return {
        "schema_version": "step5d_v3_timing_acceptance_evaluation_v1",
        "evaluator": (
            "tools/step5d_timing_acceptance.py:"
            "evaluate_step5d_v3_timing_raw"
        ),
        "raw_path": base.get("raw_path"),
        "raw_sha256": base.get("raw_sha256"),
        "input_claim_class": "formal_raw_capture_step5d_v3",
        "output_claim_class": (
            "formal_acceptance_step5d_v3" if accepted else "diagnostic_only"
        ),
        "accepted": accepted,
        "classification": (
            (
                "production_sched_other_zero_observed_miss_v3_sphere_acceptance_eligible"
                if production_zero_miss
                else "production_sched_other_bounded_last_command_hold_v3_sphere_acceptance_eligible"
            )
            if accepted
            else "diagnostic_only_not_v3_acceptance"
        ),
        "blockers": sorted(set(blockers)),
        "base_evaluation": base,
        "v3_moving_sphere": sphere,
        "claim_boundary": (
            "formal source-bound timing under production SCHED_OTHER/0; acceptance "
            "requires either zero observed misses or the exact <=1% / <=10 consecutive "
            "last-command-hold contract with a 20 ms TP stop watchdog. This is not a "
            "hard-realtime scheduler claim, and the fixture bound does not certify "
            "physical stopping performance or authorize live use"
        ),
    }
