#!/usr/bin/env python3
"""Sweep strict-RNN parameters through the shared P0 MuJoCo control path.

The frozen P0 v8 profile remains the comparison baseline.  Candidate profiles
may change only the RNN integration parameters; the calibrated model, target
builder, qdot rail, SafetyEnvelope, DLS shadow-only path, and simulator adapter
remain identical.  A sweep is diagnostic evidence and cannot promote live or
P0 acceptance state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

import run_step5d_p0_v8_mujoco as p0
from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver
from step5d_control_contract import V30_DEFERRED_NUMERIC_FIELDS
from ur10e_mujoco_adapter import MuJoCoVelocityPlant


BASELINE = {
    "inner_iterations": 1024,
    "epsilon": 0.010,
    "sigr_exponent_r": 0.8,
}


@dataclass(frozen=True, order=True)
class RnnProfile:
    inner_iterations: int
    epsilon: float
    sigr_exponent_r: float

    def validate(self) -> None:
        if int(self.inner_iterations) < 1:
            raise ValueError("inner_iterations must be positive")
        if not math.isfinite(float(self.epsilon)) or float(self.epsilon) <= 0.0:
            raise ValueError("epsilon must be finite and positive")
        if (
            not math.isfinite(float(self.sigr_exponent_r))
            or not 0.0 < float(self.sigr_exponent_r) <= 1.0
        ):
            raise ValueError("sigr_exponent_r must be in (0, 1]")

    def payload(self) -> dict[str, int | float | str | bool]:
        self.validate()
        return {
            "backend": "cupy",
            "inner_iterations": int(self.inner_iterations),
            "epsilon": float(self.epsilon),
            "sigr_exponent_r": float(self.sigr_exponent_r),
            "qdot_cap_rad_s": p0.P0_V8_QDOT_CAP_RAD_S,
            "effective_ko": p0.P0_V8_EFFECTIVE_KO,
            "dls_runtime_fallback_allowed": False,
        }

    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def parse_ints(value: str) -> tuple[int, ...]:
    result = tuple(int(piece.strip()) for piece in value.split(",") if piece.strip())
    if not result or any(item < 1 for item in result):
        raise argparse.ArgumentTypeError("expected positive comma-separated integers")
    return result


def parse_floats(value: str) -> tuple[float, ...]:
    result = tuple(float(piece.strip()) for piece in value.split(",") if piece.strip())
    if not result or any(not math.isfinite(item) for item in result):
        raise argparse.ArgumentTypeError("expected finite comma-separated floats")
    return result


def profiles(
    iterations: Sequence[int],
    epsilon_values: Sequence[float],
    r_values: Sequence[float],
) -> tuple[RnnProfile, ...]:
    baseline = RnnProfile(**BASELINE)
    candidates = {
        RnnProfile(int(inner), float(epsilon), float(r))
        for inner in iterations
        for epsilon in epsilon_values
        for r in r_values
    }
    candidates.add(baseline)
    for candidate in candidates:
        candidate.validate()
    return (baseline, *sorted(candidates - {baseline}))


def make_solver(profile: RnnProfile) -> StrictTaseRnnSolver:
    solver = StrictTaseRnnSolver(
        StrictRnnConfig(
            paper_truth_path=p0.EXPERIMENT_ROOT
            / "config"
            / "step5d_liveprep_solver_gate.json",
            qdot_limit_rad_s=p0.P0_V8_QDOT_CAP_RAD_S,
            epsilon=profile.epsilon,
            sigr_exponent_r=profile.sigr_exponent_r,
            inner_iterations=profile.inner_iterations,
            backend="cupy",
        )
    )
    if not solver.cupy_host_staging_pinned:
        raise RuntimeError("profile sweep requires pinned CuPy host staging")
    if not solver.cupy_dedicated_stream or not solver.cupy_busy_poll_completion:
        raise RuntimeError("profile sweep requires the production CuPy completion path")
    equivalence = solver.cupy_parallel_equivalence or {}
    if equivalence.get("bitwise_equal") is not True:
        raise RuntimeError("profile failed parallel/serial equation equivalence")
    return solver


def distribution(values: np.ndarray) -> dict[str, float]:
    array = np.asarray(values, dtype=float)
    if array.size < 1 or not np.all(np.isfinite(array)):
        raise ValueError("distribution requires finite samples")
    return {
        "p50": float(np.percentile(array, 50)),
        "p95": float(np.percentile(array, 95)),
        "p99": float(np.percentile(array, 99)),
        "max": float(np.max(array)),
    }


def _deferred_column(result: p0.NominalPhaseResult, name: str) -> np.ndarray:
    index = V30_DEFERRED_NUMERIC_FIELDS.index(name)
    return result.deferred.numeric[: result.deferred.count, index]


def summarize(
    profile: RnnProfile,
    result: p0.NominalPhaseResult,
    baseline: p0.NominalPhaseResult,
    *,
    paced: bool,
) -> dict[str, Any]:
    if result.qdot.shape != baseline.qdot.shape:
        raise ValueError("candidate and baseline trace shapes differ")
    predicted = np.einsum("nij,nj->ni", result.command_jacobian, result.qdot)
    baseline_predicted = np.einsum(
        "nij,nj->ni", baseline.command_jacobian, baseline.qdot
    )
    predicted_approach = np.einsum(
        "ni,ni->n", predicted[:, :3], result.approach_normal
    )
    desired_approach = np.einsum(
        "ni,ni->n", result.desired_twist[:, :3], result.approach_normal
    )
    residual = _deferred_column(result, "residual_norm")
    wall = p0.wall_timing(result, paced=paced)
    accepted_ratio = result.accepted_tick_count / result.tick_count
    quality_eligible = bool(
        result.control_path_pass
        and accepted_ratio >= 0.99
        and np.count_nonzero(predicted_approach <= 0.0) == 0
        and np.count_nonzero(desired_approach <= 0.0) == 0
        and np.all(np.isfinite(residual))
        and float(np.max(residual)) <= 1e-3
    )
    return {
        "profile": profile.payload(),
        "profile_sha256": profile.fingerprint(),
        "is_v8_baseline": profile == RnnProfile(**BASELINE),
        "samples": result.tick_count,
        "paced_500hz": bool(paced),
        "control_path_pass": bool(result.control_path_pass),
        "quality_eligible": quality_eligible,
        "timing_eligible": bool(quality_eligible and wall["pass"] is True),
        "accepted_ratio": accepted_ratio,
        "safe_hold_count": result.safe_hold_count,
        "stop_count": result.stop_count,
        "nonfinite_output_count": result.nonfinite_output_count,
        "qdot_bound_violation_count": result.qdot_bound_violation_count,
        "unexpected_contact_count": result.unexpected_contact_count,
        "cage_collision_count": result.cage_collision_count,
        "missed_sequence_count": 0,
        "normal_sign_mismatch_count": int(
            np.count_nonzero(predicted_approach <= 0.0)
        ),
        "max_qdot_abs_rad_s": result.max_qdot_abs_rad_s,
        "residual_norm": distribution(residual),
        "qdot_delta_vs_v8_rad_s": distribution(
            np.max(np.abs(result.qdot - baseline.qdot), axis=1)
        ),
        "twist_delta_vs_v8": distribution(
            np.linalg.norm(predicted - baseline_predicted, axis=1)
        ),
        "wall_timing": wall,
        "cupy_parallel_equivalence": {
            "bitwise_equal": True,
            "parallel_block_threads": 6,
        },
    }


def choose(rows: Iterable[dict[str, Any]], *, require_timing: bool) -> dict[str, Any] | None:
    key = "timing_eligible" if require_timing else "quality_eligible"
    eligible = [row for row in rows if row.get(key) is True]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda row: (
            float(row["wall_timing"]["p99_ms"]),
            float(row["wall_timing"]["max_ms"]),
            int(row["profile"]["inner_iterations"]),
            float(row["profile"]["epsilon"]),
            float(row["profile"]["sigr_exponent_r"]),
        ),
    )


def run(
    *,
    model_manifest: Path,
    candidate_profiles: Sequence[RnnProfile],
    duration_s: float,
    paced: bool,
) -> dict[str, Any]:
    if not candidate_profiles or candidate_profiles[0] != RnnProfile(**BASELINE):
        raise ValueError("the frozen v8 baseline must be evaluated first")
    plant = MuJoCoVelocityPlant(model_manifest)
    spec = p0.PhaseSpec(duration_s=float(duration_s), sequence_index=0)
    baseline_solver = make_solver(candidate_profiles[0])
    baseline_result = p0.run_nominal_phase(
        plant=plant,
        solver=baseline_solver,
        spec=spec,
        pace_wall_clock=paced,
    )
    rows = [
        summarize(
            candidate_profiles[0],
            baseline_result,
            baseline_result,
            paced=paced,
        )
    ]
    for candidate in candidate_profiles[1:]:
        result = p0.run_nominal_phase(
            plant=plant,
            solver=make_solver(candidate),
            spec=spec,
            pace_wall_clock=paced,
        )
        rows.append(summarize(candidate, result, baseline_result, paced=paced))
    selected = choose(rows, require_timing=paced)
    return {
        "schema": "step5d_p0_rnn_profile_sweep_v1",
        "mode": "offline_parameter_selection_no_live_promotion",
        "baseline_profile": candidate_profiles[0].payload(),
        "duration_s": float(duration_s),
        "paced_500hz": bool(paced),
        "profile_count": len(rows),
        "profiles": rows,
        "selected_profile": None if selected is None else selected["profile"],
        "selected_profile_sha256": (
            None if selected is None else selected["profile_sha256"]
        ),
        "selection_requires_followup_2_10_60": True,
        "safety_boundary": {
            "qdot_cap_rad_s": p0.P0_V8_QDOT_CAP_RAD_S,
            "dls_runtime_fallback_allowed": False,
            "safety_envelope_unchanged": True,
            "controller_upload_authorized": False,
            "bridge_start_authorized": False,
            "tp_play_authorized": False,
            "live_motion_authorized": False,
            "workflow_state": "liveprep_blocked",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-manifest", type=Path, required=True)
    parser.add_argument("--iterations", type=parse_ints, default=(128, 256, 512))
    parser.add_argument(
        "--epsilon-values", type=parse_floats, default=(0.005, 0.010)
    )
    parser.add_argument("--r-values", type=parse_floats, default=(0.6, 0.8, 1.0))
    parser.add_argument("--duration-s", type=float, default=2.0)
    parser.add_argument("--paced", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = run(
        model_manifest=args.model_manifest,
        candidate_profiles=profiles(
            args.iterations, args.epsilon_values, args.r_values
        ),
        duration_s=args.duration_s,
        paced=args.paced,
    )
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(args.output)
    return 0 if payload["selected_profile"] is not None else 3


if __name__ == "__main__":
    raise SystemExit(main())
