#!/usr/bin/env python3
"""Paced v34 receive-to-log timing gate with late control-thread FIFO promotion."""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import io
import json
import math
import os
import statistics
import struct
import time
from collections import deque
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np

import kunwei_rtde_bridge as bridge
import step5c_calibrated_kinematics_audit as kinematics
from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver
from step5d_control_contract import (
    ControlCandidate,
    DeferredV30Diagnostics,
    SafetyEnvelope,
    Step5dObservation,
    apply_direction_preserving_slew,
)
from step5d_paper_outer_loop import (
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
)
from step5d_runtime_interface import STEP5D_ABLATION_V34_STAGE_ID


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPLAY = (
    ROOT
    / "runs"
    / "bridge_step5d_strict_rnn_ablation_v33c20_20260715_002234"
    / "bridge_rtde_500hz.csv"
)
SOURCE_FILES = (
    "tools/run_step5d_v34_live_path_timing.py",
    "tools/kunwei_rtde_bridge.py",
    "tools/step5d_paper_outer_loop.py",
    "tools/step5c_strict_rnn.py",
    "tools/step5d_control_contract.py",
    "tools/step5d_runtime_interface.py",
    "scripts/bridge-line-operator.sh",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def finite(row: dict[str, str], key: str) -> float:
    try:
        value = float(row.get(key, ""))
    except (TypeError, ValueError):
        return math.nan
    return value if math.isfinite(value) else math.nan


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = [
            row
            for row in csv.DictReader(handle)
            if abs(finite(row, "ur_output_double_register_35") - 25.0) <= 0.03
        ]
    if len(rows) < 2000:
        raise RuntimeError(f"replay has only {len(rows)} Stage25 rows; need at least 2000")
    return rows


def distribution_ms(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "mean_ms": statistics.fmean(ordered),
        "p50_ms": ordered[int(0.50 * (len(ordered) - 1))],
        "p95_ms": ordered[int(0.95 * (len(ordered) - 1))],
        "p99_ms": ordered[int(0.99 * (len(ordered) - 1))],
        "max_ms": ordered[-1],
    }


def tick_inputs(row: dict[str, str]) -> tuple[tuple[float, ...], ...]:
    pose = tuple(finite(row, f"ur_actual_TCP_pose_{axis}") for axis in range(6))
    speed = tuple(finite(row, f"ur_actual_TCP_speed_{axis}") for axis in range(6))
    q = tuple(finite(row, f"ur_actual_q_{axis}") for axis in range(6))
    force_t = tuple(finite(row, f"_step4e_force_t_{axis}") for axis in "xyz")
    normal = tuple(finite(row, f"_step4e_control_normal_b_{axis}") for axis in "xyz")
    if not all(math.isfinite(value) for value in (*pose, *speed, *q, *force_t, *normal)):
        raise RuntimeError("nonfinite replay tick")
    return pose, speed, q, force_t, normal


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-csv", type=Path, default=DEFAULT_REPLAY)
    parser.add_argument("--duration-s", type=float, default=60.2)
    parser.add_argument("--prewarm-samples", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.duration_s < 60.0:
        raise SystemExit("--duration-s must be at least 60.0 for the frozen full-run timing gate")
    if bridge.runtime_scheduler_metadata() != {"policy": "SCHED_OTHER", "policy_value": os.SCHED_OTHER, "priority": 0}:
        raise SystemExit("v34 timing must launch under SCHED_OTHER/0")

    rows = load_rows(args.replay_csv)
    model_bundle = kinematics.build_calibrated_model()
    tcp_offset = np.asarray((0.0, 0.0, 0.1221), dtype=float)
    solver = StrictTaseRnnSolver(
        StrictRnnConfig(
            paper_truth_path=ROOT / "config" / "step5d_liveprep_solver_gate.json",
            qdot_limit_rad_s=0.5,
            epsilon=0.01,
            sigr_exponent_r=0.8,
            inner_iterations=512,
            backend="cupy",
        )
    )
    outer_state = Step5dOuterLoopState()
    previous_qdot = (0.0,) * 6

    # Prewarm CUDA, RNN, and BLAS while every thread is still SCHED_OTHER.
    for index in range(args.prewarm_samples):
        pose, speed, q, force_t, normal = tick_inputs(rows[index])
        jacobian = bridge.step5d_tcp_jacobian_base(model_bundle, np.asarray(q), tcp_offset)
        lower, upper = bridge.step5d_omega_bounds(
            np.asarray(q),
            model_bundle.model.lowerPositionLimit,
            model_bundle.model.upperPositionLimit,
            alpha_s_inv=4.0,
            qdot_limit_rad_s=0.5,
        )
        error_x = finite(rows[index], "_step4e_path_error_x_m")
        error_y = finite(rows[index], "_step4e_path_error_y_m")
        outer = compute_step5d_outer_loop(
            Step5dOuterLoopConfig(
                kp=1.5,
                ko=0.4,
                kf=0.01,
                Md_scalar=1000.0,
                Bd_scalar=7000.0,
                force_target_n=12.0,
                force_integral_limit_n_s=1.0,
            ),
            outer_state,
            Step5dOuterLoopInputs(
                tcp_pose_base=pose,
                tcp_speed_base=speed,
                force_tcp_n=force_t,
                control_reaction_normal_base=normal,
                x_pd_base=(pose[0] + error_x, pose[1] + error_y, pose[2]),
                xdot_pd_base=(
                    finite(rows[index], "_step4e_desired_vx_m_s"),
                    finite(rows[index], "_step4e_desired_vy_m_s"),
                    0.0,
                ),
                dt_s=0.002,
            ),
            include_diagnostics=False,
        )
        outer_state = outer.next_state
        desired = np.asarray(outer.xdot_c, dtype=float)
        if index == 0:
            solver.warm_start(J=jacobian, xdot_c=desired, omega_minus=lower, omega_plus=upper)
        diagnostics = solver.step(
            J=jacobian,
            xdot_c=desired,
            omega_minus=lower,
            omega_plus=upper,
            dt=0.002,
            cmd_valid=True,
        )
        raw = ControlCandidate(
            qdot=diagnostics.theta_dot_state,
            predicted_twist=tuple(float(value) for value in jacobian @ np.asarray(diagnostics.theta_dot_state)),
            residual_norm=diagnostics.constraint_residual_norm,
            active_bounds_count=sum(diagnostics.active_bounds_mask),
            frame_id="base",
            solver_status="40",
        )
        prewarm_observation = Step5dObservation(
            sequence=index,
            timestamp_s=index * 0.002,
            q=q,
            qd=(0.0,) * 6,
            tcp_pose=pose,
            tcp_twist=speed,
            wrench=tuple(force_t) + (0.0, 0.0, 0.0),
            jacobian=tuple(tuple(float(value) for value in line) for line in jacobian),
            desired_twist=outer.xdot_c,
            reaction_normal=normal,
            approach_normal=tuple(-value for value in normal),
            command_frame="base",
            normal_frame="base",
            normal_motion_policy="frame_contract_only",
        )
        slewed = apply_direction_preserving_slew(
            prewarm_observation,
            raw,
            previous_qdot=previous_qdot,
            dt_s=0.002,
            max_slew_rad_s2=0.1,
        )
        previous_qdot = slewed.qdot
        encoded = b"".join(bridge.pack_rtde_value("DOUBLE", value) for value in slewed.qdot)
        json.dumps(
            {
                "raw_rnn_residual": raw.residual_norm,
                "post_slew_residual": slewed.residual_norm,
                "qdot": slewed.qdot,
                "outer": outer.xdot_c,
            },
            separators=(",", ":"),
        )
        if len(encoded) != 48:
            raise RuntimeError("prewarm encoded qdot packet width mismatch")

    outer_state = Step5dOuterLoopState()
    previous_qdot = (0.0,) * 6
    safety_envelope = SafetyEnvelope(qdot_cap_rad_s=0.5)
    deferred = DeferredV30Diagnostics(capacity=max(33_000, int(math.ceil(args.duration_s * 500.0)) + 100))

    input_name_by_register = {
        int(field.rsplit("_", 1)[1]): name
        for field, name in zip(bridge.INPUT_FIELDS, bridge.INPUT_NAMES)
    }
    input_types = ["DOUBLE"] * len(bridge.INPUT_FIELDS)
    transport_frames = 0
    transport_payload_bytes = 0

    def capture_transport(packet_type: str, payload: bytes) -> None:
        nonlocal transport_frames, transport_payload_bytes
        if packet_type != "U":
            raise RuntimeError("unexpected timing transport packet type")
        transport_frames += 1
        transport_payload_bytes += len(payload)

    rtde_sender = bridge.RTDEBridgeClient.__new__(bridge.RTDEBridgeClient)
    rtde_sender._send_packet = capture_transport

    pending_packets: deque[tuple[int, bytes]] = deque()
    rtde_client = bridge.RTDEBridgeClient.__new__(bridge.RTDEBridgeClient)
    rtde_client.sock = object()
    rtde_client._recv_packet = pending_packets.popleft
    freshness = bridge.RTDEFeedbackFreshness()
    stream = io.StringIO()
    writer = csv.DictWriter(
        stream,
        fieldnames=(
            *bridge.INPUT_NAMES,
            "timestamp",
            "feedback_age",
            "raw_rnn_residual",
            "post_slew_residual",
            "qdot",
            "outer",
        ),
    )
    writer.writeheader()
    stream.seek(0)
    stream.truncate(0)
    compute_ms: list[float] = []
    release_times: list[float] = []
    missed_slots_total = 0
    encoded_bytes = 0
    sample_count = 0
    lifecycle: dict[str, Any] = {}
    scheduler_restored = False
    gc_was_enabled = gc.isenabled()
    try:
        gc.collect()
        gc.disable()
        lifecycle = bridge.promote_v34_control_thread_scheduler(STEP5D_ABLATION_V34_STAGE_ID)
        lifecycle["python_gc_was_enabled"] = gc_was_enabled
        lifecycle["python_gc_enabled_during_control"] = gc.isenabled()
        start = time.monotonic()
        deadline = start + 0.050
        with patch.object(
            bridge.select,
            "select",
            side_effect=lambda readers, _writers, _errors, _timeout=0.0: (
                ([rtde_client.sock], [], []) if pending_packets else ([], [], [])
            ),
        ):
            while time.monotonic() - start < args.duration_s:
                now = time.monotonic()
                if now < deadline:
                    time.sleep(deadline - now)
                released = time.monotonic()
                release_times.append(released)
                started = time.perf_counter()
                row = rows[(args.prewarm_samples + sample_count) % len(rows)]
                controller_timestamp = sample_count * 0.002
                pending_packets.extend(
                    (ord("U"), bytes([1]) + struct.pack("!d", timestamp))
                    for timestamp in (controller_timestamp - 0.004, controller_timestamp - 0.002, controller_timestamp)
                )
                decoded, drained = rtde_client.recv_latest_available_sample(1, ["DOUBLE"])
                if decoded is None or drained != 3:
                    raise RuntimeError("latest-sample drain contract failed")
                feedback_age = freshness.observe(float(decoded["timestamp"]), released)
                if freshness.update_guard(feedback_age, released):
                    raise RuntimeError("fresh timing sample triggered structural feedback stop")

                pose, speed, q, force_t, normal = tick_inputs(row)
                error_x = finite(row, "_step4e_path_error_x_m")
                error_y = finite(row, "_step4e_path_error_y_m")
                outer = compute_step5d_outer_loop(
                    Step5dOuterLoopConfig(
                        kp=1.5,
                        ko=0.4,
                        kf=0.01,
                        Md_scalar=1000.0,
                        Bd_scalar=7000.0,
                        force_target_n=12.0,
                        force_integral_limit_n_s=1.0,
                    ),
                    outer_state,
                    Step5dOuterLoopInputs(
                        tcp_pose_base=pose,
                        tcp_speed_base=speed,
                        force_tcp_n=force_t,
                        control_reaction_normal_base=normal,
                        x_pd_base=(pose[0] + error_x, pose[1] + error_y, pose[2]),
                        xdot_pd_base=(
                            finite(row, "_step4e_desired_vx_m_s"),
                            finite(row, "_step4e_desired_vy_m_s"),
                            0.0,
                        ),
                        dt_s=0.002,
                    ),
                    include_diagnostics=False,
                )
                outer_state = outer.next_state
                jacobian = bridge.step5d_tcp_jacobian_base(model_bundle, np.asarray(q), tcp_offset)
                lower, upper = bridge.step5d_omega_bounds(
                    np.asarray(q),
                    model_bundle.model.lowerPositionLimit,
                    model_bundle.model.upperPositionLimit,
                    alpha_s_inv=4.0,
                    qdot_limit_rad_s=0.5,
                )
                diagnostics = solver.step(
                    J=jacobian,
                    xdot_c=outer.xdot_c,
                    omega_minus=lower,
                    omega_plus=upper,
                    dt=0.002,
                    cmd_valid=True,
                )
                raw = ControlCandidate(
                    qdot=diagnostics.theta_dot_state,
                    predicted_twist=tuple(float(value) for value in jacobian @ np.asarray(diagnostics.theta_dot_state)),
                    residual_norm=diagnostics.constraint_residual_norm,
                    active_bounds_count=sum(diagnostics.active_bounds_mask),
                    frame_id="base",
                    solver_status="40",
                )
                observation = Step5dObservation(
                    sequence=sample_count,
                    timestamp_s=released,
                    q=q,
                    qd=(0.0,) * 6,
                    tcp_pose=pose,
                    tcp_twist=speed,
                    wrench=tuple(force_t) + (0.0, 0.0, 0.0),
                    jacobian=tuple(tuple(float(value) for value in line) for line in jacobian),
                    desired_twist=outer.xdot_c,
                    reaction_normal=normal,
                    approach_normal=tuple(-value for value in normal),
                    command_frame="base",
                    normal_frame="base",
                    path_time_s=sample_count * 0.002,
                    force_error_n=12.0 - float(np.dot(np.asarray(force_t), np.asarray(normal))),
                    orientation_error_rad=finite(row, "_step5d_outer_orientation_error_rad"),
                    omega_minus=tuple(float(value) for value in lower),
                    omega_plus=tuple(float(value) for value in upper),
                    dt_s=0.002,
                    normal_motion_policy="frame_contract_only",
                )
                slewed, _dls_shadow, decision, register_command = bridge.step5d_v30_contract_pipeline(
                    observation,
                    raw,
                    previous_qdot=previous_qdot,
                    safety_envelope=safety_envelope,
                    deferred_diagnostics=deferred,
                    max_slew_rad_s2=0.1,
                )
                if not decision.accepted or register_command.stop_request:
                    raise RuntimeError(f"production control seam rejected timing tick: {decision.reason}")
                previous_qdot = register_command.qdot
                packet = {name: 0.0 for name in bridge.INPUT_NAMES}
                packet.update(
                    {
                        "normal_force_n": float(np.dot(np.asarray(force_t), np.asarray(normal))),
                        "force_norm_n": float(np.linalg.norm(np.asarray(force_t))),
                        "heartbeat": float(sample_count),
                        "sensor_ok": 1.0,
                        "target_force_n": 12.0,
                    }
                )
                for register, value in register_command.as_register_values().items():
                    packet[input_name_by_register[register]] = float(value)
                rtde_sender.send_input_sample(
                    1,
                    input_types,
                    [packet[name] for name in bridge.INPUT_NAMES],
                )
                encoded_bytes += 1 + 8 * len(bridge.INPUT_FIELDS)
                writer.writerow(
                    {
                        **packet,
                        "timestamp": released,
                        "feedback_age": feedback_age,
                        "raw_rnn_residual": raw.residual_norm,
                        "post_slew_residual": slewed.residual_norm,
                        "qdot": json.dumps(slewed.qdot, separators=(",", ":")),
                        "outer": json.dumps(outer.xdot_c, separators=(",", ":")),
                    }
                )
                stream.seek(0)
                stream.truncate(0)
                compute_ms.append((time.perf_counter() - started) * 1000.0)
                sample_count += 1
                deadline, missed, _lateness = bridge.advance_periodic_deadline(deadline, time.monotonic(), 0.002)
                missed_slots_total += missed
    finally:
        if os.sched_getscheduler(0) == os.SCHED_FIFO:
            os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
        if gc_was_enabled:
            gc.enable()
        scheduler_restored = bridge.runtime_scheduler_metadata()["policy"] == "SCHED_OTHER"

    gaps = [later - earlier for earlier, later in zip(release_times, release_times[1:])]
    gap_over_20ms_count = sum(gap > 0.020 for gap in gaps)
    gap_45_to_60ms_count = sum(0.045 <= gap <= 0.060 for gap in gaps)
    elapsed_s = release_times[-1] - release_times[0] if len(release_times) >= 2 else 0.0
    timing = distribution_ms(compute_ms)
    source_binding = {name: sha256(ROOT / name) for name in SOURCE_FILES}
    table = json.loads((ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
    stage_row = next(row for row in table["stages"] if row.get("id") == STEP5D_ABLATION_V34_STAGE_ID)
    stage_contract = {
        key: stage_row[key]
        for key in (
            "duration_s",
            "phase_law",
            "runtime_profile",
            "scheduler_lifecycle",
            "stage25_outer_profile",
            "feedback_policy",
            "wire_protocol",
            "guard",
            "bridge_runtime",
            "acceptance",
        )
    }
    source_binding["step5d_v34_stage_contract"] = hashlib.sha256(
        json.dumps(stage_contract, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    passed = (
        elapsed_s >= 60.0
        and sample_count >= 30_000
        and deferred.count == sample_count
        and transport_frames == sample_count
        and transport_payload_bytes == encoded_bytes
        and gap_over_20ms_count == 0
        and gap_45_to_60ms_count == 0
        and timing["p99_ms"] <= 2.0
        and lifecycle.get("promotion_verified") is True
        and lifecycle.get("helper_non_other_thread_count") == 0
        and lifecycle.get("kernel_rt_bandwidth_unchanged") is True
        and lifecycle.get("python_gc_enabled_during_control") is False
        and scheduler_restored
    )
    payload: dict[str, Any] = {
        "schema": "step5d_v34_live_path_timing_v1",
        "profile": STEP5D_ABLATION_V34_STAGE_ID,
        "components": [
            "SCHED_OTHER CUDA/RNN/BLAS prewarm",
            "late control-thread SCHED_FIFO/20 promotion",
            "rtde drain-latest decode",
            "feedback age guard",
            "Step5b-equivalent outer",
            "calibrated Jacobian",
            "CuPy RNN512",
            "production candidate-to-slew-to-safety-to-register seam",
            "host qdot slew 0.1",
            "full 24-register RTDE input transport encode",
            "full input-register CSV serialization",
        ],
        "paced_elapsed_s": elapsed_s,
        "samples": sample_count,
        "encoded_bytes": encoded_bytes,
        "transport_frames": transport_frames,
        "deferred_control_rows": deferred.count,
        "timing": timing,
        "row_gap_max_s": max(gaps) if gaps else None,
        "row_gap_over_20ms_count": gap_over_20ms_count,
        "row_gap_45_to_60ms_count": gap_45_to_60ms_count,
        "missed_slots_total": missed_slots_total,
        "scheduler_lifecycle": lifecycle,
        "scheduler_restored_to_other": scheduler_restored,
        "source_binding": source_binding,
        "source_binding_sha256": hashlib.sha256(
            json.dumps(source_binding, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "replay_source": str(args.replay_csv),
        "replay_source_sha256": sha256(args.replay_csv),
        "pass": passed,
        "claim_boundary": "paced local replay only; no network, controller write, bridge start, or motion authorization",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
