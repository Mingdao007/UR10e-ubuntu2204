"""Offline TASE comparison and frozen holdout campaign.

The campaign is deliberately separate from the live-entry registry.  Every
method receives the same measured wrench/pose/Jacobian trace and the same
joint-velocity box.  A small deterministic contact-plant proxy supplies
response variation for software testing; it is never presented as a UR10e
measurement or a surface model.  Unknown or unavailable compositions remain
failed attempts in the denominator and are never substituted or sent to live.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import random
from typing import Any, Mapping

import numpy as np

from contact_method_registry import default_registry
from contact_yield_protocol import Task


SCHEMA = "ur10e.tase-research-campaign-v1"
ATTEMPT_SCHEMA = "ur10e.tase-research-attempt-v1"
PRIMARY_METHOD = "TASE_RNN_MATURE"
EXECUTABLE_METHODS = (
    "TASE_RNN",
    "TASE_RNN_MATURE",
    "TASE_RNN_MATURE_MINUS",
    "TASE_QP",
    "TASE_IMPROVED",
)
COMPOSITION_METHODS = (
    "TASE_RNN_MATURE+LAC",
    "TASE_RNN_MATURE+NAC",
    "TASE_RNN_MATURE+SFC",
)
METHODS = EXECUTABLE_METHODS + COMPOSITION_METHODS
CASES = ("plane", "incline", "low_curvature", "stiffness_change")
DT_S = 0.002
FORCE_TARGET_N = 5.0
RAW_FORCE_LIMIT_N = 20.0
QDOT_LIMIT_RAD_S = 0.05


@dataclass(frozen=True)
class CampaignConfig:
    """Budget and deterministic proxy settings.

    Defaults implement the requested 8 initial + 12 BO + 4 repeat units and
    five paired holdout rounds.  Smaller values are useful for focused tests.
    """

    horizon_ticks: int = 256
    seed: int = 20260921
    initial_units: int = 8
    bo_units: int = 12
    repeat_units: int = 4
    holdout_rounds: int = 5

    def __post_init__(self) -> None:
        for name in ("horizon_ticks", "initial_units", "bo_units", "repeat_units", "holdout_rounds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.horizon_ticks < 8:
            raise ValueError("horizon_ticks must be at least 8")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ValueError("seed must be a non-negative integer")

    @property
    def tuning_units(self) -> int:
        return self.initial_units + self.bo_units

    @property
    def total_units(self) -> int:
        return self.tuning_units + self.repeat_units


@dataclass(frozen=True)
class SurfaceCase:
    name: str
    description: str
    nominal_normal: tuple[float, float, float]
    stiffness_n_per_m: float


SURFACE_CASES = {
    "plane": SurfaceCase("plane", "fixed plane; measured-wrench normal only", (0.0, 0.0, 1.0), 120.0),
    "incline": SurfaceCase("incline", "10 degree nominal incline; no geometry input", (0.173648, 0.0, 0.984808), 120.0),
    "low_curvature": SurfaceCase("low_curvature", "slowly varying low-curvature normal proxy", (0.0, 0.0, 1.0), 120.0),
    "stiffness_change": SurfaceCase("stiffness_change", "piecewise stiffness change; no curvature input", (0.0, 0.0, 1.0), 260.0),
}


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _unit(value: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(value))
    if not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError("normal proxy has invalid norm")
    return value / norm


def _normal_for(case: SurfaceCase, index: int, horizon: int) -> np.ndarray:
    if case.name == "low_curvature":
        phase = 2.0 * math.pi * index / max(1, horizon - 1)
        return _unit(np.asarray((0.05 * math.sin(phase), 0.035 * math.cos(phase), 1.0)))
    return _unit(np.asarray(case.nominal_normal, dtype=float))


def _rotation_for_reaction(normal: np.ndarray) -> np.ndarray:
    """Construct a fixed-roll TCP frame whose tool axis is the approach axis."""
    approach = -_unit(np.asarray(normal, dtype=float))
    reference = np.asarray((1.0, 0.0, 0.0))
    if abs(float(np.dot(reference, approach))) > 0.9:
        reference = np.asarray((0.0, 1.0, 0.0))
    x_axis = _unit(reference - float(np.dot(reference, approach)) * approach)
    y_axis = np.cross(approach, x_axis)
    return np.column_stack((x_axis, y_axis, approach))


def _disturbance(case: SurfaceCase, index: int, horizon: int, rng: random.Random) -> np.ndarray:
    if case.name == "plane":
        return np.zeros(3)
    if case.name == "incline":
        return np.asarray((0.0, 0.10 * math.sin(2.0 * math.pi * index / horizon), 0.0))
    if case.name == "low_curvature":
        return np.asarray((0.0, 0.0, 0.25 * math.sin(4.0 * math.pi * index / horizon)))
    # A deterministic stiffness case includes a short wrench disturbance; it
    # is a software proxy and is labelled as such in every result row.
    pulse = 0.6 if int(0.35 * horizon) <= index < int(0.55 * horizon) else 0.0
    return np.asarray((0.05 * rng.uniform(-1.0, 1.0), 0.0, pulse))


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    return float(np.percentile(np.asarray(values, dtype=float), 95.0))


def _metrics(rows: list[dict[str, Any]], *, failed: bool, error: str | None) -> dict[str, Any]:
    errors = [float(row["normal_error_n"]) for row in rows]
    abs_errors = [abs(value) for value in errors]
    force_norms = [float(row["force_norm_n"]) for row in rows]
    path_errors = [float(row["path_error_m"]) for row in rows]
    qdot = [float(row["qdot_norm_rad_s"]) for row in rows]
    realization_residuals = [float(row["realization_residual_norm"]) for row in rows]
    ages = [float(row["age_s"]) for row in rows]
    contact_lost = sum(float(row["normal_force_n"]) < 1.0 for row in rows)
    saturated = sum(bool(row["saturated"]) for row in rows)
    return {
        "normal_force_mae_n": float(np.mean(abs_errors)) if errors else None,
        "normal_force_rmse_n": float(np.sqrt(np.mean(np.square(errors)))) if errors else None,
        "normal_force_bias_n": float(np.mean(errors)) if errors else None,
        "normal_force_tail_p95_n": _p95(abs_errors),
        "force_norm_max_n": max(force_norms) if force_norms else None,
        "contact_loss_ticks": int(contact_lost),
        "trajectory_completion": bool(rows and not failed and path_errors[-1] <= 0.02),
        "path_error_rms_m": float(np.sqrt(np.mean(np.square(path_errors)))) if path_errors else None,
        "realization_residual_max": max(realization_residuals) if realization_residuals else None,
        "saturation_ticks": int(saturated),
        "timing_dt_s": {"min": min((float(row["dt_s"]) for row in rows), default=None),
                        "max": max((float(row["dt_s"]) for row in rows), default=None),
                        "mean": float(np.mean([float(row["dt_s"]) for row in rows])) if rows else None},
        "observation_age_s": {"p95": _p95(ages), "max": max(ages, default=None)},
        "attempt_ticks": len(rows),
        "failed": bool(failed),
        "error": error,
    }


def _candidate(method: str, index: int) -> dict[str, Any]:
    """Deterministic initial/BO proposal; the ledger still freezes the winner."""
    if method == "TASE_RNN":
        return {"epsilon": 0.014 + 0.0015 * (index % 8), "sigr_exponent_r": 0.2 + 0.05 * (index % 4), "lambda_update_sign": "plus"}
    if method in {"TASE_RNN_MATURE", "TASE_RNN_MATURE_MINUS"}:
        return {"epsilon": 0.014 + 0.0015 * (index % 8), "sigr_exponent_r": 0.2 + 0.05 * (index % 4), "lambda_update_sign": "minus"}
    if method == "TASE_QP":
        return {"lambda_update_sign": "minus"}
    if method == "TASE_IMPROVED":
        return {"force_integral_limit_n_s": 0.5 + 0.25 * (index % 5), "force_contact_gate_n": 0.25 + 0.25 * (index % 3), "force_integral_leak_tau_s": 0.25 + 0.125 * (index % 4), "normal_weight": 80.0 + 20.0 * (index % 4)}
    return {"normal_controller": "TASE_RNN_MATURE", "tangential_controller": method.split("+", 1)[1]}


def _make_observation(*, position: np.ndarray, normal: np.ndarray, force_n: float, index: int, horizon: int, dt_s: float, stiffness: float, rng: random.Random) -> tuple[dict[str, Any], dict[str, Any]]:
    task = Task()
    t = index * dt_s
    reference = task.reference(min(t, task.duration_s))
    measured_force = normal * force_n + _disturbance(SURFACE_CASES["stiffness_change"], index, horizon, rng) * (0.0 if stiffness < 200.0 else 1.0)
    observation = {
        "time_s": float(t), "state_age_s": 0.010 if index % 5 else 0.018,
        "position_m": tuple(float(x) for x in position), "rotation": _rotation_for_reaction(normal),
        "joint_position_rad": (0.0,) * 6, "jacobian": np.eye(6),
        "raw_force_base_n": tuple(float(x) for x in measured_force),
        "raw_torque_base_nm": (0.0, 0.0, 0.0), "joint_velocity_lower": (-QDOT_LIMIT_RAD_S,) * 6,
        "joint_velocity_upper": (QDOT_LIMIT_RAD_S,) * 6,
        "linear_velocity_base_m_s": (0.0, 0.0, 0.0), "angular_velocity_base_rad_s": (0.0, 0.0, 0.0),
        # This is a measured-wrench direction estimate, not supplied surface geometry.
        "local_normal_base": tuple(float(x) for x in _unit(measured_force)),
        "software_injection_base_n": (0.0, 0.0, 0.0),
    }
    ref = {"position_m": reference["position_m"], "velocity_m_s": reference["velocity_m_s"], "reference_force_n": FORCE_TARGET_N}
    return observation, ref


def run_attempt(*, method: str, candidate: Mapping[str, Any], case_name: str, attempt_id: str, config: CampaignConfig, qp_library: Path) -> dict[str, Any]:
    case = SURFACE_CASES[case_name]
    registry = default_registry()
    rows: list[dict[str, Any]] = []
    position = np.zeros(3, dtype=float)
    force_n = FORCE_TARGET_N
    rng = random.Random(config.seed ^ int(_sha({"attempt": attempt_id})[:8], 16))
    handle = None
    error: str | None = None
    try:
        if method in COMPOSITION_METHODS:
            raise RuntimeError("composition identity is offline-only but no executable common-realizer adapter is registered")
        options = {"qp_library": qp_library}
        if method == "TASE_IMPROVED":
            options["config"] = dict(candidate)
        else:
            options["config"] = dict(candidate)
        handle = registry.initialize(method, **options)
        for index in range(config.horizon_ticks):
            normal = _normal_for(case, index, config.horizon_ticks)
            stiffness = case.stiffness_n_per_m
            if case_name == "stiffness_change" and index >= config.horizon_ticks // 2:
                stiffness = 260.0
            observation, reference = _make_observation(position=position, normal=normal, force_n=force_n, index=index, horizon=config.horizon_ticks, dt_s=DT_S, stiffness=stiffness, rng=rng)
            result = handle.step(observation, reference, DT_S)
            qdot = np.asarray(result["qdot_rad_s"], dtype=float)
            # The plant receives the final realized command J qdot.  The
            # proxy uses J=I, but computing it explicitly keeps the metric
            # tied to the same joint-velocity realization contract as live.
            jacobian = np.asarray(observation["jacobian"], dtype=float)
            actual_twist = jacobian @ qdot
            if actual_twist.shape != (6,) or not np.all(np.isfinite(actual_twist)):
                raise RuntimeError("realized Jqdot is invalid")
            desired_twist = np.asarray(result.get("xdot_c", actual_twist), dtype=float)
            if desired_twist.shape != (6,) or not np.all(np.isfinite(desired_twist)):
                raise RuntimeError("adapter returned an invalid desired Cartesian command")
            realization_residual = float(np.linalg.norm(actual_twist - desired_twist))
            normal_velocity = float(np.dot(actual_twist[:3], normal))
            target_position = np.asarray(reference["position_m"], dtype=float)
            path_error = float(np.linalg.norm(position - target_position))
            force_n += DT_S * (-stiffness * normal_velocity - 0.7 * (force_n - FORCE_TARGET_N))
            force_n += 0.15 * math.sin(index * 0.11) if case_name != "plane" else 0.0
            force_n = float(np.clip(force_n, 0.0, RAW_FORCE_LIMIT_N - 1e-6))
            rows.append({"time_s": index * DT_S, "dt_s": DT_S, "age_s": observation["state_age_s"], "normal_force_n": force_n, "normal_error_n": force_n - FORCE_TARGET_N, "force_norm_n": float(np.linalg.norm(observation["raw_force_base_n"])), "path_error_m": path_error, "qdot_norm_rad_s": float(np.linalg.norm(qdot)), "saturated": bool(np.any(np.isclose(qdot, QDOT_LIMIT_RAD_S, atol=1e-8))), "normal_velocity_m_s": normal_velocity, "realization_residual_norm": realization_residual})
            position = position + DT_S * actual_twist[:3]
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if handle is not None:
            handle.close()
    metrics = _metrics(rows, failed=error is not None or len(rows) < config.horizon_ticks, error=error)
    return {"schema": ATTEMPT_SCHEMA, "attempt_id": attempt_id, "method": method, "candidate": dict(candidate), "case": case_name, "proxy_only": True, "surface_geometry_provided_to_controller": False, "metrics": metrics}


def _ci95(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean": None, "lower": None, "upper": None}
    mean = float(np.mean(values))
    if len(values) == 1:
        half = 0.0
    else:
        half = 1.96 * float(np.std(values, ddof=1)) / math.sqrt(len(values))
    return {"n": len(values), "mean": mean, "lower": mean - half, "upper": mean + half}


def run_campaign(*, output_dir: Path, qp_library: Path, config: CampaignConfig = CampaignConfig()) -> dict[str, Any]:
    out = Path(output_dir).resolve()
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty output: {out}")
    out.mkdir(parents=True, exist_ok=True)
    attempts: list[dict[str, Any]] = []
    frozen: dict[str, dict[str, Any]] = {}
    for method in METHODS:
        training: list[dict[str, Any]] = []
        for unit in range(config.tuning_units):
            strategy = "initial" if unit < config.initial_units else "bo"
            candidate = _candidate(method, unit)
            case_name = CASES[unit % len(CASES)]
            attempt = run_attempt(method=method, candidate=candidate, case_name=case_name, attempt_id=f"{method}-{strategy}-{unit:02d}", config=config, qp_library=Path(qp_library))
            attempt["budget_stage"] = strategy
            attempts.append(attempt); training.append(attempt)
        usable = [a for a in training if not a["metrics"]["failed"] and a["metrics"]["normal_force_mae_n"] is not None]
        if usable:
            selected = min(usable, key=lambda a: (float(a["metrics"]["normal_force_mae_n"]), a["attempt_id"]))["candidate"]
        else:
            selected = _candidate(method, 0)
        frozen[method] = {"candidate": dict(selected), "source": "min_training_normal_force_mae" if usable else "no_eligible_training_attempt", "eligible_training_attempts": len(usable)}
        for repeat in range(config.repeat_units):
            attempt = run_attempt(method=method, candidate=selected, case_name=CASES[(config.tuning_units + repeat) % len(CASES)], attempt_id=f"{method}-repeat-{repeat:02d}", config=config, qp_library=Path(qp_library))
            attempt["budget_stage"] = "repeat"
            attempts.append(attempt)
    holdout: list[dict[str, Any]] = []
    for block in range(config.holdout_rounds):
        order = list(METHODS); random.Random(config.seed + block).shuffle(order)
        for method in order:
            for case_name in CASES:
                attempt = run_attempt(method=method, candidate=frozen[method]["candidate"], case_name=case_name, attempt_id=f"holdout-{block:02d}-{method}-{case_name}", config=config, qp_library=Path(qp_library))
                attempt["budget_stage"] = "frozen_holdout"; attempt["block"] = block; holdout.append(attempt)
    paired: dict[str, Any] = {}
    baseline = [a for a in holdout if a["method"] == PRIMARY_METHOD]
    base_map = {(a["block"], a["case"]): a for a in baseline}
    for method in METHODS:
        if method == PRIMARY_METHOD:
            continue
        diffs: list[float] = []
        for a in holdout:
            key = (a["block"], a["case"])
            b = base_map.get(key)
            if a["method"] == method and b is not None and not a["metrics"]["failed"] and not b["metrics"]["failed"]:
                diffs.append(float(a["metrics"]["normal_force_mae_n"]) - float(b["metrics"]["normal_force_mae_n"]))
        ci = _ci95(diffs); ci["improvement_threshold_n"] = 0.10; ci["supported_improvement"] = bool(ci["upper"] is not None and ci["upper"] <= -0.10); paired[method] = ci
    attempts_path = out / "attempts.jsonl"
    holdout_path = out / "holdout.jsonl"
    attempts_path.write_text("".join(json.dumps(a, sort_keys=True) + "\n" for a in attempts), encoding="ascii")
    holdout_path.write_text("".join(json.dumps(a, sort_keys=True) + "\n" for a in holdout), encoding="ascii")
    summary = {"schema": SCHEMA, "claim_scope": "offline proxy only; no robot, bridge, physical acceptance, or human-push claim", "protocol": {"methods": list(METHODS), "executable_methods": list(EXECUTABLE_METHODS), "composition_methods": list(COMPOSITION_METHODS), "cases": list(CASES), "budget": {"initial": config.initial_units, "bo": config.bo_units, "repeat": config.repeat_units, "holdout_rounds": config.holdout_rounds}, "normal_force_target_n": FORCE_TARGET_N, "raw_force_limit_n": RAW_FORCE_LIMIT_N, "dt_s": DT_S}, "config": config.__dict__, "attempt_denominators": {"tuning": len(attempts), "holdout": len(holdout), "failed_tuning": sum(a["metrics"]["failed"] for a in attempts), "failed_holdout": sum(a["metrics"]["failed"] for a in holdout)}, "frozen": frozen, "paired_vs_primary": paired, "artifacts": {"attempts_sha256": _sha(attempts), "holdout_sha256": _sha(holdout)}}
    summary["artifacts"] = {
        "attempts_sha256": _file_sha256(attempts_path),
        "holdout_sha256": _file_sha256(holdout_path),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="ascii")
    return summary


__all__ = ["CampaignConfig", "METHODS", "run_attempt", "run_campaign"]
