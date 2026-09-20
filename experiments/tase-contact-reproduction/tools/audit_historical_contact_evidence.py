#!/usr/bin/env python3
"""Build a compact audit from indexed contact evidence.

This reader intentionally consumes only the selected JSON reports and receipts;
it does not open robot raw logs, replay a controller, or access hardware.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = EXPERIMENT_ROOT.parents[1]
DEFAULT_OUTPUT = EXPERIMENT_ROOT / "report/historical-contact-mechanism-audit-v1/evidence.json"


def _load(root: Path, relative: str) -> tuple[dict[str, Any], str]:
    path = root / relative
    with path.open(encoding="utf-8") as handle:
        return json.load(handle), relative


def _iso_duration(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def _baseline_diag(error: str) -> dict[str, Any]:
    marker = "baseline_diag="
    if marker not in error:
        raise ValueError("live receipt does not contain baseline_diag")
    return json.loads(error.split(marker, 1)[1])


def _method_model_row(method: str, payload: dict[str, Any], source: str) -> dict[str, Any]:
    nominal = payload["nominal"]
    disturbed = payload["disturbed"]
    recovery = payload.get("recovery", {})
    assert nominal["claim_scope"] == "simulation only"
    assert disturbed["claim_scope"] == "simulation only"
    return {
        "id": f"{method.lower()}-training-model",
        "evidence_class": "simulation",
        "method": method,
        "source_paths": [source],
        "claim_scope": nominal["claim_scope"],
        "scenario_pair": {
            "nominal": {
                "force_peak_n": nominal["force_peak_n"],
                "force_mae_n": nominal["force_mae_n"],
                "progress_ratio": nominal["progress_ratio"],
                "saturation_ticks": nominal["saturation_ticks"],
            },
            "disturbed": {
                "force_peak_n": disturbed["force_peak_n"],
                "force_mae_n": disturbed["force_mae_n"],
                "progress_ratio": disturbed["progress_ratio"],
                "saturation_ticks": disturbed["saturation_ticks"],
            },
        },
        "recovery_s": recovery.get("recovery_s"),
        "tangent_drift_m": {
            "nominal_path_peak_m": nominal["path_peak_m"],
            "disturbed_path_peak_m": disturbed["path_peak_m"],
        },
        "missing_or_limited": [
            "simulation-only contact model; no physical force equivalence",
            "no physical recovery or controller-law ranking",
        ],
    }


def build_audit(root: Path = EXPERIMENT_ROOT) -> dict[str, Any]:
    target_dispatch, target_dispatch_path = _load(
        root, "runs/yield-sfc-full-20260920T151241Z/dispatch_receipt.json"
    )
    target_summary, target_summary_path = _load(
        root, "runs/yield-sfc-full-20260920T151241Z/summary.json"
    )
    target_supervisor, target_supervisor_path = _load(
        root, "runs/yield-sfc-full-20260920T151241Z/supervisor-result.json"
    )
    shear_result, shear_result_path = _load(
        root, "report/yield-live-transition-v1/scheduler-closeout-contact-result.json"
    )
    shear_walk, shear_walk_path = _load(
        root, "report/yield-live-transition-v1/qualification-force-walk.json"
    )
    replay, replay_path = _load(
        root, "report/contact-yield-recovery-20260920/open-loop-replay.json"
    )
    full_state_replay, full_state_replay_path = _load(
        root, "report/contact-yield-recovery-20260920/full-state-replay.json"
    )
    training, training_path = _load(
        root, "report/yield-fair-training-v1/initial-descriptors.json"
    )
    forceoff, forceoff_path = _load(
        root, "report/yield-normal-v2/forceoff-summary.json"
    )
    native_smoke, native_smoke_path = _load(
        root, "report/yield-fair-campaign-native-smoke-v1/result.json"
    )
    timing, timing_path = _load(
        root, "report/contact-yield-recovery-20260920/first-qualification-timing-151241.json"
    )
    offline_campaign, offline_campaign_path = _load(
        root, "report/contact-six-qp-20260917/offline-campaign.json"
    )
    holdout_schedule, holdout_schedule_path = _load(
        root, "report/contact-six-qp-20260917/holdout-schedule.json"
    )

    diag = _baseline_diag(target_dispatch["error"])
    shear_diag = _baseline_diag(shear_result["fault"])
    last_pause = target_dispatch["failure_state"]["last_pause"]
    recovery = target_supervisor["autonomous_home_recovery"]
    target_stop = target_dispatch["stop"]
    assert target_dispatch["executed"] is False
    assert target_dispatch["physical_qualification"] is False
    assert recovery["success"] is True
    assert recovery["relief_complete"] is True
    assert recovery["trial_stays_failed"] is True
    assert shear_result["active_state21_samples"] == 1119
    assert shear_result["qualification_passed"] is False
    assert shear_walk["evidence"]["xy_walk_m"] == shear_result["home_xy_offset_m"]
    assert replay["claim_scope"].startswith("recorded-input controller-law comparison")
    assert full_state_replay["claim_scope"].startswith("fresh-instance full-state forward replay")
    assert forceoff["live_executed"] is False
    assert native_smoke["engineering_only"] is True
    timing_failure = timing["live_stream_evidence"]["dispatch_failure"]
    published_tail = timing["live_stream_evidence"]["published_packets"]["tail"]
    published_control = next(packet for packet in published_tail if packet["command_mode"] == 1)
    published_stop = next(packet for packet in published_tail if packet["command_mode"] == 4)
    assert timing["finding"]["first_readiness_interval_s"] == 0.002
    assert timing["finding"]["first_interval_is_measured"] is False
    assert timing["finding"]["failed_tick_actual_dt_s"] is None
    assert timing["finding"]["failed_tick_actual_dt_lower_bound_s"] > timing["finding"]["four_ms_limit_s"]
    assert timing_failure["command_timeline_length"] == 0

    rows: list[dict[str, Any]] = [
        {
            "id": "r006-live-first-qualification-tick",
            "evidence_class": "real_physical_attempt",
            "method": target_dispatch["method"],
            "source_paths": [target_dispatch_path, target_supervisor_path, timing_path],
            "status": "first_pause_nominally_accepted; following_readiness_call_failed",
            "duration_s": None,
            "progress": {
                "preflight_summary_command_packets_sent": target_summary["command_packets_sent"],
                "dispatch_command_timeline_length": timing_failure["command_timeline_length"],
                "published_wire_packets": [
                    {
                        "sequence": published_control["sequence"],
                        "command_mode": published_control["command_mode"],
                        "host_monotonic_s": published_control["host_monotonic_s"],
                    },
                    {
                        "sequence": published_stop["sequence"],
                        "command_mode": published_stop["command_mode"],
                        "host_monotonic_s": published_stop["host_monotonic_s"],
                    },
                ],
                "path_started": False,
                "requested_path_duration_s": target_dispatch["live_path"]["path_duration_s"],
            },
            "timing": {
                "first_pause_nominal_dt_s": timing["finding"]["first_readiness_interval_s"],
                "first_pause_measured": timing["finding"]["first_interval_is_measured"],
                "next_call_actual_dt_s": timing["finding"]["failed_tick_actual_dt_s"],
                "next_call_actual_dt_lower_bound_s": timing["finding"]["failed_tick_actual_dt_lower_bound_s"],
                "robot_frame_receive_gap_s": timing["live_stream_evidence"]["robot_frames"]["host_receive_delta_s"],
                "interpretation": timing["finding"]["interpretation"],
            },
            "force": {
                "one_sample_filtered_normal_peak_n": diag["filtered_max_n"],
                "one_sample_force_norm_peak_n": diag["force_norm_max_n"],
                "readiness_samples": diag["readiness_samples"],
                "provenance": "source-reported baseline_diag from dispatch receipt; not recomputed from raw logs",
            },
            "tangent_drift_m": None,
            "recovery": {
                "stop_requested": target_stop["stop_requested"],
                "safety_mode": target_stop["safety_mode"],
                "autonomous_relief_success": recovery["relief_complete"],
                "autonomous_home_success": recovery["success"],
                "trial_stays_failed": recovery["trial_stays_failed"],
            },
            "memory_or_saturation": {
                "controller_state": "fresh startup; no PATH state",
                "startup_reason": last_pause["reason"],
                "observations": diag["samples"],
            },
            "missing_or_limited": [
                "no contact PATH force tracking",
                "no tangent drift or controller saturation metric",
                "exact failed-call actual_dt_s was not stored; only a proven lower bound is available",
                "dispatch command_timeline is empty but published_packets corroborate wire sends; it is not a zero-send record",
            ],
        },
        {
            "id": "r006-stopped-preflight-transport",
            "evidence_class": "real_transport_preflight",
            "method": "SFC",
            "source_paths": [target_summary_path],
            "status": "stopped_diagnostic_only",
            "capture_duration_s": _iso_duration(
                target_summary["started_at"], target_summary["completed_at"]
            ),
            "progress": {
                "preflight_summary_command_packets_sent": target_summary["command_packets_sent"],
                "load_play_sent": target_summary["load_play_sent"],
            },
            "force": {
                "raw_force_norm_max_n": target_summary["raw_force_norm_max_n"],
                "raw_torque_norm_max_nm": target_summary["raw_torque_norm_max_nm"],
                "provenance": "source-reported stopped preflight summary; separate capture window",
            },
            "transport": target_summary["rtde_receive_gap_s"],
            "tangent_drift_m": None,
            "missing_or_limited": [
                "separate stopped preflight window; cannot explain the later live first tick",
                "no command consumption or contact qualification",
            ],
        },
        {
            "id": "sfc-120659-baseline-shear",
            "evidence_class": "real_physical_contact",
            "method": "SFC",
            "source_paths": [shear_result_path, shear_walk_path],
            "status": "qualification_failed_after_active_contact",
            "duration_s": shear_result["active_state21_span_s"],
            "progress": {
                "active_state21_samples": shear_result["active_state21_samples"],
                "xy_walk_m": shear_result["home_xy_offset_m"],
            },
            "force": {
                "raw_force_norm_peak_n": shear_result["raw_force_norm_peak_n"],
                "corrected_force_norm_peak_n": shear_result["corrected_force_norm_peak_n"],
                "final_signed_normal_load_n": shear_walk["evidence"]["final_signed_normal_load_n"],
                "stopped_raw_force_norm_n": shear_result["stopped_raw_force_norm_n"],
                "provenance": {
                    "active_state21": "source-reported scheduler-closeout values",
                    "final_signed_normal_load_n": "source-reported final signed normal/compensated channel; not a raw norm peak",
                    "stopped_raw_force_norm_n": "source-reported stopped raw peak from a later stop window",
                },
            },
            "tangent_drift_m": shear_walk["evidence"]["xy_walk_m"],
            "recovery": {
                "protective_stop": shear_result["protective_stop"],
                "safety_mode": shear_result["dashboard_stop"]["safetymode"],
                "automatic_withdrawal": "gates failed; operator action pending",
            },
            "memory_or_saturation": {
                "readiness_samples": shear_diag["readiness_samples"],
                "active_state21_samples": shear_result["active_state21_samples"],
                "saturation_metric": None,
            },
            "mechanism_text": shear_walk["mechanism"],
            "missing_or_limited": [
                "no independent normal ground truth",
                "no DSFC/MSFC/LAC physical paired run in the selected index",
            ],
        },
        {
            "id": "sfc-recorded-input-open-loop-replay",
            "evidence_class": "open_loop_replay",
            "method": replay["method"],
            "source_paths": [replay_path],
            "source_run": replay["source_run"],
            "claim_scope": replay["claim_scope"],
            "initialization_assumption": replay["initialization_assumption"],
            "versions": replay["versions"],
            "metrics": {
                "parent": replay["parent"],
                "original_patch_1f4c44b4": replay["original_patch"],
                "current_candidate": replay["candidate_worktree"],
                "comparison": replay["comparison"],
            },
            "missing_or_limited": [
                "open loop; not a new physical force prediction",
                "fresh cold state per version; retained warm-state effect is not represented",
            ],
        },
        {
            "id": "full-state-forward-replay",
            "evidence_class": "replay",
            "source_paths": [full_state_replay_path],
            "claim_scope": full_state_replay["claim_scope"],
            "samples": full_state_replay["samples"],
            "passed": full_state_replay["passed"],
            "missing_or_limited": ["same implementation replay; not independent physics evidence"],
        },
        {
            "id": "dsfc-force-direction-model-ablation",
            "evidence_class": "synthetic_model_ablation",
            "method": forceoff["runs"][0]["metrics"]["method"],
            "source_paths": [forceoff_path],
            "claim_scope": forceoff["runs"][0]["metrics"]["claim_scope"],
            "dataset_role": forceoff["dataset_role"],
            "live_executed": forceoff["live_executed"],
            "model_role": "force-direction correction ablation; not a recorded sensor injection",
            "metrics": forceoff["runs"][0]["metrics"],
            "missing_or_limited": ["synthetic/model data; no physical force equivalence"],
        },
    ]

    for method in ("SFC", "DSFC", "MSFC"):
        rows.append(_method_model_row(method, training["methods"][method], training_path))

    rows.append(
        {
            "id": "native-sfc-development-smoke",
            "evidence_class": "simulation",
            "method": "SFC",
            "source_paths": [native_smoke_path],
            "claim_scope": native_smoke["members"]["nominal"]["recomputed_metrics"]["claim_scope"],
            "full_cycle": native_smoke["members"]["nominal"]["full_cycle"],
            "duration_s": native_smoke["files"]["nominal"]["elapsed_s"],
            "force_peak_n": native_smoke["members"]["nominal"]["recomputed_metrics"]["force_peak_n"],
            "progress_ratio": native_smoke["members"]["nominal"]["recomputed_metrics"]["progress_ratio"],
            "missing_or_limited": ["no hardware accessed; engineering smoke only"],
        }
    )

    method_coverage = {
        "SFC": {
            "real_physical_rows": ["r006-live-first-qualification-tick", "sfc-120659-baseline-shear"],
            "physical_status": "indexed real attempts exist",
            "note": "Real evidence exists, but the two rows fail at different phases and are not a paired law comparison.",
        },
        "DSFC": {
            "real_physical_rows": [],
            "offline_rows": ["dsfc-training-model", "dsfc-force-direction-model-ablation"],
            "physical_status": "not found in selected source scope",
            "searched_paths": [training_path, offline_campaign_path],
            "note": "Selected rows are simulation/model development only; this is not a claim that no older physical artifact exists elsewhere.",
        },
        "MSFC": {
            "real_physical_rows": [],
            "offline_rows": ["msfc-training-model"],
            "physical_status": "not found in selected source scope",
            "searched_paths": [training_path, offline_campaign_path],
            "note": "Selected rows are simulation/model development only; this is not a claim that no older physical artifact exists elsewhere.",
        },
        "LAC": {
            "real_physical_rows": [],
            "offline_rows": ["lac-offline-holdout"],
            "physical_status": "not found in selected source scope",
            "searched_paths": [offline_campaign_path, holdout_schedule_path],
            "offline_facts": {
                "claim_scope": offline_campaign["claim_scope"],
                "live_qualified": offline_campaign["live_qualified"],
                "hardware_qualified": offline_campaign["hardware_qualified"],
                "motion_authorized": offline_campaign["motion_authorized"],
                "completed_trials": offline_campaign["progress"]["LAC"]["completed_trials"],
                "failed_trials": offline_campaign["progress"]["LAC"]["failed_trials"],
                "holdout_trial_count": offline_campaign["holdout"]["LAC"]["trial_count"],
                "holdout_complete_count": offline_campaign["holdout"]["LAC"]["complete_count"],
                "schedule_rows": len(holdout_schedule["rows"]),
                "schedule_lac_rows": sum(row["controller"] == "LAC" for row in holdout_schedule["rows"]),
            },
            "note": "LAC is indexed here as offline synthetic replay/holdout only; no physical result is claimed.",
        },
    }

    return {
        "schema": "ur10e.historical-contact-mechanism-audit-v1",
        "claim_scope": "offline evidence audit; real, replay, synthetic/model, and simulation artifacts are separated",
        "source_policy": {
            "raw_logs_read": False,
            "hardware_accessed": False,
            "scheduling_or_controller_changes": False,
            "selected_real_runs": [
                "runs/yield-sfc-full-20260920T151241Z",
                "runs/yield-sfc-qualification-20260920T120659Z",
            ],
        },
        "evidence_table": rows,
        "method_coverage": method_coverage,
        "mechanism_assessment": {
            "supported": [
                {
                    "mechanism": "execution/readiness boundary",
                    "scope": "r006 only",
                    "evidence": "The first pause uses a nominal 2 ms interval without a measured prior timestamp; the following call has an 11.450823 ms actual lower bound, published mode-1/STOP packets, no PATH, and then recovers to stopped NORMAL/Home. It cannot identify a contact-law failure.",
                },
                {
                    "mechanism": "baseline SFC full residual admits tangent shear after contact",
                    "scope": "120659Z baseline",
                    "causal_status": "source-report hypothesis; the raw/compensated values support the coincident observation but do not isolate a counterfactual cause",
                    "evidence": "The indexed report records 2.444 mm XY walk, 4 N-class XY shear, 120 N/m path hold, 16.1545 N final signed normal/compensated load, 20.2460 N active corrected peak, and 25.4292 N later stopped raw peak.",
                },
            ],
            "not_established": [
                "A common estimator/constraint/execution mechanism across SFC, DSFC, MSFC, and LAC physical runs; comparable DSFC/MSFC/LAC physical rows were not found in the selected source scope.",
                "That the r006 first-tick failure was caused by the SFC law; it stopped before PATH and before command publication.",
                "A physical force ranking from the simulation, synthetic/model-ablation, or replay rows.",
            ],
            "shared_geometry_estimation": "Not isolated by the selected real rows; normal tilt, estimator error, task basis, geometry, and command admission remain possible contributors.",
            "control_law_novelty": "No new repair is introduced here. Baseline-only normal projection at 1f4c44b4 and the current zero-tangent qualification candidate already exist; this audit only proposes a discriminating comparison.",
            "decision": "The evidence supports one bounded SFC baseline-law candidate for recorded-input/offline comparison first, but comparative physical data are insufficient to design a novel controller or make a cross-method claim.",
        },
        "bounded_hypothesis": {
            "statement": "After contact, the baseline's full 3-D force residual can turn measured tangent shear into tangent command; normal-only residual projection during baseline should reduce XY walk and force escalation while leaving entry/PATH behavior unchanged.",
            "evidence_paths": [shear_result_path, shear_walk_path],
            "limitations": [
                "This is a candidate mechanism, not evidence that the current zero-tangent candidate is a safe contact hold.",
                "Normal tilt, estimator error, geometry, and command admission remain confounders.",
            ],
            "existing_repairs_acknowledged": [
                "1f4c44b4 already applies baseline-only outward-normal residual projection.",
                "The current candidate already suppresses baseline tangent command for qualification; that is an intervention to compare, not a new contribution from this audit.",
            ],
        },
            "paired_experiment": {
            "platform": "existing UR10e/Kunwei contact platform and already prepared SFC qualification package",
            "arms": [
                "parent baseline controller",
                "1f4c44b4 baseline normal-only residual projection",
            ],
            "stage": "recorded-input/offline comparison first",
            "known_risk": "The parent arm is the known shear/force-overload behavior from 120659Z; a live parent rerun would deliberately reintroduce that known failure mode.",
            "hold_constant": [
                "same Home/task basis and 5 N figure-eight recipe",
                "same entry/PATH, limits, cadence, and recovery policy",
                "one qualification attempt per arm with no limit or duration change",
            ],
            "measure": [
                "first-tick readiness pass/fail",
                "baseline state-21 duration and corrected/raw force peak",
                "XY tangent drift and stopped raw force",
                "phase reached and recovery outcome",
            ],
            "interpretation": {
                "readiness_fails_both": "execution/admission remains unresolved; do not rank controller laws",
                "readiness_passes_and_projection_reduces_drift": "supports the bounded baseline residual mechanism",
                "readiness_passes_but_drift_is_unchanged": "look next at estimator/geometry/constraint coupling before changing the law",
            },
            "future_physical_gate": "Only consider a physical comparison after an independent acceptable envelope is established and existing protection/recovery gates remain active; do not automatically rerun the known failed parent law.",
            "authorization": "offline design only; main owns any future hardware execution",
        },
        "synthetic_injected_force_status": {
            "status": "not independently indexed",
            "nearest_selected_artifact": offline_campaign_path,
            "classification": "offline-campaign is an explicit synthetic contact-plant proxy; forceoff is a simulation/model ablation, not a synthetic sensor-injection trace; no physical equivalence is claimed",
        },
        "historical_pointer_audit": {
            "requested_pointer": "Aug-20 100 physical trials / 78 accepted",
            "status": "not_found_in_selected_scope",
            "searched_paths": [
                "config/step5d_review_index_v2.json",
                "config/step5d_review_index_v3.json",
                "config/step5d_v29_imported_evidence_manifest.json",
                "config/step5d_v30_revisit_ledger.json",
                "config/step5/step5d_autotune_v3_attempt_ledger.json",
                "report/yield-training-report-real-v1/report.json",
                "report/yield-fair-training-v1/launcher-result.json",
                offline_campaign_path,
                holdout_schedule_path,
            ],
            "limitation": "This bounded audit does not scan other worktrees, raw logs, or unindexed archives; the requested historical denominator/acceptance pointer remains unresolved.",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    payload = build_audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "rows": len(payload["evidence_table"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
