#!/usr/bin/env python3
"""Offline integral-saturation report from sealed raw_force_evidence.

Replays the r008 greybox normal-channel integral (limit default from
IntegralLimitCoordinate, currently 100.0 N·s) on
selected attempt sequences and emits sat_frac / time_at_limit / |I| stats.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r004.path_controller import derive_force_terms  # noqa: E402
from step5d_autotune_v4_r008.controller_seam import IntegralLimitCoordinate  # noqa: E402
from step5d_autotune_v4_r008.greybox.reconstruct import FORCE_INTEGRAL_LIMIT_N_S  # noqa: E402


def _load_observation(run_dir: Path, seq: int) -> dict[str, Any]:
    for line in (run_dir / "r006-observations.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("record_type") == "observation" and int(row.get("attempt_sequence", -1)) == seq:
            return row
    raise SystemExit(f"observation attempt_sequence={seq} missing under {run_dir}")


def _samples_to_arrays(samples: Sequence[Mapping[str, Any]]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    path_t = np.asarray([float(s["path_time_s"]) for s in samples], dtype=float)
    force = np.asarray([float(s["filtered_normal_n"]) for s in samples], dtype=float)
    if path_t.size < 2:
        raise SystemExit("raw evidence has fewer than 2 samples")
    dt = np.empty_like(path_t)
    dt[0] = max(float(path_t[1] - path_t[0]), 1e-6)
    dt[1:] = np.diff(path_t)
    dt = np.clip(dt, 1e-6, 0.05)
    return path_t, force, dt


def _integral_stats(
    force: np.ndarray,
    dt: np.ndarray,
    *,
    target_force_n: float,
    force_p_gain: float,
    force_damping: float,
    kf: float,
    integral_limit_n_s: float,
    formal_window_s: tuple[float, float],
) -> dict[str, Any]:
    limit = abs(float(integral_limit_n_s))
    n = int(force.size)
    integral = 0.0
    velocity = 0.0
    integrals = np.empty(n, dtype=float)
    at_limit = np.zeros(n, dtype=bool)
    for index in range(n):
        err = float(target_force_n) - float(force[index])
        step = dt[index]
        integral = min(max(integral + err * step, -limit), limit)
        velocity = velocity * (1.0 - step * force_damping) + step * force_p_gain * (
            err + kf * integral
        )
        integrals[index] = integral
        at_limit[index] = abs(integral) >= (limit - 1e-12)

    # Align formal MAE window [5, 60) with force-objective convention via path_time
    # reconstructed from cumsum(dt); caller passes path_t separately for masking.
    return {
        "integrals": integrals,
        "at_limit": at_limit,
        "limit_n_s": limit,
    }


def _window_mask(path_t: np.ndarray, window: tuple[float, float]) -> np.ndarray:
    lo, hi = window
    return (path_t >= lo) & (path_t < hi)


def analyze_sequence(
    run_dir: Path,
    seq: int,
    *,
    integral_limit_n_s: float,
    formal_window_s: tuple[float, float],
) -> dict[str, Any]:
    obs = _load_observation(run_dir, seq)
    raw = obs.get("raw_artifact") or {}
    rel = raw.get("relative_path")
    if not rel:
        raise SystemExit(f"seq {seq} has no raw_artifact.relative_path")
    payload = json.loads((run_dir / rel).read_text(encoding="utf-8"))
    samples = payload["samples"]
    path_t, force, dt = _samples_to_arrays(samples)
    cand = obs.get("candidate") or {}
    target = float(cand.get("target_force_n", 5.0))
    kp = float(cand["force_p_gain"])
    kd = float(cand["force_damping"])
    ki = float(cand.get("force_i_gain", 0.0))
    i_off = bool(cand.get("i_off", ki == 0.0))

    class _View:
        force_p_gain = kp
        force_i_gain = ki
        force_damping = kd
        target_force_n = target

    terms = derive_force_terms(_View())
    kf = 0.0 if i_off else float(terms["kf"])
    tracked = _integral_stats(
        force,
        dt,
        target_force_n=target,
        force_p_gain=kp,
        force_damping=kd,
        kf=kf,
        integral_limit_n_s=integral_limit_n_s,
        formal_window_s=formal_window_s,
    )
    integrals = tracked["integrals"]
    at_limit = tracked["at_limit"]
    mask = _window_mask(path_t, formal_window_s)
    if not np.any(mask):
        raise SystemExit(f"seq {seq}: empty formal window {formal_window_s}")
    abs_i = np.abs(integrals[mask])
    sat_dt = float(np.sum(dt[mask][at_limit[mask]]))
    window_dt = float(np.sum(dt[mask]))
    return {
        "attempt_sequence": seq,
        "kind": obs.get("kind"),
        "mae_n": obs.get("mae_n"),
        "eligible": obs.get("eligible"),
        "i_off": i_off,
        "force_i_gain": ki,
        "force_p_gain": kp,
        "force_damping": kd,
        "kf": kf,
        "target_force_n": target,
        "integral_limit_n_s": float(integral_limit_n_s),
        "formal_window_s": list(formal_window_s),
        "sample_count": int(force.size),
        "formal_sample_count": int(np.count_nonzero(mask)),
        "sat_frac": sat_dt / window_dt if window_dt > 0 else math.nan,
        "time_at_limit_s": sat_dt,
        "formal_duration_s": window_dt,
        "abs_I_max": float(np.max(abs_i)),
        "abs_I_p95": float(np.quantile(abs_i, 0.95)),
        "abs_I_mean": float(np.mean(abs_i)),
        "raw_relative_path": rel,
        "execution_id": obs.get("metrics", {}).get("execution_id") or raw.get("execution_id"),
    }


def _summarize_good_trials(
    trials_by_seq: Sequence[dict[str, Any]],
    *,
    limits: Sequence[float],
    mae_threshold: float,
) -> dict[str, Any]:
    good = [t for t in trials_by_seq if t.get("mae_n") is not None and float(t["mae_n"]) <= mae_threshold]
    by_limit: dict[str, dict[str, Any]] = {}
    for limit in limits:
        key = str(float(limit))
        sat_fracs = [
            float(t["by_limit"][key]["sat_frac"])
            for t in good
            if key in t.get("by_limit", {})
        ]
        by_limit[key] = {
            "good_trial_count": len(sat_fracs),
            "sat_frac_mean": float(np.mean(sat_fracs)) if sat_fracs else math.nan,
            "sat_frac_max": float(np.max(sat_fracs)) if sat_fracs else math.nan,
            "near_zero_sat_frac_le_0.01": bool(sat_fracs) and all(s <= 0.01 for s in sat_fracs),
        }
    return {
        "mae_threshold_n": float(mae_threshold),
        "good_attempt_sequences": [int(t["attempt_sequence"]) for t in good],
        "good_trial_count": len(good),
        "by_limit": by_limit,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--sequences",
        type=int,
        nargs="+",
        default=[18, 29, 34],
        help="attempt_sequence values to replay",
    )
    parser.add_argument(
        "--integral-limit-n-s",
        type=float,
        default=None,
        help="single limit (legacy); ignored when --integral-limits-n-s is set",
    )
    parser.add_argument(
        "--integral-limits-n-s",
        type=float,
        nargs="+",
        default=None,
        help="sweep multiple integral limits (N·s) in one report",
    )
    parser.add_argument("--formal-lo-s", type=float, default=5.0)
    parser.add_argument("--formal-hi-s", type=float, default=60.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/tmp/r008_far005_integral_sat_report.json"),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    run_dir = args.run_dir.resolve()
    window = (float(args.formal_lo_s), float(args.formal_hi_s))
    if args.integral_limits_n_s:
        limits = [float(v) for v in args.integral_limits_n_s]
    elif args.integral_limit_n_s is not None:
        limits = [float(args.integral_limit_n_s)]
    else:
        limits = [float(IntegralLimitCoordinate().force_integral_limit_n_s)]

    if len(limits) == 1:
        trials = [
            analyze_sequence(
                run_dir,
                seq,
                integral_limit_n_s=limits[0],
                formal_window_s=window,
            )
            for seq in args.sequences
        ]
        report = {
            "schema": "step5d.r008.integral_sat_report/v1",
            "generated_at_unix": time.time(),
            "run_dir": str(run_dir),
            "method": {
                "integral_limit_n_s": limits[0],
                "force_integral_limit_default": float(FORCE_INTEGRAL_LIMIT_N_S),
                "formal_window_s": list(window),
                "kernel": "greybox.reconstruct.normal_velocity integral clamp",
            },
            "trials": trials,
        }
    else:
        trials_by_seq: list[dict[str, Any]] = []
        for seq in args.sequences:
            by_limit: dict[str, dict[str, Any]] = {}
            base: dict[str, Any] | None = None
            for limit in limits:
                row = analyze_sequence(
                    run_dir,
                    seq,
                    integral_limit_n_s=limit,
                    formal_window_s=window,
                )
                key = str(float(limit))
                by_limit[key] = {
                    "sat_frac": row["sat_frac"],
                    "time_at_limit_s": row["time_at_limit_s"],
                    "abs_I_max": row["abs_I_max"],
                    "abs_I_p95": row["abs_I_p95"],
                    "abs_I_mean": row["abs_I_mean"],
                    "formal_duration_s": row["formal_duration_s"],
                }
                if base is None:
                    base = {k: row[k] for k in (
                        "attempt_sequence", "kind", "mae_n", "eligible", "i_off",
                        "force_i_gain", "force_p_gain", "force_damping", "kf",
                        "target_force_n", "formal_window_s", "sample_count",
                        "formal_sample_count", "raw_relative_path", "execution_id",
                    )}
            assert base is not None
            base["by_limit"] = by_limit
            trials_by_seq.append(base)
        report = {
            "schema": "step5d.r008.integral_limit_sweep/v1",
            "generated_at_unix": time.time(),
            "run_dir": str(run_dir),
            "method": {
                "integral_limits_n_s": limits,
                "force_integral_limit_default": float(FORCE_INTEGRAL_LIMIT_N_S),
                "formal_window_s": list(window),
                "kernel": "greybox.reconstruct.normal_velocity integral clamp",
            },
            "trials": trials_by_seq,
            "summary": _summarize_good_trials(trials_by_seq, limits=limits, mae_threshold=1.0),
        }
        trials = trials_by_seq
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "trial_count": len(trials)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
