#!/usr/bin/env python3
"""Replay a sealed rate460 TASE trace through frozen SFC+TASE commands.

This is a fixed-observation command replay. It validates the production RNN
solver/Jacobian path and reports command changes; it does not predict the
robot's counterfactual force response and never connects to a live device.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from contact_yield_live_writer import R006Candidate, _select_tase_solver_profile, native_motion_profile
from step5d_autotune_v4_r004.motion_profile import same_direction_qdot_rescale
from tase_contact_provider import (
    TASE_FORCE_PREEMPT_THRESHOLD_N,
    TaseContactProvider,
    current_model_binding,
    load_tase_outer_config,
)
from tase_sfc_tangent_augmentation import (
    TaseSfcTangentAugmentationRuntime,
    load_phase_force_template,
)


SCHEMA = "tase.sfc-tangent-production-command-replay-v1"
REPLACEMENT_METHOD_ID = "TASE_RNN_MATURE+SFC_TANGENT_REPLACE_OFFLINE_V1"
BIN_WIDTH_S = 0.1
FORMAL_START_S = 5.0
FORMAL_END_S = 60.0
TARGET_FORCE_N = 5.0
DEFAULT_SOURCE = Path(
    "runs/tase-resident-autotune-A-rate460-20260926T1413Z/confirmation-rate460-e-v1/"
    "session-01/attempts/0002"
)
DEFAULT_PARAMETER_FILE = Path(
    "runs/tase-resident-autotune-A-rate460-20260926T1413Z/confirmation-rate460-e-v1/"
    "candidates/session-01-02-A.json"
)


class ReplayError(RuntimeError):
    """The sealed trace cannot support an identity-bound command replay."""


class _ReplayProvider:
    reference_phase = "path"
    command_normal_base = (0.0, 0.0, 1.0)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _vector(row: Mapping[str, Any], name: str, width: int) -> np.ndarray:
    try:
        value = np.asarray(row[name], dtype=float)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ReplayError(f"source field {name} is invalid") from exc
    if value.shape != (width,) or not np.isfinite(value).all():
        raise ReplayError(f"source field {name} must be a finite width-{width} vector")
    return value


def _new_provider(root: Path, parameter_file: Path) -> TaseContactProvider:
    home = json.loads((root / "config/figure8_home_v1.json").read_text(encoding="utf-8"))
    pose = home.get("pose_m_rad")
    if not isinstance(pose, list) or len(pose) != 6:
        raise ReplayError("canonical Figure-eight Home pose is invalid")
    outer_config, parameter_binding = load_tase_outer_config(parameter_file)
    return TaseContactProvider(
        contract=current_model_binding(),
        candidate=R006Candidate(),
        motion_profile=native_motion_profile(),
        home_pose=pose,
        solver_profile=_select_tase_solver_profile(
            family="tase_sfc_tangent_augment", library="/tmp/not-used-by-rnn.so",
        ),
        outer_loop_config=outer_config,
        parameter_binding=parameter_binding,
        protocol_id="figure8_window60_r013_rate460_v1",
    )


def _apply_last_recorded_transition(runtime: Any, row: Mapping[str, Any]) -> bool:
    events = row.get("transition_events", ())
    if not isinstance(events, list):
        raise ReplayError("transition_events is not a list")
    state_event = None
    for event in events:
        if isinstance(event, Mapping) and "theta_dot_state" in event:
            state_event = event
    if state_event is None:
        return False
    runtime.solver.restore({
        "theta_dot_state": state_event["theta_dot_state"],
        "lambda_state": state_event["lambda_state"],
    })
    runtime._active_mode = "path"
    return True


def _recorded_force_preempt(row: Mapping[str, Any]) -> bool:
    return any(
        isinstance(event, Mapping)
        and event.get("event_type") == "force_preempt_warm_start"
        for event in row.get("transition_events", ())
    )


def _slew_limit(runtime_qdot: Any, row: Mapping[str, Any], previous_qdot: Any) -> tuple[np.ndarray, float]:
    dt_s = float(row["actual_dt_s"])
    delta_limit = float(row["host_slew_delta_limit_rad_s"])
    if not math.isfinite(dt_s) or dt_s <= 0.0 or not math.isfinite(delta_limit) or delta_limit <= 0.0:
        raise ReplayError("recorded host slew timing is invalid")
    ramp = same_direction_qdot_rescale(
        runtime_qdot,
        previous_qdot,
        dt_s=dt_s,
        max_slew_rad_s2=delta_limit / min(dt_s, 0.02),
    )
    return np.asarray(ramp.qdot, dtype=float), float(ramp.scale)


def _compose_replay_twist(
    *,
    composition: str,
    base_twist: np.ndarray,
    diagnostics: Mapping[str, Any],
    normal_value: Any,
    motion_profile: Any,
    overlay: TaseSfcTangentAugmentationRuntime,
) -> tuple[np.ndarray, np.ndarray, float]:
    requested_sfc = np.asarray(
        diagnostics["requested_sfc_velocity_base_m_s"], dtype=float,
    )
    if composition == "additive":
        applied_sfc = np.asarray(diagnostics["sfc_velocity_base_m_s"], dtype=float)
        return base_twist.copy() + np.concatenate((applied_sfc, np.zeros(3))), applied_sfc, float(
            diagnostics["sfc_velocity_scale_from_existing_caps"]
        )
    if composition != "replace_tase_tangent":
        raise ReplayError(f"unsupported replay composition {composition!r}")
    normal = np.asarray(normal_value, dtype=float)
    normal_norm = float(np.linalg.norm(normal))
    if normal.shape != (3,) or not np.isfinite(normal).all() or normal_norm <= 1e-12:
        raise ReplayError("TASE normal is invalid for replacement composition")
    normal /= normal_norm
    normal_linear = normal * float(np.dot(normal, base_twist[:3]))
    tangent_projector = np.eye(3) - np.outer(normal, normal)
    requested_sfc = tangent_projector @ requested_sfc
    tangent_scale = overlay._max_scale_inside_ball(
        np.zeros(3, dtype=float), requested_sfc,
        float(motion_profile.tangential_cap_m_s),
    )
    total_scale = overlay._max_scale_inside_ball(
        normal_linear, requested_sfc,
        float(motion_profile.total_linear_cap_m_s),
    )
    scale = min(tangent_scale, total_scale)
    applied_sfc = requested_sfc * scale
    result = base_twist.copy()
    result[:3] = normal_linear + applied_sfc
    return result, applied_sfc, scale


def _force_metrics(
    samples: list[tuple[float, float]], *, source_signal: str,
) -> dict[str, Any]:
    """Report sample and equal-weight 100 ms-bin metrics without mixing them."""
    result: dict[str, Any] = {}
    for name, start, end in (("full_0_60", 0.0, 60.0), ("formal_5_60", 5.0, 60.0)):
        selected = [(t, force) for t, force in samples if start <= t < end]
        if not selected:
            result[name] = {"sample_count": 0, "sample_mae_n": None, "bin_count": 0}
            continue
        errors = [force - TARGET_FORCE_N for _, force in selected]
        abs_errors = sorted(abs(value) for value in errors)
        sample_p95 = abs_errors[max(0, math.ceil(0.95 * len(abs_errors)) - 1)]
        expected_bins = int(round((end - start) / BIN_WIDTH_S))
        totals = [0.0] * expected_bins
        counts = [0] * expected_bins
        for stamp, force in selected:
            index = int(math.floor((stamp - start) / BIN_WIDTH_S + 1e-9))
            if 0 <= index < expected_bins:
                totals[index] += force
                counts[index] += 1
        bin_errors = [totals[i] / counts[i] - TARGET_FORCE_N for i in range(expected_bins) if counts[i]]
        abs_bin_errors = sorted(abs(value) for value in bin_errors)
        result[name] = {
            "sample_count": len(selected),
            "sample_mae_n": sum(abs(value) for value in errors) / len(errors),
            "sample_rmse_n": math.sqrt(sum(value * value for value in errors) / len(errors)),
            "sample_bias_n": sum(errors) / len(errors),
            "sample_abs_error_p95_n": sample_p95,
            "bin_width_s": BIN_WIDTH_S,
            "nonempty_bins": len(bin_errors),
            "required_bins": expected_bins,
            "complete_bin_coverage": len(bin_errors) == expected_bins,
            "bin_mean_mae_n": (
                sum(abs(value) for value in bin_errors) / len(bin_errors)
                if bin_errors else None
            ),
            "bin_mean_rmse_n": (
                math.sqrt(sum(value * value for value in bin_errors) / len(bin_errors))
                if bin_errors else None
            ),
            "bin_mean_bias_n": sum(bin_errors) / len(bin_errors) if bin_errors else None,
            "bin_mean_abs_error_p95_n": (
                abs_bin_errors[max(0, math.ceil(0.95 * len(abs_bin_errors)) - 1)]
                if abs_bin_errors else None
            ),
            "metric_source": f"Kunwei KWR75 {source_signal} from synchronized PATH command trace",
        }
    return result


def run_replay(
    *,
    root: Path,
    source_attempt: Path,
    parameter_file: Path,
    out_dir: Path,
    max_rows: int | None = None,
    composition: str = "additive",
    phase_template_file: Path | None = None,
) -> dict[str, Any]:
    timeline_path = source_attempt / "command_timeline.jsonl"
    if not timeline_path.is_file():
        raise ReplayError(f"command timeline is missing: {timeline_path}")
    if not parameter_file.is_file():
        raise ReplayError(f"parameter binding is missing: {parameter_file}")
    source_digest = _sha256_file(timeline_path)
    attempt_result_path = source_attempt / "attempt-result.json"
    if not attempt_result_path.is_file():
        raise ReplayError("source attempt receipt is missing")
    source_receipt = json.loads(attempt_result_path.read_text(encoding="utf-8"))
    source_evidence = source_receipt.get("evidence", {})
    source_lifecycle = source_receipt.get("lifecycle", {})
    source_metrics = source_evidence.get("metrics", {}) if isinstance(source_evidence, Mapping) else {}
    parameter_document = json.loads(parameter_file.read_text(encoding="utf-8"))
    if source_receipt.get("evidence_eligible") is not True or source_lifecycle.get("path_complete") is not True:
        raise ReplayError("source attempt is not an eligible completed PATH receipt")
    if source_metrics.get("protocol_id") != "figure8_window60_r013_rate460_v1":
        raise ReplayError("source attempt protocol is not rate460")
    if source_metrics.get("complete_bins") != 550 or source_metrics.get("complete") is not True:
        raise ReplayError("source attempt receipt does not prove the complete formal 550-bin PATH")
    if parameter_document.get("protocol_id") != source_metrics.get("protocol_id"):
        raise ReplayError("candidate parameter binding does not match the source protocol")
    robot_frames_path = source_attempt / "robot_frames.jsonl"
    sensor_trace_path = source_attempt / "raw_sensor.jsonl"
    try:
        with robot_frames_path.open("r", encoding="utf-8") as stream:
            robot_frame_first = json.loads(next(line for line in stream if line.strip()))
        with sensor_trace_path.open("r", encoding="utf-8") as stream:
            sensor_first = json.loads(next(line for line in stream if line.strip()))
    except (OSError, StopIteration, json.JSONDecodeError) as exc:
        raise ReplayError("source RTDE/Kunwei raw evidence is missing or malformed") from exc
    if "actual_TCP_force" in robot_frame_first:
        raise ReplayError("source RTDE robot frame contains UR internal force; refuse mixed-source replay")
    if "corrected_wrench_n_nm" not in sensor_first:
        raise ReplayError("source sensor stream does not identify corrected Kunwei wrench")

    baseline_provider = _new_provider(root, parameter_file)
    candidate_provider = _new_provider(root, parameter_file)
    baseline_runtime = baseline_provider.runtime
    candidate_base_runtime = candidate_provider.runtime
    phase_force_template = None
    phase_template_binding = None
    if phase_template_file is not None:
        if composition != "additive":
            raise ReplayError("phase-template replay currently supports additive composition only")
        phase_force_template, phase_template_binding = load_phase_force_template(
            Path(phase_template_file).resolve(strict=True),
            allow_leave_one_out=True,
        )
    candidate_overlay = TaseSfcTangentAugmentationRuntime(
        candidate_base_runtime,
        _ReplayProvider(),
        root=root,
        phase_force_template_base_n=phase_force_template,
        phase_template_binding=phase_template_binding,
    )
    # Bind the actual TASE command normal from the source row at each call.
    replay_provider = candidate_overlay._provider

    out_dir.mkdir(parents=True, exist_ok=False)
    trace_path = out_dir / "candidate_command_trace.jsonl.gz"
    counters = {
        "path_rows": 0,
        "source_transition_events": 0,
        "source_warm_start_rows": 0,
        "source_direction_retry_rows": 0,
        "candidate_direction_retry_rows": 0,
        "candidate_vs_source_retry_decision_disagreements": 0,
        "baseline_rows_with_qdot_error_over_1e-9": 0,
        "jacobian_rows_with_error_over_1e-9": 0,
        "candidate_joint_bound_violations": 0,
        "candidate_slew_limited_rows": 0,
        "candidate_sfc_cap_limited_rows": 0,
        "sfc_reference_alignment_valid_rows": 0,
        "sfc_reference_direction_positive_rows": 0,
        "sfc_reference_direction_negative_rows": 0,
        "candidate_task_reference_alignment_valid_rows": 0,
        "candidate_task_reference_direction_positive_rows": 0,
        "candidate_task_reference_direction_negative_rows": 0,
    }
    maxima = {
        "baseline_vs_source_published_qdot_abs_rad_s": 0.0,
        "source_model_jacobian_abs": 0.0,
        "sfc_tangent_force_n": 0.0,
        "candidate_joint_qdot_abs_rad_s": 0.0,
        "sfc_requested_tangent_speed_m_s": 0.0,
        "sfc_applied_tangent_speed_m_s": 0.0,
        "candidate_vs_baseline_tcp_linear_delta_m_s": 0.0,
        "candidate_vs_baseline_normal_delta_m_s": 0.0,
        "candidate_vs_baseline_tangent_delta_m_s": 0.0,
        "candidate_vs_baseline_angular_delta_rad_s": 0.0,
        "candidate_qdot_delta_rad_s": 0.0,
        "sfc_reference_alignment_cosine": 0.0,
        "candidate_task_reference_alignment_cosine": 0.0,
    }
    sums = {
        "sfc_tangent_force_n": 0.0,
        "sfc_requested_tangent_speed_m_s": 0.0,
        "sfc_applied_tangent_speed_m_s": 0.0,
        "candidate_vs_baseline_tcp_linear_delta_m_s": 0.0,
        "candidate_vs_baseline_normal_delta_abs_m_s": 0.0,
        "candidate_vs_baseline_tangent_delta_m_s": 0.0,
        "candidate_qdot_delta_rad_s": 0.0,
        "sfc_reference_alignment_cosine": 0.0,
        "candidate_task_reference_alignment_cosine": 0.0,
    }
    source_first_path_time_s = None
    source_last_path_time_s = None
    source_bins: set[int] = set()
    force_samples = {"normal_force_measured_n": [], "normal_force_filtered_n": []}
    previous_candidate_qdot: np.ndarray | None = None
    normal_basis = np.asarray((0.0, 0.0, 1.0), dtype=float)

    try:
        with timeline_path.open("r", encoding="utf-8") as source, gzip.open(
            trace_path, "wt", encoding="utf-8", compresslevel=6,
        ) as target:
            for line in source:
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("reference_phase") != "path":
                    continue
                if max_rows is not None and counters["path_rows"] >= max_rows:
                    break
                if row.get("sfc_replay_inputs") is None:
                    raise ReplayError("PATH row is missing synchronized Kunwei/TCP inputs")
                inputs = row["sfc_replay_inputs"]
                source_events = row.get("transition_events", ())
                if not isinstance(source_events, list):
                    raise ReplayError("PATH transition_events is invalid")
                counters["source_transition_events"] += len(source_events)
                counters["source_warm_start_rows"] += int(_recorded_force_preempt(row))
                counters["source_direction_retry_rows"] += int(bool(row.get("force_preempt_direction_retry")))

                base_twist = _vector(row, "requested_outer_twist_m_s_rad_s", 6)
                actual_q = _vector(row, "actual_q_rad", 6)
                actual_qd = _vector(row, "actual_qd_rad_s", 6)
                pose = _vector(inputs, "actual_tcp_pose_m_rad", 6)
                wrench = _vector(inputs, "control_wrench_tcp_n_nm", 6)
                jacobian_logged = np.asarray(row.get("jacobian_6x6"), dtype=float)
                if jacobian_logged.shape != (6, 6) or not np.isfinite(jacobian_logged).all():
                    raise ReplayError("logged calibrated Jacobian is invalid")
                dt_s = float(row["actual_dt_s"])
                path_time_s = float(row["reference_time_s"])
                force_norm_n = float(row["measured_force_norm_n"])
                if not all(math.isfinite(value) for value in (dt_s, path_time_s, force_norm_n)):
                    raise ReplayError("PATH timing or Kunwei force norm is non-finite")
                if source_first_path_time_s is None:
                    source_first_path_time_s = path_time_s
                    source_initial_qdot = _vector(row, "previous_published_qdot_rad_s", 6)
                    previous_candidate_qdot = source_initial_qdot.copy()
                source_last_path_time_s = path_time_s
                for name in force_samples:
                    value = float(row[name])
                    if not math.isfinite(value):
                        raise ReplayError(f"source force {name} is non-finite")
                    if 0.0 <= path_time_s < 60.0:
                        force_samples[name].append((path_time_s, value))
                    if FORMAL_START_S <= path_time_s < FORMAL_END_S:
                        source_bins.add(int(math.floor(
                            (path_time_s - FORMAL_START_S) / BIN_WIDTH_S + 1e-9
                        )))

                # Baseline replay restores only observed state transitions,
                # then runs the exact production calibrated RNN/Jacobian and
                # the recorded provider slew constraint.
                _apply_last_recorded_transition(baseline_runtime, row)
                baseline_command = baseline_runtime.command(
                    actual_q=actual_q,
                    actual_qd=actual_qd,
                    actual_tcp_pose=pose,
                    desired_twist=base_twist,
                    actual_dt_s=dt_s,
                    mode="path",
                    path_time_s=path_time_s,
                )
                baseline_qdot, _baseline_slew_scale = _slew_limit(
                    baseline_command.qdot,
                    row,
                    _vector(row, "previous_published_qdot_rad_s", 6),
                )
                source_qdot = _vector(row, "published_packet_qdot_rad_s", 6)
                baseline_qdot_error = float(np.max(np.abs(baseline_qdot - source_qdot)))
                maxima["baseline_vs_source_published_qdot_abs_rad_s"] = max(
                    maxima["baseline_vs_source_published_qdot_abs_rad_s"], baseline_qdot_error,
                )
                counters["baseline_rows_with_qdot_error_over_1e-9"] += int(
                    baseline_qdot_error > 1e-9
                )
                jacobian_error = float(np.max(np.abs(np.asarray(baseline_command.jacobian_6x6) - jacobian_logged)))
                maxima["source_model_jacobian_abs"] = max(
                    maxima["source_model_jacobian_abs"], jacobian_error,
                )
                counters["jacobian_rows_with_error_over_1e-9"] += int(jacobian_error > 1e-9)

                # The source force and pose are the baseline-corrected
                # external Kunwei KWR75 signal and synchronized RTDE pose.
                replay_provider.command_normal_base = tuple(
                    float(value) for value in _vector(inputs, "control_reaction_normal_base", 3)
                )
                candidate_overlay.augment_twist(
                    base_twist,
                    phase="path",
                    mode="path",
                    actual_tcp_pose=pose,
                    force_tcp_n=wrench[:3],
                    actual_dt_s=dt_s,
                    path_time_s=path_time_s,
                )
                sfc_diagnostics = candidate_overlay._last_diagnostics
                candidate_twist, used_sfc_velocity, used_sfc_scale = _compose_replay_twist(
                    composition=composition,
                    base_twist=base_twist,
                    diagnostics=sfc_diagnostics,
                    normal_value=inputs["control_reaction_normal_base"],
                    motion_profile=candidate_base_runtime.motion_profile,
                    overlay=candidate_overlay,
                )

                # Keep the source force-threshold warm-start events fixed for
                # a same-input comparison. Re-evaluate the direction retry
                # against the candidate's own preliminary J*qdot.
                if not any(
                    isinstance(event, Mapping) and event.get("event_type") == "path_origin"
                    for event in source_events
                ) and counters["path_rows"] == 0:
                    raise ReplayError("source PATH origin state is missing")
                candidate_events = [
                    event for event in source_events
                    if isinstance(event, Mapping) and event.get("event_type") == "path_origin"
                ]
                if candidate_events:
                    event = candidate_events[-1]
                    candidate_base_runtime.solver.restore({
                        "theta_dot_state": event["theta_dot_state"],
                        "lambda_state": event["lambda_state"],
                    })
                    candidate_base_runtime._active_mode = "path"
                if _recorded_force_preempt(row):
                    candidate_base_runtime.warm_start_for_twist(
                        actual_q=actual_q, desired_twist=candidate_twist, mode="path",
                    )
                candidate_command = candidate_base_runtime.command(
                    actual_q=actual_q,
                    actual_qd=actual_qd,
                    actual_tcp_pose=pose,
                    desired_twist=candidate_twist,
                    actual_dt_s=dt_s,
                    mode="path",
                    path_time_s=path_time_s,
                )
                candidate_linearized = np.asarray(candidate_command.jacobian_6x6) @ np.asarray(
                    candidate_command.qdot, dtype=float,
                )
                candidate_retry = bool(
                    force_norm_n >= TASE_FORCE_PREEMPT_THRESHOLD_N
                    and -float(candidate_linearized[2]) > 0.0
                )
                if candidate_retry:
                    candidate_base_runtime.warm_start_for_twist(
                        actual_q=actual_q, desired_twist=candidate_twist, mode="path",
                    )
                    candidate_command = candidate_base_runtime.command(
                        actual_q=actual_q,
                        actual_qd=actual_qd,
                        actual_tcp_pose=pose,
                        desired_twist=candidate_twist,
                        actual_dt_s=dt_s,
                        mode="path",
                        path_time_s=path_time_s,
                    )
                    candidate_linearized = np.asarray(candidate_command.jacobian_6x6) @ np.asarray(
                        candidate_command.qdot, dtype=float,
                    )
                candidate_unlimited_qdot = np.asarray(candidate_command.qdot, dtype=float)
                candidate_qdot, candidate_slew_scale = _slew_limit(
                    candidate_unlimited_qdot,
                    row,
                    previous_candidate_qdot,
                )
                previous_candidate_qdot = candidate_qdot.copy()
                normal_basis = np.asarray(inputs["control_reaction_normal_base"], dtype=float)
                normal_basis /= np.linalg.norm(normal_basis)
                tangent_projector = np.eye(3) - np.outer(normal_basis, normal_basis)
                candidate_task = np.asarray(candidate_command.jacobian_6x6) @ candidate_qdot
                baseline_task = np.asarray(baseline_command.jacobian_6x6) @ baseline_qdot
                diagnostics = sfc_diagnostics
                tangent_force_norm = float(np.linalg.norm(diagnostics["tangent_force_base_n"]))
                requested_sfc_velocity = np.asarray(
                    diagnostics["requested_sfc_velocity_base_m_s"], dtype=float,
                )
                sfc_velocity = used_sfc_velocity
                applied_sfc = float(np.linalg.norm(sfc_velocity))
                sfc_was_scaled = used_sfc_scale < 1.0 - 1e-12
                reference_tangent = tangent_projector @ _vector(
                    inputs, "reference_velocity_base_m_s", 3,
                )
                reference_speed = float(np.linalg.norm(reference_tangent))
                sfc_speed = float(np.linalg.norm(sfc_velocity))
                candidate_task_tangent = tangent_projector @ candidate_task[:3]
                candidate_task_tangent_speed = float(np.linalg.norm(candidate_task_tangent))
                sfc_reference_cosine = None
                candidate_reference_cosine = None
                if reference_speed > 1e-12 and sfc_speed > 1e-12:
                    sfc_reference_cosine = float(
                        np.dot(sfc_velocity, reference_tangent) / (sfc_speed * reference_speed)
                    )
                    counters["sfc_reference_alignment_valid_rows"] += 1
                    counters["sfc_reference_direction_positive_rows"] += int(sfc_reference_cosine >= 0.0)
                    counters["sfc_reference_direction_negative_rows"] += int(sfc_reference_cosine < 0.0)
                    sums["sfc_reference_alignment_cosine"] += sfc_reference_cosine
                if reference_speed > 1e-12 and candidate_task_tangent_speed > 1e-12:
                    candidate_reference_cosine = float(
                        np.dot(candidate_task_tangent, reference_tangent)
                        / (candidate_task_tangent_speed * reference_speed)
                    )
                    counters["candidate_task_reference_alignment_valid_rows"] += 1
                    counters["candidate_task_reference_direction_positive_rows"] += int(
                        candidate_reference_cosine >= 0.0
                    )
                    counters["candidate_task_reference_direction_negative_rows"] += int(
                        candidate_reference_cosine < 0.0
                    )
                    sums["candidate_task_reference_alignment_cosine"] += candidate_reference_cosine
                task_delta = candidate_task - baseline_task
                task_linear_delta = float(np.linalg.norm(task_delta[:3]))
                normal_delta = float(np.dot(normal_basis, task_delta[:3]))
                tangent_delta = float(np.linalg.norm(tangent_projector @ task_delta[:3]))
                angular_delta = float(np.linalg.norm(task_delta[3:]))
                qdot_delta = float(np.linalg.norm(candidate_qdot - baseline_qdot))
                lower = _vector(row, "solver_qdot_lower_rad_s", 6)
                upper = _vector(row, "solver_qdot_upper_rad_s", 6)
                joint_violation = bool(
                    np.any(candidate_unlimited_qdot < lower - 1e-12)
                    or np.any(candidate_unlimited_qdot > upper + 1e-12)
                )

                counters["path_rows"] += 1
                counters["candidate_direction_retry_rows"] += int(candidate_retry)
                counters["candidate_vs_source_retry_decision_disagreements"] += int(
                    candidate_retry != bool(row.get("force_preempt_direction_retry"))
                )
                counters["candidate_joint_bound_violations"] += int(joint_violation)
                counters["candidate_slew_limited_rows"] += int(candidate_slew_scale < 1.0 - 1e-12)
                counters["candidate_sfc_cap_limited_rows"] += int(sfc_was_scaled)
                for key, value in (
                    ("sfc_tangent_force_n", tangent_force_norm),
                    ("candidate_joint_qdot_abs_rad_s", float(np.max(np.abs(candidate_qdot)))),
                    ("sfc_requested_tangent_speed_m_s", float(np.linalg.norm(requested_sfc_velocity))),
                    ("sfc_applied_tangent_speed_m_s", applied_sfc),
                    ("candidate_vs_baseline_tcp_linear_delta_m_s", task_linear_delta),
                    ("candidate_vs_baseline_normal_delta_m_s", abs(normal_delta)),
                    ("candidate_vs_baseline_tangent_delta_m_s", tangent_delta),
                    ("candidate_vs_baseline_angular_delta_rad_s", angular_delta),
                    ("candidate_qdot_delta_rad_s", qdot_delta),
                ):
                    maxima[key] = max(maxima[key], value)
                for key, value in (
                    ("sfc_tangent_force_n", tangent_force_norm),
                    ("sfc_requested_tangent_speed_m_s", float(np.linalg.norm(requested_sfc_velocity))),
                    ("sfc_applied_tangent_speed_m_s", applied_sfc),
                    ("candidate_vs_baseline_tcp_linear_delta_m_s", task_linear_delta),
                    ("candidate_vs_baseline_normal_delta_abs_m_s", abs(normal_delta)),
                    ("candidate_vs_baseline_tangent_delta_m_s", tangent_delta),
                    ("candidate_qdot_delta_rad_s", qdot_delta),
                ):
                    sums[key] += value

                trace_row = {
                    "composition": composition,
                    "reference_time_s": path_time_s,
                    "actual_dt_s": dt_s,
                    "force_source": "baseline-corrected Kunwei KWR75 control_wrench_tcp_n_nm",
                    "force_tcp_n": wrench[:3].tolist(),
                    "base_tase_twist_m_s_rad_s": base_twist.tolist(),
                    "candidate_twist_m_s_rad_s": candidate_twist.tolist(),
                    "requested_sfc_tangent_velocity_base_m_s": requested_sfc_velocity.tolist(),
                    "sfc_tangent_velocity_base_m_s": sfc_velocity.tolist(),
                    "sfc_cap_scale": used_sfc_scale,
                    "rnn_baseline_qdot_rad_s": baseline_qdot.tolist(),
                    "rnn_candidate_qdot_rad_s": candidate_qdot.tolist(),
                    "source_published_qdot_rad_s": source_qdot.tolist(),
                    "baseline_qdot_abs_error_rad_s": baseline_qdot_error,
                    "candidate_vs_baseline_jqdot_m_s_rad_s": task_delta.tolist(),
                    "candidate_direction_retry": candidate_retry,
                    "source_direction_retry": bool(row.get("force_preempt_direction_retry")),
                    "candidate_slew_scale": candidate_slew_scale,
                    "reference_tangent_velocity_base_m_s": reference_tangent.tolist(),
                    "sfc_reference_alignment_cosine": sfc_reference_cosine,
                    "candidate_task_reference_alignment_cosine": candidate_reference_cosine,
                }
                target.write(json.dumps(trace_row, sort_keys=True, allow_nan=False) + "\n")
    finally:
        for provider in (baseline_provider, candidate_provider):
            solver = getattr(provider.runtime, "solver", None)
            close = getattr(solver, "close", None)
            if callable(close):
                close()

    count = counters["path_rows"]
    if count == 0:
        raise ReplayError("no formal PATH rows found")
    source_path_complete = bool(
        max_rows is None
        and len(source_bins) == 550
        and source_first_path_time_s is not None
        and source_first_path_time_s <= 0.002
        and source_last_path_time_s is not None
        and source_last_path_time_s >= 60.0 - 0.1
    )
    if max_rows is None and not source_path_complete:
        raise ReplayError(
            f"full replay source coverage is incomplete: {len(source_bins)}/550 bins, "
            f"range={source_first_path_time_s}..{source_last_path_time_s}"
        )
    baseline_matches_source = bool(
        counters["baseline_rows_with_qdot_error_over_1e-9"] == 0
        and counters["jacobian_rows_with_error_over_1e-9"] == 0
    )
    report = {
        "schema": SCHEMA,
        "method_id": (
            candidate_overlay.composition_binding["method_id"]
            if composition == "additive" else REPLACEMENT_METHOD_ID
        ),
        "composition": composition,
        "source_attempt": str(source_attempt.resolve()),
        "source_timeline_sha256": source_digest,
        "parameter_file": str(parameter_file.resolve()),
        "row_scope": "complete sealed PATH" if source_path_complete else "diagnostic PATH prefix",
        "path_rows": count,
        "reference_time_s": [source_first_path_time_s, source_last_path_time_s],
        "source_coverage": {
            "formal_bins": len(source_bins),
            "required_formal_bins": 550,
            "receipt_path_complete": source_lifecycle.get("path_complete") is True,
            "receipt_formal_mae_n": source_evidence.get("mae_n"),
            "receipt_effective_rate_hz": source_evidence.get("effective_rate_hz"),
            "trace_reconstruction_force_metrics": {
                "measured_Kunwei_normal": _force_metrics(
                    force_samples["normal_force_measured_n"],
                    source_signal="normal_force_measured_n",
                ),
                "filtered_Kunwei_normal": _force_metrics(
                    force_samples["normal_force_filtered_n"],
                    source_signal="normal_force_filtered_n",
                ),
            },
        },
        "source_protocol": "figure8_window60_r013_rate460_v1",
        "source_force_channel": "baseline-corrected external Kunwei KWR75 only",
        "source_has_ur_actual_tcp_force": False,
        "candidate_parameters": {
            **candidate_overlay.composition_binding,
            "method_id": (
                candidate_overlay.composition_binding["method_id"]
                if composition == "additive" else REPLACEMENT_METHOD_ID
            ),
            "path_semantics": (
                "retain TASE tangent path command and add projected SFC tangent velocity"
                if composition == "additive"
                else "retain TASE normal and posture, replace TASE tangent path command with projected SFC velocity"
            ),
        },
        "baseline_reproduction": {
            **{key: counters[key] for key in (
                "baseline_rows_with_qdot_error_over_1e-9",
                "jacobian_rows_with_error_over_1e-9",
            )},
            **{key: maxima[key] for key in (
                "baseline_vs_source_published_qdot_abs_rad_s",
                "source_model_jacobian_abs",
            )},
            "max_abs_tolerance_rad_s": 1e-9,
            "matches_source": baseline_matches_source,
        },
        "candidate_command_diagnostics": {
            **counters,
            "maxima": maxima,
            "means": {
                key: value / count
                for key, value in sums.items()
                if key not in {
                    "sfc_reference_alignment_cosine",
                    "candidate_task_reference_alignment_cosine",
                }
            },
            "sfc_reference_alignment": {
                "valid_rows": counters["sfc_reference_alignment_valid_rows"],
                "mean_cosine": (
                    sums["sfc_reference_alignment_cosine"]
                    / counters["sfc_reference_alignment_valid_rows"]
                    if counters["sfc_reference_alignment_valid_rows"] else None
                ),
                "positive_fraction": (
                    counters["sfc_reference_direction_positive_rows"]
                    / counters["sfc_reference_alignment_valid_rows"]
                    if counters["sfc_reference_alignment_valid_rows"] else None
                ),
            },
            "candidate_task_reference_alignment": {
                "valid_rows": counters["candidate_task_reference_alignment_valid_rows"],
                "mean_cosine": (
                    sums["candidate_task_reference_alignment_cosine"]
                    / counters["candidate_task_reference_alignment_valid_rows"]
                    if counters["candidate_task_reference_alignment_valid_rows"] else None
                ),
                "positive_fraction": (
                    counters["candidate_task_reference_direction_positive_rows"]
                    / counters["candidate_task_reference_alignment_valid_rows"]
                    if counters["candidate_task_reference_alignment_valid_rows"] else None
                ),
            },
            "mean_sfc_tangent_force_n": sums["sfc_tangent_force_n"] / count,
            "mean_sfc_force_command_speed_m_s": sums["sfc_requested_tangent_speed_m_s"] / count,
            "mean_sfc_applied_speed_m_s": sums["sfc_applied_tangent_speed_m_s"] / count,
        },
        "formal_force_mae_n": None,
        "diagnostic_force_mae_n": None,
        "force_accuracy_claim": "not estimable from fixed-observation command replay; no counterfactual force feedback",
        "timing_claim": "offline replay only; not live rate460 timing qualification",
        "candidate_trace": str(trace_path.resolve()),
    }
    report_path = out_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source-attempt", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--parameter-file", type=Path, default=DEFAULT_PARAMETER_FILE)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--max-rows", type=int)
    parser.add_argument("--phase-template-file", type=Path)
    parser.add_argument(
        "--composition",
        choices=("additive", "replace_tase_tangent"),
        default="additive",
    )
    args = parser.parse_args()
    for name in ("max_rows",):
        value = getattr(args, name)
        if value is not None and value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    report = run_replay(
        root=args.root.resolve(),
        source_attempt=args.source_attempt.resolve(),
        parameter_file=args.parameter_file.resolve(),
        out_dir=args.out_dir.resolve(),
        max_rows=args.max_rows,
        composition=args.composition,
        phase_template_file=args.phase_template_file,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI wrapper
    raise SystemExit(main())
