#!/usr/bin/env python3
"""One source-bound v33 receive-to-log offline timing gate."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import statistics
import struct
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

import kunwei_rtde_bridge as bridge
import step5c_calibrated_kinematics_audit as kinematics
from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver
from step5d_paper_outer_loop import (
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPLAY = ROOT / "runs" / "bridge_step5d_strict_rnn_ablation_v32_20260714_225729" / "bridge_rtde_500hz.csv"
SOURCE_FILES = (
    "tools/run_step5d_v33_live_path_timing.py",
    "tools/kunwei_rtde_bridge.py",
    "tools/step5d_paper_outer_loop.py",
    "tools/step5c_strict_rnn.py",
    "tools/step5d_control_contract.py",
    "tools/step5d_runtime_interface.py",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def finite(row: dict[str, str], key: str) -> float:
    try:
        value = float(row.get(key, ""))
    except (TypeError, ValueError):
        return math.nan
    return value if math.isfinite(value) else math.nan


def load_rows(path: Path, count: int) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if abs(finite(row, "ur_output_double_register_35") - 25.0) <= 0.03:
                rows.append(row)
                if len(rows) >= count:
                    break
    if len(rows) < count:
        raise RuntimeError(f"replay has only {len(rows)} Stage25 rows; need {count}")
    return rows


def distribution(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "mean_ms": statistics.fmean(ordered),
        "p50_ms": ordered[int(0.50 * (len(ordered) - 1))],
        "p95_ms": ordered[int(0.95 * (len(ordered) - 1))],
        "p99_ms": ordered[int(0.99 * (len(ordered) - 1))],
        "max_ms": ordered[-1],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-csv", type=Path, default=DEFAULT_REPLAY)
    parser.add_argument("--samples", type=int, default=2000)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = load_rows(args.replay_csv, args.samples + args.warmup)
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
    elapsed_ms: list[float] = []
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=["timestamp", "feedback_age", "qdot", "outer", "solver_status"])
    freshness = bridge.RTDEFeedbackFreshness()
    pending_packets: deque[tuple[int, bytes]] = deque()
    rtde_client = bridge.RTDEBridgeClient.__new__(bridge.RTDEBridgeClient)
    rtde_client.sock = object()
    rtde_client._recv_packet = pending_packets.popleft
    bridge.select.select = lambda readers, _writers, _errors, _timeout=0.0: (
        ([rtde_client.sock], [], []) if pending_packets else ([], [], [])
    )
    for index, row in enumerate(rows):
        started = time.perf_counter()
        timestamp = float(index) * 0.002
        pending_packets.extend(
            (ord("U"), bytes([1]) + struct.pack("!d", sample_timestamp))
            for sample_timestamp in (timestamp - 0.004, timestamp - 0.002, timestamp)
        )
        decoded, drained = rtde_client.recv_latest_available_sample(1, ["DOUBLE"])
        if decoded is None or drained != 3:
            raise RuntimeError(f"latest-sample drain contract failed: decoded={decoded} drained={drained}")
        feedback_age = freshness.observe(float(decoded["timestamp"]), timestamp + 0.001)
        feedback_structural_stop = freshness.update_guard(feedback_age, timestamp + 0.001)
        if feedback_structural_stop:
            raise RuntimeError("fresh timing sample unexpectedly triggered feedback structural stop")
        pose = tuple(finite(row, f"ur_actual_TCP_pose_{axis}") for axis in range(6))
        speed = tuple(finite(row, f"ur_actual_TCP_speed_{axis}") for axis in range(6))
        q = np.asarray([finite(row, f"ur_actual_q_{axis}") for axis in range(6)], dtype=float)
        force_t = tuple(finite(row, f"_step4e_force_t_{axis}") for axis in "xyz")
        normal = tuple(finite(row, f"_step4e_control_normal_b_{axis}") for axis in "xyz")
        error_x = finite(row, "_step4e_path_error_x_m")
        error_y = finite(row, "_step4e_path_error_y_m")
        output = compute_step5d_outer_loop(
            Step5dOuterLoopConfig(
                kp=1.5, ko=0.4, kf=0.01, Md_scalar=1000.0, Bd_scalar=7000.0,
                force_target_n=12.0, force_integral_limit_n_s=1.0,
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
        outer_state = output.next_state
        jacobian = bridge.step5d_tcp_jacobian_base(model_bundle, q, tcp_offset)
        lower, upper = bridge.step5d_omega_bounds(
            q,
            model_bundle.model.lowerPositionLimit,
            model_bundle.model.upperPositionLimit,
            alpha_s_inv=4.0,
            qdot_limit_rad_s=0.5,
        )
        if index == 0:
            solver.warm_start(J=jacobian, xdot_c=output.xdot_c, omega_minus=lower, omega_plus=upper)
        diagnostics = solver.step(
            J=jacobian,
            xdot_c=output.xdot_c,
            omega_minus=lower,
            omega_plus=upper,
            dt=0.002,
            cmd_valid=True,
        )
        qdot = diagnostics.theta_dot_state
        encoded = b"".join(bridge.pack_rtde_value("DOUBLE", value) for value in qdot)
        if len(encoded) != 48:
            raise RuntimeError("encoded qdot packet width mismatch")
        writer.writerow(
            {
                "timestamp": timestamp,
                "feedback_age": feedback_age,
                "qdot": json.dumps(qdot, separators=(",", ":")),
                "outer": json.dumps(output.xdot_c, separators=(",", ":")),
                "solver_status": "accepted",
            }
        )
        stream.seek(0)
        stream.truncate(0)
        elapsed = (time.perf_counter() - started) * 1000.0
        if index >= args.warmup:
            elapsed_ms.append(elapsed)
    timing = distribution(elapsed_ms)
    source_binding = {name: sha256(ROOT / name) for name in SOURCE_FILES}
    stage_table = json.loads((ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
    stage_contract = {
        row["id"]: {
            "runtime_profile": row["runtime_profile"],
            "stage25_outer_profile": row["stage25_outer_profile"],
            "feedback_policy": row["feedback_policy"],
            "guard": row["guard"],
        }
        for row in stage_table["stages"]
        if row.get("id") in {
            "step5d_strict_rnn_ablation_v33c20",
            "step5d_strict_rnn_ablation_v33",
        }
    }
    source_binding["step5d_v33_stage_contract"] = hashlib.sha256(
        json.dumps(stage_contract, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload: dict[str, Any] = {
        "schema": "step5d_v33_live_path_timing_v1",
        "profile": "step5d_strict_rnn_ablation_v33",
        "components": ["rtde_drain_latest_decode", "feedback_age_guard", "outer", "calibrated_jacobian", "cupy_rnn512", "rtde_encode", "csv_log"],
        "samples": len(elapsed_ms),
        "warmup": args.warmup,
        "timing": timing,
        "deadline_ms": 2.0,
        "p99_deadline_pass": timing["p99_ms"] <= 2.0,
        "source_binding": source_binding,
        "source_binding_sha256": hashlib.sha256(
            json.dumps(source_binding, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "replay_source": str(args.replay_csv),
        "replay_source_sha256": sha256(args.replay_csv),
        "claim_boundary": "offline exact-component live-path timing; no network, controller write, bridge start, or motion authorization",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["p99_deadline_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
