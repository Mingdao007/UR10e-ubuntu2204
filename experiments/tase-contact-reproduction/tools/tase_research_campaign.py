"""Offline TASE comparison and frozen holdout campaign.

The campaign is deliberately separate from the live-entry registry. Every
method uses the same measured-input schema, common online normal observer, and
joint-velocity box; paired holdout methods also share the same seeded proxy
disturbance. A small deterministic contact-plant proxy supplies response
variation for software testing; it is never presented as a UR10e measurement
or a surface model. Unknown or unavailable compositions remain failed attempts
in the denominator and are never substituted or sent to live.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
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
    "TASE_QP+SFC",
)
METHODS = EXECUTABLE_METHODS + COMPOSITION_METHODS
CASES = ("plane", "incline", "low_curvature", "stiffness_change")
DT_S = 0.002
FORCE_TARGET_N = 5.0
RAW_FORCE_LIMIT_N = 20.0
QDOT_LIMIT_RAD_S = 0.05
FIXED_PROXY_ROTATION = np.diag((1.0, -1.0, -1.0))


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
    stiffness_initial_n_per_m: float
    stiffness_final_n_per_m: float | None = None


SURFACE_CASES = {
    "plane": SurfaceCase("plane", "fixed plane; measured-wrench normal only", (0.0, 0.0, 1.0), 120.0),
    "incline": SurfaceCase("incline", "10 degree nominal incline; no geometry input", (0.173648, 0.0, 0.984808), 120.0),
    "low_curvature": SurfaceCase("low_curvature", "slowly varying low-curvature normal proxy", (0.0, 0.0, 1.0), 120.0),
    "stiffness_change": SurfaceCase("stiffness_change", "piecewise stiffness change; no curvature input", (0.0, 0.0, 1.0), 120.0, 260.0),
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


def _normal_observer_profile() -> tuple[str, dict[str, Any]]:
    path = Path(__file__).resolve().parents[1] / "config" / "yield_normal_observer_v3.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return "yield_normal_observer_v3", dict(payload)


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


def _matched_rnn_best_outer_profile() -> tuple[str, dict[str, Any]]:
    """Freeze the shared outer loop at the accepted RNN incumbent for this study."""
    path = Path(__file__).resolve().parents[1] / "config" / "tase_figure8_integral_0p1_rate400.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    frozen = document["frozen"]
    config = {
        "kp": float(frozen["kp"]),
        "ko": float(frozen["ko"]),
        "kf": float(frozen["kf"]),
        "Md_scalar": float(document["Md_scalar"]),
        "Bd_scalar": float(document["Bd_scalar"]),
        "force_target_n": float(frozen["force_target_n"]),
        "force_integral_limit_n_s": float(frozen["force_integral_limit_n_s"]),
        "force_integral_policy": str(frozen["force_integral_policy"]),
        "force_integral_authority_error_n": float(frozen["force_integral_authority_error_n"]),
        "force_sign_convention": str(frozen["force_sign_convention"]),
        "delay_T_s": 0.004,
    }
    return str(document["candidate_id"]), config


def _candidate(method: str, index: int) -> dict[str, Any]:
    """Return a deterministic point in the method's offline search space."""
    outer_profile_id, outer_config = _matched_rnn_best_outer_profile()
    if method == "TASE_RNN":
        return {"epsilon": 0.014 + 0.0015 * (index % 8), "sigr_exponent_r": 0.2 + 0.05 * ((index // 8) % 4), "lambda_update_sign": "plus", "outer_profile_id": outer_profile_id, "normal_outer_config": outer_config}
    if method in {"TASE_RNN_MATURE", "TASE_RNN_MATURE_MINUS"}:
        return {"epsilon": 0.014 + 0.0015 * (index % 8), "sigr_exponent_r": 0.2 + 0.05 * ((index // 8) % 4), "lambda_update_sign": "minus", "outer_profile_id": outer_profile_id, "normal_outer_config": outer_config}
    if method == "TASE_QP":
        return {"lambda_update_sign": "minus", "outer_profile_id": outer_profile_id, "normal_outer_config": outer_config}
    if method == "TASE_IMPROVED":
        return {"force_integral_limit_n_s": 0.5 + 0.25 * (index % 5), "force_contact_gate_n": 0.25 + 0.25 * ((index // 5) % 3), "force_integral_leak_tau_s": 0.25 + 0.125 * ((index // 15) % 4), "normal_weight": 80.0 + 20.0 * ((index // 60) % 4), "outer_profile_id": outer_profile_id, "normal_outer_config": outer_config}
    normal_controller, tangential_controller = method.split("+", 1)
    law_config_path = Path(__file__).resolve().parents[1] / "config" / "contact_benchmark_laws.json"
    law_config = json.loads(law_config_path.read_text(encoding="utf-8"))
    law = law_config["laws"][tangential_controller]
    return {
        "normal_controller": normal_controller,
        "tangential_controller": tangential_controller,
        "tangential_parameters": dict(law["parameters"]),
        "tangential_parameter_source": "config/contact_benchmark_laws.json",
        "tangential_parameter_status": law["parameter_status"],
        "path_stiffness_n_per_m": 120.0,
        "initial_normal": [0.0, 0.0, 1.0],
        "dt_s": DT_S,
        "outer_profile_id": outer_profile_id,
        "normal_outer_config": outer_config,
    }


def _candidate_pool(method: str) -> list[dict[str, Any]]:
    if method == "TASE_QP":
        # The matched native QP has no tunable solver parameter in this
        # transport-free build; BO therefore has one admissible point and
        # records repeated acquisition of that fixed identity explicitly.
        return [_candidate(method, 0)]
    if method in COMPOSITION_METHODS:
        return [_candidate(method, 0)]
    return [_candidate(method, index) for index in range(32 if method != "TASE_IMPROVED" else 240)]


def _candidate_key(candidate: Mapping[str, Any]) -> str:
    return json.dumps(dict(candidate), sort_keys=True, separators=(",", ":"))


def _candidate_distance(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    scales = {
        "epsilon": 0.01, "sigr_exponent_r": 0.2,
        "force_integral_limit_n_s": 1.0, "force_contact_gate_n": 1.0,
        "force_integral_leak_tau_s": 0.5, "normal_weight": 100.0,
    }
    distance = 0.0
    for key in set(left) | set(right):
        a, b = left.get(key), right.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            distance += ((float(a) - float(b)) / scales.get(key, 1.0)) ** 2
        elif a != b:
            distance += 1.0
    return math.sqrt(distance)


def _propose_bo(method: str, observed: list[dict[str, Any]], proposal_index: int) -> dict[str, Any]:
    """Deterministic Gaussian-process lower-confidence-bound proposal."""
    pool = _candidate_pool(method)
    used = {_candidate_key(item["candidate"]) for item in observed if item.get("budget_stage") in {"initial", "bo"}}
    available = [candidate for candidate in pool if _candidate_key(candidate) not in used]
    if not available:
        return dict(pool[proposal_index % len(pool)])
    grouped: dict[str, list[float]] = {}
    candidates_by_key: dict[str, Mapping[str, Any]] = {}
    for item in observed:
        if item.get("metrics", {}).get("failed") or item.get("metrics", {}).get("normal_force_mae_n") is None:
            continue
        key = _candidate_key(item["candidate"])
        grouped.setdefault(key, []).append(float(item["metrics"]["normal_force_mae_n"]))
        candidates_by_key[key] = item["candidate"]
    if not grouped:
        return dict(available[proposal_index % len(available)])
    observations = [(candidates_by_key[key], float(np.mean(values))) for key, values in grouped.items()]
    points = [point for point, _ in observations]
    scores = np.asarray([score for _, score in observations], dtype=float)
    prior = float(np.mean(scores))
    length_scale = 0.45
    observation_noise = 0.01
    kernel = np.asarray(
        [[math.exp(-(_candidate_distance(left, right) ** 2) / (2.0 * length_scale ** 2)) for right in points] for left in points],
        dtype=float,
    )
    system = kernel + (observation_noise ** 2) * np.eye(len(points))
    alpha = np.linalg.solve(system, scores - prior)
    def acquisition(candidate: Mapping[str, Any]) -> tuple[float, str]:
        cross = np.asarray([math.exp(-(_candidate_distance(candidate, point) ** 2) / (2.0 * length_scale ** 2)) for point in points])
        prediction = prior + float(cross @ alpha)
        variance = max(0.0, 1.0 - float(cross @ np.linalg.solve(system, cross)))
        return prediction - 1.96 * math.sqrt(variance), _candidate_key(candidate)
    return dict(min(available, key=acquisition))


def _make_observation(*, case: SurfaceCase, position: np.ndarray, normal: np.ndarray, force_n: float, index: int, horizon: int, dt_s: float, stiffness: float, rng: random.Random, linear_velocity: np.ndarray | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    task = Task()
    t = index * dt_s
    reference = task.reference(min(t, task.duration_s))
    sensor_noise = np.asarray(tuple(0.015 * rng.uniform(-1.0, 1.0) for _ in range(3)))
    disturbance = _disturbance(case, index, horizon, rng)
    if case.name == "stiffness_change" and stiffness < 200.0:
        disturbance = np.zeros(3)
    measured_force = normal * force_n + disturbance + sensor_noise
    observation = {
        "time_s": float(t), "state_age_s": 0.010 if index % 5 else 0.018,
        # Fixed identity pose is an external proxy assumption; it does not
        # encode the evaluator's nominal normal in the controller input.
        "position_m": tuple(float(x) for x in position), "rotation": FIXED_PROXY_ROTATION,
        "joint_position_rad": (0.0,) * 6, "jacobian": np.eye(6),
        "raw_force_base_n": tuple(float(x) for x in measured_force),
        "raw_torque_base_nm": (0.0, 0.0, 0.0), "joint_velocity_lower": (-QDOT_LIMIT_RAD_S,) * 6,
        "joint_velocity_upper": (QDOT_LIMIT_RAD_S,) * 6,
        "linear_velocity_base_m_s": tuple(float(x) for x in (linear_velocity if linear_velocity is not None else np.zeros(3))), "angular_velocity_base_rad_s": (0.0, 0.0, 0.0),
        # This is a measured-wrench direction estimate, not supplied surface geometry.
        "local_normal_base": tuple(float(x) for x in _unit(measured_force)),
        "software_injection_base_n": (0.0, 0.0, 0.0),
    }
    ref = {"position_m": reference["position_m"], "velocity_m_s": reference["velocity_m_s"], "reference_force_n": FORCE_TARGET_N}
    return observation, ref


def run_attempt(*, method: str, candidate: Mapping[str, Any], case_name: str, attempt_id: str, config: CampaignConfig, qp_library: Path, trial_key: str | None = None) -> dict[str, Any]:
    case = SURFACE_CASES[case_name]
    registry = default_registry()
    rows: list[dict[str, Any]] = []
    position = np.zeros(3, dtype=float)
    linear_velocity = np.zeros(3, dtype=float)
    force_n = FORCE_TARGET_N
    rng = random.Random(config.seed ^ int(_sha({"trial": trial_key or attempt_id})[:8], 16))
    observer_profile_id, observer_parameters = _normal_observer_profile()
    from contact_yield_normal import NormalEstimator
    normal_estimator = NormalEstimator(
        (0.0, 0.0, -1.0),
        initial_inward_normal_base=(0.0, 0.0, -1.0),
        **observer_parameters,
    )
    normal_updates: list[dict[str, Any]] = []
    handle = None
    error: str | None = None
    composition_diagnostics: list[dict[str, Any]] = []
    try:
        options = {"qp_library": qp_library}
        if method in EXECUTABLE_METHODS:
            options["config"] = {
                key: value for key, value in candidate.items()
                if key not in {"normal_outer_config", "outer_profile_id"}
            }
            options["outer_config"] = candidate.get("normal_outer_config")
        elif method in COMPOSITION_METHODS:
            options["config"] = dict(candidate)
            options["outer_config"] = candidate.get("normal_outer_config")
        else:
            options["config"] = dict(candidate)
        handle = registry.initialize(method, **options)
        for index in range(config.horizon_ticks):
            normal = _normal_for(case, index, config.horizon_ticks)
            stiffness = case.stiffness_initial_n_per_m
            if case_name == "stiffness_change" and index >= config.horizon_ticks // 2:
                stiffness = case.stiffness_final_n_per_m or stiffness
            observation, reference = _make_observation(case=case, position=position, normal=normal, force_n=force_n, index=index, horizon=config.horizon_ticks, dt_s=DT_S, stiffness=stiffness, rng=rng, linear_velocity=linear_velocity)
            raw_force = np.asarray(observation["raw_force_base_n"], dtype=float)
            normal_update = normal_estimator.update(
                dt_s=DT_S,
                measured_linear_velocity_base_m_s=linear_velocity,
                measured_force_base_n=raw_force,
                in_contact=float(np.linalg.norm(raw_force)) >= 1.0,
            )
            estimated_outward_normal = -np.asarray(normal_update["inward_normal_base"], dtype=float)
            observation["local_normal_base"] = tuple(float(value) for value in estimated_outward_normal)
            normal_updates.append(dict(normal_update))
            result = handle.step(observation, reference, DT_S)
            composition = result.get("diagnostics", {}).get("composition")
            if isinstance(composition, Mapping):
                composition_diagnostics.append(dict(composition))
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
            rows.append({"time_s": index * DT_S, "dt_s": DT_S, "age_s": observation["state_age_s"], "normal_force_n": force_n, "normal_error_n": force_n - FORCE_TARGET_N, "force_norm_n": float(np.linalg.norm(observation["raw_force_base_n"])), "path_error_m": path_error, "qdot_norm_rad_s": float(np.linalg.norm(qdot)), "saturated": bool(np.any(np.isclose(np.abs(qdot), QDOT_LIMIT_RAD_S, atol=1e-8))), "normal_velocity_m_s": normal_velocity, "realization_residual_norm": realization_residual, "normal_estimate_base": estimated_outward_normal.tolist(), "normal_motion_update_applied": bool(normal_update["motion_update_applied"]), "normal_coplanarity_update_applied": bool(normal_update.get("coplanarity_update_applied", False)), "stiffness_n_per_m": stiffness})
            position = position + DT_S * actual_twist[:3]
            linear_velocity = actual_twist[:3].copy()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if handle is not None:
            handle.close()
    metrics = _metrics(rows, failed=error is not None or len(rows) < config.horizon_ticks, error=error)
    composition_summary = None
    if composition_diagnostics:
        normal_leaks = []
        tangent_shadow_norms = []
        for item in composition_diagnostics:
            normal = np.asarray(item["normal"], dtype=float)
            sfc_twist = np.asarray(item["sfc_tangent_twist_m_s_rad_s"], dtype=float)
            tangent_shadow = np.asarray(item["tase_tangent_shadow_m_s"], dtype=float)
            normal_leaks.append(abs(float(np.dot(normal, sfc_twist[:3]))))
            tangent_shadow_norms.append(float(np.linalg.norm(tangent_shadow)))
        composition_summary = {
            "composition_id": composition_diagnostics[-1]["composition_id"],
            "normal_solver": composition_diagnostics[-1]["normal_solver"],
            "final_realizer": composition_diagnostics[-1]["final_realizer"],
            "final_realization_calls_per_tick": sorted({int(item["final_realization_calls"]) for item in composition_diagnostics}),
            "sfc_active_ticks": sum(bool(item["sfc_enabled"]) for item in composition_diagnostics),
            "sfc_normal_leak_max_m_s": max(normal_leaks, default=0.0),
            "tase_tangent_shadow_max_m_s": max(tangent_shadow_norms, default=0.0),
            "tangential_force_input_max_n": max(
                (float(np.linalg.norm(item["tangential_force_input_n"])) for item in composition_diagnostics),
                default=0.0,
            ),
            "sfc_input_max_n": max(
                (float(np.linalg.norm(item["sfc_input_n"])) for item in composition_diagnostics),
                default=0.0,
            ),
            "sfc_law_velocity_max_m_s": max(
                (float(np.linalg.norm(item["sfc_law_velocity_m_s"])) for item in composition_diagnostics),
                default=0.0,
            ),
            "sfc_law_identity": composition_diagnostics[-1]["sfc_law_identity"],
            "sfc_build_fingerprint": composition_diagnostics[-1]["sfc_build_fingerprint"],
            "sfc_parameters": composition_diagnostics[-1]["sfc_parameters"],
            "path_stiffness_n_per_m": composition_diagnostics[-1]["path_stiffness_n_per_m"],
        }
        metrics["composition"] = composition_summary
    return {
        "schema": ATTEMPT_SCHEMA,
        "attempt_id": attempt_id,
        "method": method,
        "controller_identity": asdict(handle.spec) if handle is not None else None,
        "candidate": dict(candidate),
        "case": case_name,
        "trial_key": trial_key or attempt_id,
        "proxy_only": True,
        "surface_geometry_provided_to_controller": False,
        "normal_observer": {
            "profile_id": observer_profile_id,
            "parameters": observer_parameters,
            "initial_outward_normal_base": [0.0, 0.0, 1.0],
            "ticks": len(normal_updates),
            "motion_update_ticks": sum(bool(item["motion_update_applied"]) for item in normal_updates),
            "coplanarity_update_ticks": sum(bool(item.get("coplanarity_update_applied", False)) for item in normal_updates),
            "final_outward_normal_base": rows[-1]["normal_estimate_base"] if rows else [0.0, 0.0, 1.0],
        },
        "composition_summary": composition_summary,
        "metrics": metrics,
    }


def _ci95(values: list[tuple[str, float]], *, seed: int) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean": None, "lower": None, "upper": None}
    groups: dict[str, list[float]] = {}
    for case, value in values:
        groups.setdefault(case, []).append(float(value))
    case_means = [float(np.mean(group)) for group in groups.values()]
    mean = float(np.mean(case_means))
    rng = np.random.default_rng(seed)
    bootstrap = np.empty(10000, dtype=float)
    for index in range(len(bootstrap)):
        bootstrap[index] = float(np.mean([
            np.mean(rng.choice(group, size=len(group), replace=True))
            for group in groups.values()
        ]))
    return {"n": len(values), "mean": mean, "lower": float(np.percentile(bootstrap, 2.5)), "upper": float(np.percentile(bootstrap, 97.5)), "case_count": len(groups), "case_means": case_means, "ci_method": "stratified paired bootstrap percentile; 10000 resamples; equal case weight"}


def run_campaign(*, output_dir: Path, qp_library: Path, config: CampaignConfig = CampaignConfig()) -> dict[str, Any]:
    out = Path(output_dir).resolve()
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty output: {out}")
    out.mkdir(parents=True, exist_ok=True)
    observer_profile_id, observer_parameters = _normal_observer_profile()
    attempts: list[dict[str, Any]] = []
    frozen: dict[str, dict[str, Any]] = {}

    def run_tuning_unit(method: str, candidate: Mapping[str, Any], stage: str, unit: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for case_name in CASES:
            attempt = run_attempt(
                method=method,
                candidate=candidate,
                case_name=case_name,
                attempt_id=f"{method}-{stage}-{unit:02d}-{case_name}",
                trial_key=f"tuning:{method}:{stage}:{unit}:{case_name}",
                config=config,
                qp_library=Path(qp_library),
            )
            attempt["budget_stage"] = stage
            attempt["candidate_unit"] = unit
            rows.append(attempt)
        return rows

    for method in METHODS:
        training: list[dict[str, Any]] = []
        for unit in range(config.initial_units):
            candidate = _candidate(method, unit)
            unit_rows = run_tuning_unit(method, candidate, "initial", unit)
            attempts.extend(unit_rows)
            training.extend(unit_rows)
        for bo_index in range(config.bo_units):
            candidate = _propose_bo(method, training, bo_index)
            unit = config.initial_units + bo_index
            unit_rows = run_tuning_unit(method, candidate, "bo", unit)
            attempts.extend(unit_rows)
            training.extend(unit_rows)
        grouped: dict[str, list[float]] = {}
        candidate_by_key: dict[str, Mapping[str, Any]] = {}
        for item in training:
            metrics = item["metrics"]
            if metrics["failed"] or metrics["normal_force_mae_n"] is None:
                continue
            key = _candidate_key(item["candidate"])
            grouped.setdefault(key, []).append(float(metrics["normal_force_mae_n"]))
            candidate_by_key[key] = item["candidate"]
        if grouped:
            selected_key = min(grouped, key=lambda key: (float(np.mean(grouped[key])), key))
            selected = dict(candidate_by_key[selected_key])
            source = "history_lcb_bo_case_balanced_mean"
        else:
            selected = _candidate(method, 0)
            source = "no_eligible_training_attempt"
        frozen[method] = {
            "candidate": dict(selected),
            "source": source,
            "eligible_training_attempts": sum(len(values) for values in grouped.values()),
            "eligible_training_candidates": len(grouped),
            "search_space_cardinality": len(_candidate_pool(method)),
        }
        for repeat in range(config.repeat_units):
            unit_rows = run_tuning_unit(method, selected, "repeat", repeat)
            attempts.extend(unit_rows)
    holdout: list[dict[str, Any]] = []
    for block in range(config.holdout_rounds):
        order = list(METHODS); random.Random(config.seed + block).shuffle(order)
        for method in order:
            for case_name in CASES:
                attempt = run_attempt(
                    method=method,
                    candidate=frozen[method]["candidate"],
                    case_name=case_name,
                    attempt_id=f"holdout-{block:02d}-{method}-{case_name}",
                    trial_key=f"holdout:{block}:{case_name}",
                    config=config,
                    qp_library=Path(qp_library),
                )
                attempt["budget_stage"] = "frozen_holdout"
                attempt["block"] = block
                holdout.append(attempt)
    paired: dict[str, Any] = {}
    baseline = [a for a in holdout if a["method"] == PRIMARY_METHOD]
    base_map = {(a["block"], a["case"]): a for a in baseline}
    for method in METHODS:
        if method == PRIMARY_METHOD:
            continue
        diffs: list[tuple[str, float]] = []
        for a in holdout:
            key = (a["block"], a["case"])
            b = base_map.get(key)
            if a["method"] == method and b is not None and not a["metrics"]["failed"] and not b["metrics"]["failed"]:
                diffs.append((a["case"], float(a["metrics"]["normal_force_mae_n"]) - float(b["metrics"]["normal_force_mae_n"])))
        ci = _ci95(diffs, seed=config.seed ^ int(_sha({"ci_method": method})[:8], 16)); ci["improvement_threshold_n"] = 0.10; ci["supported_improvement"] = bool(ci["upper"] is not None and ci["upper"] <= -0.10); paired[method] = ci
    attempts_path = out / "attempts.jsonl"
    holdout_path = out / "holdout.jsonl"
    attempts_path.write_text("".join(json.dumps(a, sort_keys=True) + "\n" for a in attempts), encoding="ascii")
    holdout_path.write_text("".join(json.dumps(a, sort_keys=True) + "\n" for a in holdout), encoding="ascii")
    summary = {
        "schema": SCHEMA,
        "claim_scope": "offline proxy only; no robot, bridge, physical acceptance, or human-push claim",
        "protocol": {
            "methods": list(METHODS),
            "executable_methods": list(EXECUTABLE_METHODS),
            "composition_methods": list(COMPOSITION_METHODS),
            "cases": list(CASES),
            "budget": {"initial": config.initial_units, "bo": config.bo_units, "repeat": config.repeat_units, "holdout_rounds": config.holdout_rounds},
            "cases_per_tuning_unit": len(CASES),
            "bo_acquisition": "history-dependent Gaussian-process RBF lower-confidence bound on case-balanced mean MAE",
            "ci_estimand": "macro-average paired normal-force MAE difference over available cases",
            "ci_method": "stratified paired bootstrap percentile; 10000 resamples; equal case weight",
            "paired_holdout_seed_contract": "holdout:block:case shared across methods",
            "paired_holdout_units": config.holdout_rounds * len(CASES),
            "common_normal_observer": {
                "profile_id": observer_profile_id,
                "parameters": observer_parameters,
                "input": "measured force and realized Cartesian velocity only; no surface geometry or evaluator truth",
            },
            "normal_force_target_n": FORCE_TARGET_N,
            "raw_force_limit_n": RAW_FORCE_LIMIT_N,
            "dt_s": DT_S,
        },
        "config": config.__dict__,
        "attempt_denominators": {"tuning": len(attempts), "holdout": len(holdout), "failed_tuning": sum(a["metrics"]["failed"] for a in attempts), "failed_holdout": sum(a["metrics"]["failed"] for a in holdout)},
        "frozen": frozen,
        "paired_vs_primary": paired,
        "artifacts": {"attempts_sha256": _sha(attempts), "holdout_sha256": _sha(holdout)},
    }
    summary["artifacts"] = {
        "attempts_sha256": _file_sha256(attempts_path),
        "holdout_sha256": _file_sha256(holdout_path),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="ascii")
    return summary


__all__ = ["CampaignConfig", "METHODS", "run_attempt", "run_campaign"]
