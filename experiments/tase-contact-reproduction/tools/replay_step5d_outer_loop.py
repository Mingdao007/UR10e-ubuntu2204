#!/usr/bin/env python3
"""Offline realizability replay of the Step5d paper outer loop.

Feeds the recorded Stage25.0 rows of a live bridge run (pose, speed, TCP-frame
force, desired path) back through ``compute_step5d_outer_loop`` with candidate
kf/ko gains and scores the resulting commands against the command box and the
live Step5b commands that actually held contact in the same run. Read-only:
no robot/bridge interaction.

The orientation reference is the run's recorded friction-projected filtered
normal (`_step4e_filtered_normal_b_*`), i.e. exactly what the bridge control
path used. Because the recorded reference starts at the stale pre-contact
latch and only converges to the true contact normal over the run, angular
statistics are reported twice: over the full Stage25 window and over the
converged tail window. The tail window is the post-entry-re-latch proxy: with
the Stage25 entry re-latch now in the bridge, the whole next run is expected
to look like the tail.

Acceptance criteria (defaults, see --help):
  1. median |shadow linear| / |live linear| within [0.5, 2.0]
  2. tail-window angular saturation ratio (|xdot_o| >= angular limit) < 5%
  3. no tail-window linear force-channel command beyond the linear cap
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from step5d_paper_outer_loop import (  # noqa: E402
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
)

DEFAULT_RUN_DIR = Path("runs/bridge_step5d_strict_rnn_ablation_v28_20260706_054904")
DEFAULT_ADMITTANCE_SCALE = 20.0
DEFAULT_KO = 0.5
LINEAR_CMD_CAP_M_S = 0.004
ANGULAR_CMD_CAP_RAD_S = 0.015
NORMAL_FILTER_TAU_S = 0.35
NORMAL_MAX_RATE_RAD_S = 0.010
LINEAR_RATIO_MIN = 0.5
LINEAR_RATIO_MAX = 2.0
ANGULAR_SATURATION_MAX_RATIO = 0.05
STAGE25_LINE_STAGE = 25.0
MIN_LIVE_LINEAR_FOR_RATIO_M_S = 2e-5


def _f(row: dict[str, str], key: str) -> float:
    try:
        return float(row.get(key, ""))
    except (TypeError, ValueError):
        return math.nan


def _vec(row: dict[str, str], keys: tuple[str, ...]) -> np.ndarray:
    return np.asarray([_f(row, key) for key in keys], dtype=float)


def _unit(vector: np.ndarray) -> np.ndarray | None:
    norm = float(np.linalg.norm(vector))
    if not math.isfinite(norm) or norm < 1e-9:
        return None
    return vector / norm


def _rotate_toward(current: np.ndarray, target: np.ndarray, max_step_rad: float) -> np.ndarray:
    dot = float(np.clip(np.dot(current, target), -1.0, 1.0))
    angle = math.acos(dot)
    if angle <= max_step_rad or angle < 1e-9:
        return target
    axis = np.cross(current, target)
    axis_norm = float(np.linalg.norm(axis))
    if axis_norm < 1e-9:
        return target
    axis = axis / axis_norm
    step = max_step_rad
    cos_s = math.cos(step)
    sin_s = math.sin(step)
    rotated = current * cos_s + np.cross(axis, current) * sin_s + axis * float(np.dot(axis, current)) * (1.0 - cos_s)
    unit = _unit(rotated)
    return unit if unit is not None else target


def load_stage25_rows(csv_path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if _f(row, "ur_output_double_register_35") == STAGE25_LINE_STAGE:
                rows.append(row)
    return rows


def replay(
    rows: list[dict[str, str]],
    *,
    admittance_scale: float,
    ko: float,
    target_force_n: float,
    tail_fraction: float = 0.5,
) -> dict[str, object]:
    state = Step5dOuterLoopState()
    prev_t: float | None = None
    linear_ratios: list[float] = []
    angular_norms: list[float] = []
    linear_norms: list[float] = []
    force_channel_norms: list[float] = []
    replayed = 0
    for row in rows:
        t_s = _f(row, "t_monotonic_s")
        dt_s = 0.002 if prev_t is None else min(max(t_s - prev_t, 0.0005), 0.05)
        prev_t = t_s
        pose = _vec(row, tuple(f"ur_actual_TCP_pose_{i}" for i in range(6)))
        speed = _vec(row, tuple(f"ur_actual_TCP_speed_{i}" for i in range(6)))
        force_tcp = _vec(row, ("_step4e_force_t_x", "_step4e_force_t_y", "_step4e_force_t_z"))
        desired_xy = (_f(row, "_step4e_desired_x_m"), _f(row, "_step4e_desired_y_m"))
        desired_vxy = (_f(row, "_step4e_desired_vx_m_s"), _f(row, "_step4e_desired_vy_m_s"))
        reference_normal = _unit(
            _vec(row, ("_step4e_filtered_normal_b_x", "_step4e_filtered_normal_b_y", "_step4e_filtered_normal_b_z"))
        )
        if reference_normal is None or not (
            np.all(np.isfinite(pose))
            and np.all(np.isfinite(speed))
            and np.all(np.isfinite(force_tcp))
            and all(math.isfinite(value) for value in (*desired_xy, *desired_vxy))
        ):
            continue

        output = compute_step5d_outer_loop(
            Step5dOuterLoopConfig(
                kp=4.0,
                ko=ko,
                kf=1.0,
                Md_scalar=12.0 * admittance_scale,
                Bd_scalar=550.0 * admittance_scale,
                force_target_n=target_force_n,
                delay_T_s=dt_s,
                force_sign_convention="step5_step6_positive_normal_load",
            ),
            state,
            Step5dOuterLoopInputs(
                tcp_pose_base=tuple(float(v) for v in pose),
                tcp_speed_base=tuple(float(v) for v in speed),
                force_tcp_n=tuple(float(v) for v in force_tcp),
                x_pd_base=(desired_xy[0], desired_xy[1], float(pose[2])),
                xdot_pd_base=(desired_vxy[0], desired_vxy[1], 0.0),
                dt_s=dt_s,
                cmd_valid=True,
                control_reaction_normal_base=tuple(float(v) for v in reference_normal),
            ),
        )
        state = output.next_state
        replayed += 1
        xdot_p = np.asarray(output.xdot_p, dtype=float)
        xdot_o = np.asarray(output.xdot_o, dtype=float)
        linear_norm = float(np.linalg.norm(xdot_p))
        angular_norm = float(np.linalg.norm(xdot_o))
        linear_norms.append(linear_norm)
        angular_norms.append(angular_norm)
        force_channel = float(abs(np.dot(xdot_p, reference_normal)))
        force_channel_norms.append(force_channel)
        live_linear = _vec(row, ("step4e_cmd_vx_m_s", "step4e_cmd_vy_m_s", "step4e_cmd_vz_m_s"))
        live_norm = float(np.linalg.norm(live_linear))
        if math.isfinite(live_norm) and live_norm >= MIN_LIVE_LINEAR_FOR_RATIO_M_S and linear_norm > 0.0:
            linear_ratios.append(linear_norm / live_norm)

    if replayed == 0:
        raise SystemExit("no replayable Stage25.0 rows found")
    tail_start = int(replayed * (1.0 - tail_fraction))
    tail_angular = angular_norms[tail_start:]
    tail_force_channel = force_channel_norms[tail_start:]
    angular_saturated_full = sum(1 for value in angular_norms if value >= ANGULAR_CMD_CAP_RAD_S)
    angular_saturated_tail = sum(1 for value in tail_angular if value >= ANGULAR_CMD_CAP_RAD_S)
    result: dict[str, object] = {
        "admittance_scale": admittance_scale,
        "ko": ko,
        "target_force_n": target_force_n,
        "replayed_rows": replayed,
        "tail_rows": len(tail_angular),
        "linear_ratio_median": statistics.median(linear_ratios) if linear_ratios else None,
        "linear_ratio_rows": len(linear_ratios),
        "linear_cmd_norm_max_m_s": max(linear_norms),
        "full_force_channel_cmd_abs_max_m_s": max(force_channel_norms),
        "tail_force_channel_cmd_abs_max_m_s": max(tail_force_channel),
        "full_angular_cmd_norm_max_rad_s": max(angular_norms),
        "full_angular_cmd_norm_mean_rad_s": statistics.mean(angular_norms),
        "full_angular_saturation_ratio": angular_saturated_full / replayed,
        "tail_angular_cmd_norm_max_rad_s": max(tail_angular),
        "tail_angular_cmd_norm_mean_rad_s": statistics.mean(tail_angular),
        "tail_angular_saturation_ratio": angular_saturated_tail / len(tail_angular),
        "criteria": {
            "linear_ratio_band": [LINEAR_RATIO_MIN, LINEAR_RATIO_MAX],
            "tail_angular_saturation_max_ratio": ANGULAR_SATURATION_MAX_RATIO,
            "tail_force_channel_cap_m_s": LINEAR_CMD_CAP_M_S,
            "tail_fraction": tail_fraction,
        },
    }
    ratio = result["linear_ratio_median"]
    result["pass_linear_ratio"] = ratio is not None and LINEAR_RATIO_MIN <= float(ratio) <= LINEAR_RATIO_MAX
    result["pass_tail_angular_saturation"] = result["tail_angular_saturation_ratio"] < ANGULAR_SATURATION_MAX_RATIO
    result["pass_tail_force_channel_cap"] = float(result["tail_force_channel_cmd_abs_max_m_s"]) <= LINEAR_CMD_CAP_M_S
    result["ok"] = bool(
        result["pass_linear_ratio"]
        and result["pass_tail_angular_saturation"]
        and result["pass_tail_force_channel_cap"]
    )
    return result


def rotvec_force_base(pose: np.ndarray, force_tcp: np.ndarray) -> np.ndarray:
    rx, ry, rz = (float(pose[3]), float(pose[4]), float(pose[5]))
    angle = math.sqrt(rx * rx + ry * ry + rz * rz)
    if angle < 1e-12:
        return force_tcp
    kx, ky, kz = (rx / angle, ry / angle, rz / angle)
    k = np.asarray([kx, ky, kz], dtype=float)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    return force_tcp * cos_a + np.cross(k, force_tcp) * sin_a + k * float(np.dot(k, force_tcp)) * (1.0 - cos_a)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN_DIR, help="bridge run directory")
    parser.add_argument("--admittance-scale", type=float, default=DEFAULT_ADMITTANCE_SCALE)
    parser.add_argument("--ko", type=float, default=DEFAULT_KO)
    parser.add_argument("--target-force-n", type=float, default=12.0)
    parser.add_argument("--grid", action="store_true", help="scan a kf/ko grid and report all candidates")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    csv_path = args.run / "bridge_rtde_500hz.csv"
    if not csv_path.exists():
        raise SystemExit(f"bridge CSV not found: {csv_path}")
    rows = load_stage25_rows(csv_path)

    if args.grid:
        candidates = [
            replay(rows, admittance_scale=scale, ko=ko, target_force_n=args.target_force_n)
            for scale in (10.0, 20.0, 40.0)
            for ko in (0.25, 0.5, 1.0)
        ]
        payload: dict[str, object] = {"grid": candidates}
        chosen = [c for c in candidates if c["ok"]]
        payload["passing"] = [(c["admittance_scale"], c["ko"]) for c in chosen]
        print(json.dumps(payload, indent=2))
        return 0 if chosen else 2

    result = replay(rows, admittance_scale=args.admittance_scale, ko=args.ko, target_force_n=args.target_force_n)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
