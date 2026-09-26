#!/usr/bin/env python3
"""Replay the recorded LEGACY_R1 command and host slew boundary offline.

This diagnostic replays saved solver inputs and transition states from one
sealed TASE-RNN attempt. It does not connect to the robot or emit commands.
The RNN equations are evaluated as an offline source-equation shadow because
the higher-level paper-truth constructor intentionally remains gated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from step5d_autotune_v4_r014.solver_profile import LEGACY_R1


SCHEMA = "tase.r32-live-rnn-output-slew-replay-v1"
ATOL_RAD_S = 1e-10
SOURCE_FILES = (
    "tools/step5c_strict_rnn.py",
    "tools/step5d_autotune_v4_r004/motion_profile.py",
    "tools/tase_contact_provider.py",
    "tools/step5d_autotune_v4_r004/calibrated_runtime.py",
    "tools/step5d_autotune_v4_r014/solver_profile.py",
)


def _vector(value: Any, name: str) -> np.ndarray:
    try:
        out = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite six-vector") from exc
    if out.shape != (6,) or not np.isfinite(out).all():
        raise ValueError(f"{name} must be a finite six-vector")
    return out


def _matrix(value: Any, name: str) -> np.ndarray:
    try:
        out = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite 6x6 matrix") from exc
    if out.shape != (6, 6) or not np.isfinite(out).all():
        raise ValueError(f"{name} must be a finite 6x6 matrix")
    return out


def _source_equation_step(
    *,
    theta: np.ndarray,
    lambda_state: np.ndarray,
    jacobian: np.ndarray,
    twist: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    dt_s: float,
    epsilon: float,
    exponent: float,
) -> tuple[np.ndarray, np.ndarray]:
    """One LEGACY_R1 NumPy iteration as defined by StrictTaseRnnSolver.step."""

    projected = np.clip(jacobian.T @ lambda_state, lower, upper)
    error = theta - projected
    sigr = np.sign(error) * np.power(np.abs(error), exponent)
    theta_delta = -(dt_s / epsilon) * sigr
    theta_next = np.where(
        np.abs(theta_delta) > np.abs(error), projected, theta + theta_delta
    )
    residual = jacobian @ theta_next - twist
    lambda_next = lambda_state - (dt_s / epsilon) * residual
    return theta_next, lambda_next


def _transition_state(events: Sequence[dict[str, Any]], row_index: int) -> tuple[np.ndarray, np.ndarray] | None:
    if not events:
        return None
    snapshots: list[tuple[np.ndarray, np.ndarray]] = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError(f"row {row_index} has a malformed transition event")
        snapshots.append(
            (
                _vector(event.get("theta_dot_state"), "transition theta_dot_state"),
                _vector(event.get("lambda_state"), "transition lambda_state"),
            )
        )
    first_theta, first_lambda = snapshots[0]
    if any(
        not np.array_equal(theta, first_theta)
        or not np.array_equal(lambda_state, first_lambda)
        for theta, lambda_state in snapshots[1:]
    ):
        raise ValueError(f"row {row_index} has ambiguous transition snapshots")
    return first_theta.copy(), first_lambda.copy()


def replay_rows(rows: Sequence[dict[str, Any]], *, atol: float = ATOL_RAD_S) -> dict[str, Any]:
    if not rows:
        raise ValueError("command timeline is empty")
    if not math.isfinite(atol) or atol <= 0.0:
        raise ValueError("atol must be finite and positive")

    theta = np.zeros(6, dtype=float)
    lambda_state = np.zeros(6, dtype=float)
    direct_errors: list[float] = []
    slew_errors: list[float] = []
    multiply_errors: list[float] = []
    direct_mismatch_rows: list[int] = []
    slew_mismatch_rows: list[int] = []
    multiply_mismatch_rows: list[int] = []
    host_slew_rows: list[int] = []
    event_count = 0

    for index, row in enumerate(rows):
        events = row.get("transition_events") or []
        if not isinstance(events, list):
            raise ValueError(f"row {index} transition_events must be a list")
        event_count += len(events)
        snapshot = _transition_state(events, index)
        if snapshot is not None:
            theta, lambda_state = snapshot
        elif index == 0:
            raise ValueError("first row must contain its path-origin state snapshot")

        jacobian = _matrix(row.get("jacobian_6x6"), "jacobian_6x6")
        twist = _vector(row.get("requested_outer_twist_m_s_rad_s"), "requested_outer_twist_m_s_rad_s")
        lower = _vector(row.get("solver_qdot_lower_rad_s"), "solver_qdot_lower_rad_s")
        upper = _vector(row.get("solver_qdot_upper_rad_s"), "solver_qdot_upper_rad_s")
        if np.any(lower > upper):
            raise ValueError(f"row {index} has inverted solver bounds")
        dt_s = float(row.get("actual_dt_s"))
        if not math.isfinite(dt_s) or dt_s <= 0.0:
            raise ValueError(f"row {index} actual_dt_s must be finite and positive")

        theta, lambda_state = _source_equation_step(
            theta=theta,
            lambda_state=lambda_state,
            jacobian=jacobian,
            twist=twist,
            lower=lower,
            upper=upper,
            dt_s=dt_s,
            epsilon=LEGACY_R1.epsilon,
            exponent=LEGACY_R1.r,
        )
        previous = _vector(row.get("previous_published_qdot_rad_s"), "previous_published_qdot_rad_s")
        published = _vector(row.get("published_packet_qdot_rad_s"), "published_packet_qdot_rad_s")
        scale = float(row.get("host_slew_scale"))
        if not math.isfinite(scale) or not 0.0 <= scale <= 1.0:
            raise ValueError(f"row {index} host_slew_scale must be in [0,1]")
        limited = row.get("host_slew_limited")
        if type(limited) is not bool or limited != (scale < 1.0):
            raise ValueError(f"row {index} slew flag disagrees with recorded scale")

        # Runtime V3 semantics interpolate the whole command delta from the
        # previously published qdot. They do not multiply the absolute qdot.
        slew_expected = previous + scale * (theta - previous)
        multiply_expected = scale * theta
        direct_error = float(np.max(np.abs(published - theta)))
        slew_error = float(np.max(np.abs(published - slew_expected)))
        multiply_error = float(np.max(np.abs(published - multiply_expected)))
        direct_errors.append(direct_error)
        slew_errors.append(slew_error)
        multiply_errors.append(multiply_error)
        if direct_error > atol:
            direct_mismatch_rows.append(index)
        if slew_error > atol:
            slew_mismatch_rows.append(index)
        if multiply_error > atol:
            multiply_mismatch_rows.append(index)
        if limited:
            host_slew_rows.append(index)

    def summarize(values: list[float]) -> dict[str, float]:
        return {
            "max_rad_s": max(values),
            "p50_rad_s": float(np.percentile(values, 50)),
            "p95_rad_s": float(np.percentile(values, 95)),
        }

    return {
        "row_count": len(rows),
        "transition_event_count": event_count,
        "host_slew_limited_row_indices": host_slew_rows,
        "host_slew_limited_rows": len(host_slew_rows),
        "comparison_tolerance_rad_s": atol,
        "pre_slew_rnn_vs_published": {
            **summarize(direct_errors),
            "matched_rows": len(rows) - len(direct_mismatch_rows),
            "mismatch_rows": direct_mismatch_rows,
        },
        "recorded_delta_slew_vs_published": {
            **summarize(slew_errors),
            "matched_rows": len(rows) - len(slew_mismatch_rows),
            "mismatch_rows": slew_mismatch_rows,
        },
        "incorrect_absolute_scale_vs_published": {
            **summarize(multiply_errors),
            "matched_rows": len(rows) - len(multiply_mismatch_rows),
            "mismatch_rows": multiply_mismatch_rows,
            "interpretation": "diagnostic comparator only; not the runtime slew equation",
        },
        "all_rnn_only_mismatches_are_slew_limited": all(
            index in host_slew_rows for index in direct_mismatch_rows
        ),
        "delta_slew_matches_every_recorded_packet": not slew_mismatch_rows,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def analyze_attempt(attempt_dir: Path) -> dict[str, Any]:
    attempt_dir = attempt_dir.resolve()
    run_dir = attempt_dir.parent.parent
    root = run_dir.parent.parent
    trace_path = attempt_dir / "command_timeline.jsonl"
    result_path = attempt_dir / "attempt-result.json"
    receipt_path = run_dir / "dispatch_receipt.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("method") != "TASE_RNN_MATURE":
        raise ValueError("attempt receipt is not TASE_RNN_MATURE")
    replay = (result.get("sealed_evidence") or {}).get("replay_evidence") or {}
    if replay.get("complete") is not True:
        raise ValueError("sealed replay evidence is not complete")
    expected_sha = (
        (result.get("sealed_evidence") or {}).get("segments") or {}
    ).get("command_timeline", {}).get("sha256")
    if not isinstance(expected_sha, str) or _sha256(trace_path) != expected_sha:
        raise ValueError("command timeline SHA differs from the sealed receipt")
    runtime = ((result.get("state") or {}).get("runtime") or {})
    if runtime.get("solver_profile_sha256") != LEGACY_R1.sha256:
        raise ValueError("attempt solver profile is not the recorded LEGACY_R1 profile")

    rows = [json.loads(line) for line in trace_path.open(encoding="utf-8") if line.strip()]
    replay_result = replay_rows(rows)
    try:
        head = subprocess.run(
            ["git", "-C", str(root.parent), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        head = "unavailable"
    source_hashes = {
        relative: _sha256(root / relative)
        for relative in SOURCE_FILES
    }
    return {
        "schema": SCHEMA,
        "evidence_class": "offline_source_equation_replay_diagnostic_only",
        "created_from_attempt": str(attempt_dir),
        "method": receipt["method"],
        "program_identity": receipt.get("program"),
        "protocol_id": ((receipt.get("tase_parameter_binding") or {}).get("protocol_id")),
        "solver_profile": LEGACY_R1.as_dict(),
        "solver_profile_sha256": LEGACY_R1.sha256,
        "experiment_git_head": head,
        "source_sha256": source_hashes,
        "sealed_trace_sha256": expected_sha,
        "replay": replay_result,
        "claim_limits": [
            "The trace replay checks the local LEGACY_R1 source-equation and host-slew mapping against recorded packets only.",
            "It does not establish paper-equation correctness; the paper-truth contract still has pending verification fields.",
            "It does not establish physical force accuracy, independent task-force truth, or a causal force-MAE effect from slew limiting.",
            "No robot command, sensor write, package change, or motion was performed.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = analyze_attempt(args.attempt_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
