"""Shared yield-recovery step(observation, reference, dt).

The selected native law consumes the full base force residual
``measured - target * outward`` plus shared tangent restoring.  Correction
velocity is projected into normal/tangent after the law step.  Nominal
feedforward is added afterwards and is not a damping input.  There is no
independent normal P bypass.  Failures restore the whole tick.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from contact_laws import SNAPSHOT_SIZE, ContactLawSnapshot
from contact_semantics import finite_vector3, finite_vector6
from contact_yield_laws import YieldLaw
from contact_yield_math import (
    YieldMathError,
    attitude_error_omega,
    cap_vector,
    finite_scalar,
    optional_finite_time,
    projector_tangent,
    require_bool,
    require_rotation,
    require_unit_vector,
    transported_roll_anchor,
)
from contact_yield_normal import NormalEstimator
from contact_yield_protocol import (
    CLAIM_SCOPE,
    FRESH_AGE_S,
    LATENCY_ERROR_BOUND_M,
    LATENCY_EXTRA_S,
    STALE_AGE_S,
    Task,
    classify_sensor_age,
    method_role,
)
from contact_yield_qp import YieldQp, YieldQpError, YieldQpFatal


class YieldControllerError(RuntimeError):
    pass


@dataclass(frozen=True)
class YieldSettings:
    filter_tau_s: float = 0.02
    path_stiffness_n_per_m: float = 120.0
    # Restoring uses measured path error. A spring on integrated COMMAND
    # correction leaves a bias when plant disturbances move the actual TCP.
    # Preserve the optional historical term for exact artifact replay/ablation.
    compliance_stiffness_n_per_m: float = 0.0
    integral_force_gain: float = 0.0
    integral_limit_n_s: float = 2.0
    normal_speed_cap_m_s: float = 0.003
    tangent_speed_cap_m_s: float = 0.01
    angular_speed_cap_rad_s: float = 0.05
    orientation_kp_s_inv: float = 4.0
    raw_force_limit_n: float = 20.0
    raw_torque_limit_nm: float = 2.0
    contact_force_n: float = 1.0
    target_force_n: float = 5.0
    latency_error_bound_m: float = LATENCY_ERROR_BOUND_M
    latency_extra_s: float = LATENCY_EXTRA_S

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            number = finite_scalar(value, name)
            if number < 0.0:
                raise YieldControllerError(f"{name} must be nonnegative")
            object.__setattr__(self, name, number)
        if self.filter_tau_s <= 0.0 or self.target_force_n <= 0.0:
            raise YieldControllerError("filter_tau_s and target_force_n must be positive")
        if self.latency_error_bound_m <= 0.0:
            raise YieldControllerError("latency_error_bound_m must be positive")
        if self.integral_force_gain != 0.0 and self.integral_limit_n_s <= 0.0:
            raise YieldControllerError("nonzero integral requires a positive antiwindup limit")


def _snapshot_law(snapshot: ContactLawSnapshot) -> dict[str, Any]:
    return {"values": list(snapshot.values), "binding_id": snapshot.binding_id}


def _finite_vector(value: Any, size: int, name: str) -> np.ndarray:
    try:
        array = np.asarray(tuple(float(item) for item in value), dtype=float)
    except (TypeError, ValueError) as error:
        raise YieldControllerError(f"{name} must be a finite length-{size} vector") from error
    if array.shape != (size,) or not np.all(np.isfinite(array)):
        raise YieldControllerError(f"{name} must be a finite length-{size} vector")
    return array


class YieldController:
    def __init__(
        self,
        *,
        method: str,
        qp_library: Path | str,
        approach_inward_base: Any = (0.0, 0.0, -1.0),
        settings: YieldSettings = YieldSettings(),
        law_parameters: Mapping[str, Any] | None = None,
        dt_s: float = 0.002,
        qp_deadline_s: float | None = None,
        build_root: Path | str | None = None,
        estimator: NormalEstimator | None = None,
        allow_pre_path_force_ramp: bool = False,
    ) -> None:
        self.allow_pre_path_force_ramp = require_bool(allow_pre_path_force_ramp, "allow_pre_path_force_ramp")
        self.method = method
        self.role = method_role(method)
        self.settings = settings
        self.dt_s = finite_scalar(dt_s, "dt_s")
        self.approach_inward_base = require_unit_vector(approach_inward_base, "approach_inward_base")
        self.law = YieldLaw(method, law_parameters, dt_s=self.dt_s, build_root=build_root)
        self.qp = YieldQp(qp_library, deadline_s=qp_deadline_s)
        self.estimator = estimator if estimator is not None else NormalEstimator(self.approach_inward_base)
        if not np.allclose(self.estimator.approach, self.approach_inward_base, atol=1e-12):
            raise YieldControllerError("estimator approach must match the bound approach normal")
        self.task = Task()
        self.filtered = np.zeros(3)
        self.initialized = False
        self.offset_base_m = np.zeros(3)
        self.integral_n_s = np.zeros(3)
        self.last_time_s: float | None = None
        self.last_path_time_s: float | None = None
        self.last_position = np.zeros(3)
        self.last_velocity = np.zeros(3)
        self.roll_anchor: np.ndarray | None = None
        identity = {
            "schema": "ur10e.contact-yield-controller-identity-v1",
            "method": self.method,
            "role": self.role,
            "law_identity": self.law.identity,
            "law_build_fingerprint": self.law.build_fingerprint,
            "parameters": self.law.parameters,
            "settings": asdict(settings),
            "dt_s": self.dt_s,
            "approach_inward_base": tuple(float(value) for value in self.approach_inward_base),
            "estimator_parameters": self.estimator.parameters(),
            "qp_library": str(self.qp.library),
            "qp_library_sha256": self.qp.library_sha256,
            "fresh_age_s": FRESH_AGE_S,
            "stale_age_s": STALE_AGE_S,
            "latency_error_bound_m": settings.latency_error_bound_m,
            "law_frame": "fixed_base",
            "feedforward_in_nonlinear_damping": False,
            "independent_normal_p_controller": False,
        }
        if self.allow_pre_path_force_ramp:
            identity["pre_path_force_policy"] = "bounded_0_to_5N_ramp_v1"
        self.identity = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.identity_payload = identity

    def snapshot(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "law22": _snapshot_law(self.law.snapshot()),
            "filter": {
                "filtered_force_base_n": self.filtered.tolist(),
                "initialized": self.initialized,
            },
            "normal_estimate": self.estimator.snapshot(),
            "offset_base_m": self.offset_base_m.tolist(),
            "integral_n_s": self.integral_n_s.tolist(),
            "time_s": self.last_time_s,
            "path_time_s": self.last_path_time_s,
            "last_position_m": self.last_position.tolist(),
            "last_velocity_base_m_s": self.last_velocity.tolist(),
            "roll_anchor": None if self.roll_anchor is None else self.roll_anchor.tolist(),
            "qp": self.qp.snapshot(),
        }

    def _parse_snapshot(self, state: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(state, Mapping) or state.get("identity") != self.identity:
            raise YieldControllerError("controller snapshot identity differs")
        required = (
            "law22",
            "filter",
            "normal_estimate",
            "offset_base_m",
            "integral_n_s",
            "time_s",
            "path_time_s",
            "last_position_m",
            "last_velocity_base_m_s",
            "roll_anchor",
            "qp",
        )
        missing = [key for key in required if key not in state]
        if missing:
            raise YieldControllerError(f"controller snapshot missing {missing}")
        law22 = state["law22"]
        if not isinstance(law22, Mapping) or "values" not in law22:
            raise YieldControllerError("law22 snapshot is malformed")
        values = _finite_vector(law22["values"], SNAPSHOT_SIZE, "law22.values")
        binding = law22.get("binding_id")
        if binding is not None and (not isinstance(binding, str) or len(binding) != 64):
            raise YieldControllerError("law22 binding_id is malformed")
        filt = state["filter"]
        if not isinstance(filt, Mapping):
            raise YieldControllerError("filter snapshot is malformed")
        filtered = _finite_vector(filt.get("filtered_force_base_n"), 3, "filtered_force_base_n")
        initialized = require_bool(filt.get("initialized"), "filter.initialized")
        estimate = state["normal_estimate"]
        if not isinstance(estimate, Mapping):
            raise YieldControllerError("normal_estimate snapshot is malformed")
        inward = require_unit_vector(estimate.get("inward_normal_base"), "inward_normal_base")
        approach = require_unit_vector(estimate.get("approach_inward_base"), "approach_inward_base")
        if not np.allclose(approach,self.approach_inward_base,atol=1e-12,rtol=0):
            raise YieldControllerError("snapshot approach frame differs")
        offset = _finite_vector(state["offset_base_m"], 3, "offset_base_m")
        integral = _finite_vector(state["integral_n_s"], 3, "integral_n_s")
        time_s = optional_finite_time(state["time_s"], "time_s")
        path_time_s = optional_finite_time(state["path_time_s"], "path_time_s")
        position = _finite_vector(state["last_position_m"], 3, "last_position_m")
        velocity = _finite_vector(state["last_velocity_base_m_s"], 3, "last_velocity_base_m_s")
        if initialized != (time_s is not None):
            raise YieldControllerError("snapshot initialization clock differs")
        if initialized and state["roll_anchor"] is None:
            raise YieldControllerError("initialized snapshot is missing roll_anchor")
        roll = state["roll_anchor"]
        roll_anchor = None if roll is None else require_rotation(roll, "roll_anchor")
        qp_state = state["qp"]
        if not isinstance(qp_state, Mapping) or "x" not in qp_state or "y" not in qp_state:
            raise YieldControllerError("qp snapshot is malformed")
        _finite_vector(qp_state["x"], 6, "qp.x")
        _finite_vector(qp_state["y"], 12, "qp.y")
        return {
            "law": ContactLawSnapshot(tuple(float(value) for value in values), binding_id=binding),
            "filtered": filtered,
            "initialized": initialized,
            "inward": inward,
            "approach": approach,
            "offset": offset,
            "integral": integral,
            "time_s": time_s,
            "path_time_s": path_time_s,
            "position": position,
            "velocity": velocity,
            "roll_anchor": None if roll_anchor is None else roll_anchor.copy(),
            "qp": {"x": list(qp_state["x"]), "y": list(qp_state["y"])},
            "estimate": {"inward_normal_base": inward.tolist(), "approach_inward_base": approach.tolist()},
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        parsed = self._parse_snapshot(state)
        law_before = self.law.snapshot()
        qp_before = self.qp.snapshot()
        try:
            self.law.restore(parsed["law"])
            self.qp.restore(parsed["qp"])
            self.estimator.restore(parsed["estimate"])
            self.filtered = parsed["filtered"].copy()
            self.initialized = parsed["initialized"]
            self.offset_base_m = parsed["offset"].copy()
            self.integral_n_s = parsed["integral"].copy()
            self.last_time_s = parsed["time_s"]
            self.last_path_time_s = parsed["path_time_s"]
            self.last_position = parsed["position"].copy()
            self.last_velocity = parsed["velocity"].copy()
            self.roll_anchor = parsed["roll_anchor"]
        except Exception:
            self.law.restore(law_before)
            self.qp.restore(qp_before)
            raise

    def close(self) -> None:
        self.law.close()

    def __enter__(self) -> "YieldController":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def hold_pre_path_clock(self, *, time_s: float, dt_s: float) -> None:
        """Advance the sample clock without native/filter/estimator memory.

        This is the explicit pre-PATH stationary freeze-carry seam.  It is not
        a state reset and it must not run after PATH has started.
        """
        sample = finite_scalar(time_s, "time_s")
        elapsed = finite_scalar(dt_s, "dt")
        if not 0.0 < elapsed <= 0.004:
            raise YieldControllerError("elapsed sample interval must be in (0, 4ms]")
        if sample < 0.0:
            raise YieldControllerError("invalid sample clock")
        if self.last_path_time_s is not None:
            raise YieldControllerError("active path cannot return to preparation or freeze its clock")
        if self.last_time_s is None:
            return
        if not math.isclose(sample, self.last_time_s + elapsed, rel_tol=0.0, abs_tol=1e-12):
            raise YieldControllerError("sample clock must advance by the actual dt")
        self.last_time_s = sample

    def step(self, observation: Mapping[str, Any], reference: Mapping[str, Any], dt: float) -> dict[str, Any]:
        dt_s = finite_scalar(dt, "dt")
        if not 0.0 < dt_s <= 0.004:
            raise YieldControllerError("elapsed sample interval must be in (0, 4ms]")
        before = self.snapshot()
        try:
            return self._step_body(observation, reference, dt_s)
        except Exception:
            self.restore(before)
            raise

    def _reject_geometry_truth(self, observation: Mapping[str, Any]) -> None:
        leaked = [
            key
            for key in (
                "true_outward_normal_base",
                "true_inward_normal_base",
                "true_normal",
                "gap_m",
                "surface_height_m",
                "evaluator",
            )
            if key in observation
        ]
        if leaked:
            raise YieldControllerError(f"controller observation contains geometry truth {leaked}")

    def _step_body(
        self,
        observation: Mapping[str, Any],
        reference: Mapping[str, Any],
        dt_s: float,
    ) -> dict[str, Any]:
        if not isinstance(observation, Mapping) or not isinstance(reference, Mapping):
            raise YieldControllerError("observation and reference must be mappings")
        self._reject_geometry_truth(observation)
        time_s = finite_scalar(observation["time_s"], "time_s")
        if time_s < 0.0:
            raise YieldControllerError("invalid sample clock")
        if self.last_time_s is not None and not math.isclose(
            time_s, self.last_time_s + dt_s, rel_tol=0.0, abs_tol=1e-12
        ):
            raise YieldControllerError("sample clock must advance by the actual dt")
        age_s = finite_scalar(observation.get("state_age_s", 0.0), "state_age_s")
        if age_s < 0.0:
            raise YieldControllerError("invalid observation age")
        age_band = classify_sensor_age(age_s)
        if age_band == "stale":
            raise YieldControllerError("stale observation at 80ms")
        relative_speed = (
            self.settings.tangent_speed_cap_m_s
            + float(self.task.sanity()["speed_upper_bound_m_s"])
        )
        if (age_s + self.settings.latency_extra_s) * relative_speed > self.settings.latency_error_bound_m:
            raise YieldControllerError("geometric latency uncertainty exceeds 0.001 m bound")
        position = finite_vector3(observation["position_m"], "position_m")
        rotation = require_rotation(observation["rotation"], "rotation")
        raw_force = finite_vector3(observation["raw_force_base_n"], "raw_force_base_n")
        raw_torque = finite_vector3(observation["raw_torque_base_nm"], "raw_torque_base_nm")
        injection = finite_vector3(
            observation.get("software_injection_base_n", (0.0, 0.0, 0.0)),
            "software_injection_base_n",
        )
        jacobian = np.asarray(observation["jacobian"], dtype=float)
        lower = finite_vector6(observation["joint_velocity_lower"], "joint_velocity_lower")
        upper = finite_vector6(observation["joint_velocity_upper"], "joint_velocity_upper")
        if np.linalg.norm(raw_force) >= self.settings.raw_force_limit_n:
            raise YieldControllerError("raw sensor guard rejected observation")
        if np.linalg.norm(raw_torque) >= self.settings.raw_torque_limit_nm:
            raise YieldControllerError("raw sensor guard rejected observation")
        phase = reference.get("phase", "path")
        if phase not in ("baseline", "entry", "path"):
            raise YieldControllerError("invalid reference phase")
        if self.last_path_time_s is not None and phase != "path":
            raise YieldControllerError("active path cannot return to preparation or freeze its clock")
        path_time_s = reference.get("path_time_s")
        if phase == "path":
            path_time_s = finite_scalar(path_time_s, "path_time_s")
            if self.last_path_time_s is None:
                if path_time_s < 0.0:
                    raise YieldControllerError("path clock must be nonnegative")
            elif not math.isclose(
                path_time_s, self.last_path_time_s + dt_s, rel_tol=0.0, abs_tol=1e-12
            ):
                raise YieldControllerError("path clock must advance by the actual dt")
        elif path_time_s is not None:
            raise YieldControllerError(f"{phase} has no path clock")
        target_force = finite_scalar(
            reference.get("force_n", self.settings.target_force_n),
            "force_n",
        )
        # Identity stays the frozen 5 N PATH invariant.  Blindly feeding the
        # mature 1-to-5 N baseline ramp would otherwise reject every pre-PATH
        # tick.  Simulation callers that omit force_n still get 5 N.
        if phase == "path" or not self.allow_pre_path_force_ramp:
            if abs(target_force - self.settings.target_force_n) > 1e-12:
                raise YieldControllerError("PATH requires the 5 N force invariant")
        elif target_force < 0.0 or target_force > self.settings.target_force_n + 1e-12:
            raise YieldControllerError("pre-PATH force reference must remain within [0, 5] N")
        ref_position = finite_vector3(reference["position_m"], "reference_position_m")
        ref_velocity = finite_vector3(reference["velocity_m_s"], "reference_velocity_m_s")

        alpha = -math.expm1(-dt_s / self.settings.filter_tau_s)
        filtered = raw_force.copy() if not self.initialized else self.filtered + alpha * (raw_force - self.filtered)
        if "linear_velocity_base_m_s" in observation:
            measured_velocity = finite_vector3(
                observation["linear_velocity_base_m_s"],
                "linear_velocity_base_m_s",
            )
        elif self.initialized:
            measured_velocity = (position - self.last_position) / dt_s
        else:
            measured_velocity = np.zeros(3)
        in_contact = float(np.linalg.norm(filtered)) >= self.settings.contact_force_n
        estimate = self.estimator.update(
            dt_s=dt_s,
            measured_linear_velocity_base_m_s=measured_velocity,
            measured_force_base_n=filtered,
            in_contact=in_contact,
        )
        inward = np.asarray(estimate["inward_normal_base"], dtype=float)
        outward = -inward
        tangent = projector_tangent(inward)
        signed_load = float(np.dot(filtered, outward))
        force_error = signed_load - target_force
        residual = (filtered + injection) - target_force * outward
        path_error = position - ref_position
        restoring = (
            -self.settings.path_stiffness_n_per_m * (tangent @ path_error)
            - self.settings.compliance_stiffness_n_per_m * (tangent @ self.offset_base_m)
        )
        pre_integral = residual + restoring
        if self.settings.integral_force_gain == 0.0:
            integral = np.zeros(3)
            law_force = pre_integral
        else:
            integral = self.integral_n_s
            law_force = pre_integral + self.settings.integral_force_gain * integral
        law_velocity = np.asarray(self.law.step(law_force, dt_s), dtype=float)
        normal_law = float(np.dot(law_velocity, inward)) * inward
        tangent_law = tangent @ law_velocity
        feedforward = tangent @ ref_velocity
        tangent_uncapped = tangent_law + feedforward
        tangent_capped, tangent_over = cap_vector(
            tangent_uncapped,
            self.settings.tangent_speed_cap_m_s,
            "tangent_velocity",
        )
        normal_uncapped = float(np.dot(normal_law, inward))
        normal_speed = float(np.clip(
            normal_uncapped,
            -self.settings.normal_speed_cap_m_s,
            self.settings.normal_speed_cap_m_s,
        ))
        saturated = (
            abs(normal_speed) < abs(normal_uncapped) - 1e-16
            or tangent_over > 0.0
        )
        if self.settings.integral_force_gain != 0.0 and not saturated:
            trial = integral + dt_s * pre_integral
            limit = self.settings.integral_limit_n_s
            norm = float(np.linalg.norm(trial))
            if norm > limit:
                trial = trial * (limit / norm)
            integral = trial
        elif self.settings.integral_force_gain == 0.0:
            integral = np.zeros(3)

        if self.roll_anchor is None:
            self.roll_anchor = rotation.copy()
        desired_rotation = transported_roll_anchor(self.roll_anchor, inward)
        omega = attitude_error_omega(
            rotation,
            desired_rotation,
            gain_s_inv=self.settings.orientation_kp_s_inv,
            angular_cap_rad_s=self.settings.angular_speed_cap_rad_s,
        )
        twist = np.concatenate((tangent_capped + normal_speed * inward, omega))
        try:
            solved = self.qp.compose(jacobian, twist, lower, upper, inward,
                continuous_scaling=require_bool(observation.get("continuous_task_scaling", False),
                                                "continuous_task_scaling"),
                path_guard_context=observation.get("path_guard_context"))
        except YieldQpFatal:
            raise
        except YieldQpError:
            raise

        applied = np.asarray(solved["applied_twist_base"], dtype=float)
        scale = float(solved["task_scale"])
        executed_feedforward = scale * feedforward
        executed_correction = applied[:3] - executed_feedforward
        self.offset_base_m = self.offset_base_m + dt_s * executed_correction
        planned_progress = float(np.linalg.norm(feedforward))
        commanded_progress = float(np.linalg.norm(tangent @ applied[:3]))
        measured_progress = float(np.linalg.norm(tangent @ measured_velocity))

        self.filtered = filtered
        self.initialized = True
        self.integral_n_s = integral
        self.last_time_s = time_s
        self.last_path_time_s = path_time_s if phase == "path" else self.last_path_time_s
        self.last_position = position.copy()
        self.last_velocity = measured_velocity.copy()
        return {
            "controller": self.method,
            "role": self.role,
            "claim_scope": CLAIM_SCOPE,
            "twist_base": tuple(float(value) for value in twist),
            "qdot_rad_s": solved["qdot_rad_s"],
            "applied_twist_base": solved["applied_twist_base"],
            "task_scale": solved["task_scale"],
            "normal_task_scale": solved.get("normal_task_scale", 1.),
            "normal_unloading_preserved": solved["normal_unloading_preserved"],
            "qp_scaling_policy": solved.get("qp_scaling_policy", "legacy_discrete_v1"),
            "qp_intervention": solved["qp_intervention"],
            "path_guard": solved.get("path_guard"),
            "feasibility_force_guarantee": False,
            "planned_progress_m_s": planned_progress,
            "commanded_tangent_progress_m_s": commanded_progress,
            "measured_progress_m_s": measured_progress,
            "path_clock_frozen": False,
            "path_time_s": path_time_s,
            "phase": phase,
            "sample_time_s": time_s,
            "elapsed_dt_s": dt_s,
            "observation_age_s": age_s,
            "age_band": age_band,
            "raw_force_base_n": tuple(float(value) for value in raw_force),
            "filtered_force_base_n": tuple(float(value) for value in filtered),
            "software_injection_base_n": tuple(float(value) for value in injection),
            "software_injection_is_human_evidence": False,
            "law_force_base_n": tuple(float(value) for value in law_force),
            "force_residual_base_n": tuple(float(value) for value in residual),
            "law_velocity_base_m_s": tuple(float(value) for value in law_velocity),
            "law_normal_velocity_m_s": tuple(float(value) for value in normal_law),
            "law_tangent_velocity_m_s": tuple(float(value) for value in tangent_law),
            "offset_base_m": tuple(float(value) for value in self.offset_base_m),
            "integral_n_s": tuple(float(value) for value in self.integral_n_s),
            "inward_normal_base": tuple(float(value) for value in inward),
            "signed_normal_load_n": signed_load,
            "force_error_n": force_error,
            "reference_force_n": target_force,
            "path_error_base_m": tuple(float(value) for value in path_error),
            "reference_velocity_base_m_s": tuple(float(value) for value in ref_velocity),
            "normal_speed_m_s": normal_speed,
            "tangent_saturation_m_s": tangent_over,
            "normal_saturated": abs(normal_speed) < abs(normal_uncapped) - 1e-16,
            "estimator": estimate,
            "qp_equality_residual": solved["qp_equality_residual"],
            "qp_bound_violation": solved["qp_bound_violation"],
            "law_state": tuple(float(value) for value in self.law.state),
            "feedforward_in_nonlinear_damping": False,
            "independent_normal_p_controller": False,
            "memory_reset": False,
            "roll_anchor": self.roll_anchor.tolist() if self.roll_anchor is not None else None,
        }
