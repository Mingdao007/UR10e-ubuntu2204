"""Frozen offline yield-recovery protocol.  No device access or live authority.

Comparison labels are fixed: SFC is the geometrically anisotropic baseline,
SFC_RADIAL is a matched ablation (not the baseline), DSFC and MSFC are
proposals.  RNN, LAC, NAC and ISFC are not in this comparison.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from contact_benchmark_protocol import (
    ENTRY_DURATION_S,
    FRESH_AGE_S,
    STALE_AGE_S,
    Task as BenchmarkTask,
    classify_sensor_age,
    quintic_entry,
)


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL_PATH = EXPERIMENT_ROOT / "config" / "contact_yield_protocol.json"
LAW_CONFIG_PATH = EXPERIMENT_ROOT / "config" / "contact_benchmark_laws.json"
QP_LIBRARY_PATH = EXPERIMENT_ROOT / "build" / "contact-qp" / "libcontact_qp.so"

SCHEMA = "ur10e.contact-yield-protocol-v2"
METHODS = ("SFC", "SFC_RADIAL", "DSFC", "MSFC")
METHOD_ROLES = {
    "SFC": "baseline_geometrically_anisotropic",
    "SFC_RADIAL": "matched_radial_ablation",
    "DSFC": "proposal",
    "MSFC": "proposal",
}
NATIVE_LAWS = {
    "SFC": "SFC",
    "SFC_RADIAL": "SFC",
    "DSFC": "DSFC",
    "MSFC": "MSFC",
}
SCENARIOS = (
    "nominal",
    "sustained_release_normal",
    "sustained_release_tangent",
    "sustained_release_oblique",
    "short_pulse_normal",
    "short_pulse_tangent",
    "short_pulse_oblique",
)
MATERIALS = ("stiff_low_mu", "compliant_high_mu")
TIMELINES = ("full_cycle", "diagnostic")
PERIOD_S = 2.0 * math.pi / 0.1
# Bounded unwrapped PATH continuation past the nominal 1 s + PERIOD_S
# endpoint when the sample grid misses the exact entry/PATH seam. Equal to
# the existing skipped-initial PATH bound (one 2 ms sample plus consume lag).
PATH_SEAM_CONTINUATION_S = 0.004
PATH_SEAM_CONTINUATION_POLICY = "unwrapped_periodic_v1"
DIAGNOSTIC_DURATION_S = 0.60
DEFAULT_DT_S = 0.002


class Task(BenchmarkTask):
    """Yield task geometry; bounded unwrapped continuation, no endpoint clamp."""

    def reference(self, time_s):
        if not math.isfinite(time_s) or time_s < 0:
            raise ValueError("time outside complete task period")
        if time_s > self.duration_s + PATH_SEAM_CONTINUATION_S:
            raise ValueError("time outside complete task period")
        a, b, w = self.along_amplitude_m, self.lateral_amplitude_m, self.omega_rad_s
        t = float(time_s)
        return {
            "position_m": (a * math.sin(w * t), b * math.sin(2 * w * t), 0.0),
            "velocity_m_s": (a * w * math.cos(w * t), 2 * b * w * math.cos(2 * w * t), 0.0),
            "acceleration_m_s2": (-a * w * w * math.sin(w * t), -4 * b * w * w * math.sin(2 * w * t), 0.0),
            "reference_force_n": self.normal_force_n,
        }
REFINEMENT_DT_S = 0.001
LATENCY_ERROR_BOUND_M = 0.001
LATENCY_EXTRA_S = 0.002
TUNING_UNITS = 24
INITIAL_UNITS = 8
BO_UNITS = 12
REPEAT_UNITS = 4
HOLDOUT_REPEATS = 5

CLAIM_SCOPE = (
    "offline simulation only; not hardware qualification, not a physical "
    "contact result, and not a declared winner among SFC/DSFC/MSFC"
)

# Scientific PATH-time schedule.  Entry is separate and not part of these clocks.
_FULL_CYCLE = {
    "short_pulse": {"start_s": 20.0, "width_s": 0.5, "hold_s": 0.0, "release_s": 0.0, "amplitude_n": 3.0},
    "sustained_release": {"start_s": 20.0, "width_s": 0.5, "hold_s": 10.0, "release_s": 5.0, "amplitude_n": 2.5},
}
# Short diagnostic PATH-time schedule.  Runner starts already in contact.
_DIAGNOSTIC = {
    "short_pulse": {"start_s": 0.24, "width_s": 0.08, "hold_s": 0.0, "release_s": 0.0, "amplitude_n": 3.0},
    "sustained_release": {"start_s": 0.22, "width_s": 0.04, "hold_s": 0.12, "release_s": 0.04, "amplitude_n": 2.5},
}


def _sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def method_role(method: str) -> str:
    if method not in METHOD_ROLES:
        raise ValueError(f"unknown yield-recovery method {method!r}")
    return METHOD_ROLES[method]


def native_law_label(method: str) -> str:
    if method not in NATIVE_LAWS:
        raise ValueError(f"unknown yield-recovery method {method!r}")
    return NATIVE_LAWS[method]


def parse_scenario(name: str, *, timeline: str = "full_cycle") -> dict[str, Any]:
    if name not in SCENARIOS:
        raise ValueError(f"unknown scenario {name!r}")
    if timeline not in TIMELINES:
        raise ValueError(f"unknown timeline {timeline!r}")
    if name == "nominal":
        return {
            "name": name,
            "kind": "nominal",
            "direction": None,
            "timeline": timeline,
            "start_s": None,
            "width_s": None,
            "hold_s": None,
            "release_s": None,
            "amplitude_n": 0.0,
        }
    kind = "short_pulse" if name.startswith("short_pulse") else "sustained_release"
    direction = name.rsplit("_", 1)[-1]
    if direction not in ("normal", "tangent", "oblique"):
        raise ValueError(f"scenario direction missing in {name!r}")
    table = _DIAGNOSTIC if timeline == "diagnostic" else _FULL_CYCLE
    return {"name": name, "kind": kind, "direction": direction, "timeline": timeline, **table[kind]}


def material_spec(name: str) -> dict[str, float]:
    if name == "stiff_low_mu":
        return {
            "name": name,
            "stiffness_n_per_m": 8000.0,
            "damping_n_s_per_m": 80.0,
            "friction_mu": 0.15,
            "regularization_m_s": 0.002,
        }
    if name == "compliant_high_mu":
        return {
            "name": name,
            "stiffness_n_per_m": 2000.0,
            "damping_n_s_per_m": 40.0,
            "friction_mu": 0.40,
            "regularization_m_s": 0.002,
        }
    raise ValueError(f"unknown material {name!r}")


def perturbation_envelope(
    kind: str,
    time_s: float,
    *,
    start_s: float,
    width_s: float,
    hold_s: float,
    release_s: float = 0.0,
) -> float:
    """Raised-cosine pulse or plateau.  Continuous at the boundaries."""
    if kind == "nominal":
        return 0.0
    t = float(time_s)
    if kind == "short_pulse":
        x = (t - start_s) / width_s
        return 0.5 * (1.0 - math.cos(2.0 * math.pi * x)) if 0.0 < x < 1.0 else 0.0
    rise_end = start_s + width_s
    hold_end = rise_end + hold_s
    fall = release_s if release_s > 0.0 else width_s
    fall_end = hold_end + fall
    if start_s < t < rise_end:
        x = (t - start_s) / width_s
        return 0.5 * (1.0 - math.cos(math.pi * x))
    if rise_end <= t <= hold_end:
        return 1.0
    if hold_end < t < fall_end:
        x = (t - hold_end) / fall
        return 0.5 * (1.0 + math.cos(math.pi * x))
    return 0.0


def evaluation_budget() -> dict[str, Any]:
    return {
        "per_method_units": TUNING_UNITS,
        "initial_units": INITIAL_UNITS,
        "bo_units": BO_UNITS,
        "repeat_units": REPEAT_UNITS,
        "trials_per_unit": ["nominal", "disturbed"],
        "failed_attempt_consumes_unit": True,
        "holdout_used_for_tuning": False,
        "holdout_repeats": HOLDOUT_REPEATS,
        "diagnostic_seed_consumes_formal_budget": False,
    }


def protocol() -> dict[str, Any]:
    task = Task()
    result = {
        "schema": SCHEMA,
        "version": 1,
        "offline_only": True,
        "hardware_qualified": False,
        "endpoint": "none",
        "simulation_only": True,
        "claim_scope": CLAIM_SCOPE,
        "methods": list(METHODS),
        "method_roles": dict(METHOD_ROLES),
        "native_laws": dict(NATIVE_LAWS),
        "excluded": ["LAC", "NAC", "ISFC", "RNN", "RPSFC"],
        "sfc_baseline_geometry": (
            "axis-decoupled componentwise SFC in a fixed base frame; "
            "geometrically anisotropic; this label is the baseline"
        ),
        "sfc_radial_ablation": (
            "matched (m, mu, n, g) vector/radial SFC; not the baseline; "
            "does not rename original SFC"
        ),
        "task": {
            **asdict(task),
            "duration_s": task.duration_s,
            "seam_continuation_policy": PATH_SEAM_CONTINUATION_POLICY,
            "maximum_seam_continuation_s": PATH_SEAM_CONTINUATION_S,
            "span_mm": [80.0, 20.0],
            "orientation": "compliant_changing_estimated_inward_normal",
            "unknown_surface": True,
            "controller_receives_true_normal": False,
            "controller_receives_true_geometry": False,
        },
        "entry": {
            "profile": "quintic_h=-4s^3+7s^4-3s^5",
            "duration_s": ENTRY_DURATION_S,
            "terminal_velocity_m_s": list(task.initial_velocity_m_s),
            "separate_from_formal_path": True,
        },
        "timing": {
            "dt_s": DEFAULT_DT_S,
            "refinement_dt_s": REFINEMENT_DT_S,
            "full_cycle_s": PERIOD_S,
            "diagnostic_duration_s": DIAGNOSTIC_DURATION_S,
            "elapsed_dt_interval_s": [0.0, 0.004],
            "elapsed_dt_open_at_zero": True,
            "path_clock_must_advance_by_actual_dt": True,
        },
        "timelines": {
            "full_cycle": {
                "entry_s": ENTRY_DURATION_S,
                "path_s": PERIOD_S,
                "perturbations": _FULL_CYCLE,
                "note": "PATH-time schedule; entry is separate; sustained hold is 10 s",
            },
            "diagnostic": {
                "entry_s": ENTRY_DURATION_S,
                "path_s": DIAGNOSTIC_DURATION_S,
                "starts_in_contact": True,
                "perturbations": _DIAGNOSTIC,
                "note": "short seed only; not the scientific 62.83 s cycle",
            },
        },
        "scenarios": [parse_scenario(name, timeline="full_cycle") for name in SCENARIOS],
        "materials": [material_spec(name) for name in MATERIALS],
        "freshness": {
            "fresh_age_s": FRESH_AGE_S,
            "stale_age_s": STALE_AGE_S,
            "held_policy": "latest_value_zero_order_hold_no_interpolation",
            "stale_policy": "fail_closed_rollback_tick",
            "cached_held_is_not_marked_fresh": True,
        },
        "geometric_latency": {
            "bound_m": LATENCY_ERROR_BOUND_M,
            "extra_hold_s": LATENCY_EXTRA_S,
            "independent_from_freshness_80ms": True,
            "speed_model": "conservative_cap_plus_reference",
        },
        "guards": {
            "raw_force_limit_n": 20.0,
            "raw_torque_limit_nm": 2.0,
            "raw_sensor_guard_precedes_injection": True,
        },
        "normal_estimator": {
            "initialize_from": "contact_approach",
            "update": "measured_local_motion_tangent_constraint",
            "manifold": "projected_gradient_unit_sphere",
            "gating": ["excitation", "contact"],
            "force_direction_correction": "bounded_friction_bias_only",
            "true_normal_available_to_controller": False,
        },
        "law_frame": "fixed_base",
        "law_input": "full_base_force_residual_plus_tangent_restoring",
        "independent_normal_p_controller": False,
        "feedforward_in_nonlinear_damping": False,
        "reset_memory_on_perturbation": False,
        "msfc_memory_through_release": True,
        "integral_default": 0.0,
        "shared_path_stiffness_n_per_m": 120.0,
        "command_integral_spring_default_n_per_m": 0.0,
        "qp": {
            "backend": "native_equality_box",
            "infeasible_fallback": "measured_task_scaling",
            "priorities": ["preserve_normal_unloading", "scale_tangent_progress"],
            "clock_frozen_on_scaling": False,
            "feasibility_force_guarantee": False,
            "deadline_or_nonfinite_is_not_infeasibility": True,
        },
        "plant": {
            "prefer_ur10e_kinematics_jacobian": True,
            "cartesian_fallback_label": (
                "simplified Cartesian servo; not full robot qualification"
            ),
            "unilateral_contact": True,
            "regularized_coulomb_friction": True,
            "finite_servo_lag": True,
            "cli_default_integration_substeps": 8,
            "integration_substeps_are_not_numerical_qualification": True,
            "software_injection_is_human_evidence": False,
        },
        "campaign": {
            "diagnostic_seed": "uses_frozen_seed_parameters_short_horizon",
            "formal_tuned": "24_paired_units_then_separate_holdout",
            "budget": evaluation_budget(),
            "no_pretend_closure": True,
            "no_cherry_pick": True,
            "no_declared_winner": True,
        },
        "metrics": [
            "mae",
            "rmse",
            "peak",
            "overlimit_duration",
            "path_error",
            "planned_progress",
            "measured_progress",
            "orientation",
            "yield",
            "recoil",
            "residual",
            "recovery",
            "contact_loss",
            "saturation",
            "qp_intervention",
            "failures",
        ],
        "rpsfc_selectable": False,
        "rnn_selectable": False,
        "research_parameters_are_contact_qualified": False,
        "synthetic_disturbance_is_physical_impact": False,
    }
    result["sha256"] = _sha256({key: value for key, value in result.items() if key != "sha256"})
    return result


def load_protocol(path: Path | str = DEFAULT_PROTOCOL_PATH) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    generated = protocol()
    stored = dict(payload)
    stored_hash = stored.pop("sha256", None)
    recomputed = _sha256(stored)
    if stored_hash != recomputed or stored_hash != generated["sha256"]:
        raise ValueError("contact_yield_protocol.json does not match the Python protocol identity")
    if payload["methods"] != list(METHODS):
        raise ValueError("protocol method set drifted")
    return payload


def law_seed_parameters(method: str) -> dict[str, float]:
    native = native_law_label(method)
    root = json.loads(LAW_CONFIG_PATH.read_text(encoding="utf-8"))
    spec = root["laws"][native]["parameters"]
    return {str(key): float(value) for key, value in spec.items()}


__all__ = [
    "CLAIM_SCOPE",
    "DEFAULT_DT_S",
    "DEFAULT_PROTOCOL_PATH",
    "DIAGNOSTIC_DURATION_S",
    "ENTRY_DURATION_S",
    "EXPERIMENT_ROOT",
    "FRESH_AGE_S",
    "HOLDOUT_REPEATS",
    "LATENCY_ERROR_BOUND_M",
    "LATENCY_EXTRA_S",
    "LAW_CONFIG_PATH",
    "MATERIALS",
    "METHODS",
    "METHOD_ROLES",
    "PATH_SEAM_CONTINUATION_POLICY",
    "PATH_SEAM_CONTINUATION_S",
    "PERIOD_S",
    "QP_LIBRARY_PATH",
    "REFINEMENT_DT_S",
    "SCENARIOS",
    "SCHEMA",
    "STALE_AGE_S",
    "TIMELINES",
    "Task",
    "classify_sensor_age",
    "evaluation_budget",
    "law_seed_parameters",
    "load_protocol",
    "material_spec",
    "method_role",
    "native_law_label",
    "parse_scenario",
    "perturbation_envelope",
    "protocol",
    "quintic_entry",
]
