"""Closed-loop contact plant for the offline yield-recovery route.

The evaluator owns the true surface normal and height field.  Those quantities
are never written into the controller observation.  Software force injection is
labeled separately from an external wrench that does affect dynamics and the
sensed wrench.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
import hashlib, json
from typing import Any, Mapping

import numpy as np

from contact_semantics import finite_vector3, finite_vector6
from contact_yield_kinematics import RobotKinematics, Ur10eKinematics, load_kinematics
from contact_yield_math import finite_scalar, projector_tangent, so3_exp, require_rotation
from contact_yield_protocol import (
    CLAIM_SCOPE,
    material_spec,
    parse_scenario,
    perturbation_envelope,
)


class YieldSimulatorError(RuntimeError):
    pass


OBSERVATION_KEYS = frozenset(
    {
        "time_s",
        "position_m",
        "rotation",
        "raw_force_base_n",
        "raw_torque_base_nm",
        "software_injection_base_n",
        "jacobian",
        "joint_velocity_lower",
        "joint_velocity_upper",
        "state_age_s",
        "actual_q",
        "actual_qdot",
        "linear_velocity_base_m_s",
    }
)


def substepped_simulator(substeps: int):
    """Sample/hold commands at controller rate; integrate plant at a finer rate.

    Optional factory preserves legacy single-step identities. The explicit
    substep count is part of the plant identity and full-state replay contract.
    It is a numerical setting, not a hardware-fidelity certificate.
    """
    if isinstance(substeps, bool) or not isinstance(substeps, int) or not 1 <= substeps <= 64:
        raise ValueError('plant substeps must be an integer in [1, 64]')

    class SubsteppedPlant(YieldSimulator):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.identity_payload = {**self.identity_payload, 'integration_substeps': substeps}
            self.identity = hashlib.sha256(
                json.dumps(self.identity_payload, sort_keys=True).encode()).hexdigest()

        def step(self, *, dt_s, **kwargs):
            original_clock = kwargs.get('path_time_s', 0.)
            for k in range(substeps):
                result = super().step(dt_s=dt_s/substeps, **{
                    **kwargs, 'path_time_s': original_clock + k*dt_s/substeps})
            return result

    return SubsteppedPlant


@dataclass(frozen=True)
class SurfaceField:
    """Unknown-to-controller height field z = h(x, y) in the base frame."""

    origin_m: tuple[float, float, float]
    kappa_xx: float = 0.8
    kappa_yy: float = 0.4
    sine_amp_m: float = 0.0004
    sine_freq_per_m: float = 40.0

    def height(self, x: float, y: float) -> float:
        dx = x - self.origin_m[0]
        dy = y - self.origin_m[1]
        return (
            self.origin_m[2]
            + 0.5 * self.kappa_xx * dx * dx
            + 0.5 * self.kappa_yy * dy * dy
            + self.sine_amp_m * math.sin(self.sine_freq_per_m * dx)
        )

    def gradient_xy(self, x: float, y: float) -> tuple[float, float]:
        dx = x - self.origin_m[0]
        dh_dx = self.kappa_xx * dx + self.sine_amp_m * self.sine_freq_per_m * math.cos(
            self.sine_freq_per_m * dx
        )
        dh_dy = self.kappa_yy * (y - self.origin_m[1])
        return dh_dx, dh_dy

    def true_outward_normal(self, position_m: Any) -> np.ndarray:
        point = finite_vector3(position_m, "position_m")
        dh_dx, dh_dy = self.gradient_xy(float(point[0]), float(point[1]))
        raw = np.array((-dh_dx, -dh_dy, 1.0), dtype=float)
        return raw / float(np.linalg.norm(raw))

    def gap_m(self, position_m: Any) -> float:
        point = finite_vector3(position_m, "position_m")
        return float(point[2] - self.height(float(point[0]), float(point[1])))


def surface_for_contact(
    position_m: Any,
    *,
    target_force_n: float = 5.0,
    material: str = "stiff_low_mu",
    kappa_xx: float = 0.8,
    kappa_yy: float = 0.4,
) -> SurfaceField:
    """Place the height field so the given TCP is in unilateral contact near target load."""
    point = finite_vector3(position_m, "position_m")
    stiffness = material_spec(material)["stiffness_n_per_m"]
    penetration = float(target_force_n) / stiffness
    return SurfaceField(
        origin_m=(float(point[0]), float(point[1]), float(point[2]) + penetration),
        kappa_xx=kappa_xx,
        kappa_yy=kappa_yy,
    )


class YieldSimulator:
    def __init__(
        self,
        *,
        kinematics: RobotKinematics | None = None,
        surface: SurfaceField,
        material: str = "stiff_low_mu",
        q: Any,
        servo_tau_s: float = 0.008,
        cartesian_mass_kg: float = 4.0,
        cartesian_inertia: Any = (0.05, 0.05, 0.05),
        joint_velocity_limit_rad_s: float = 0.05,
        force_sensor_tau_s: float = 0.004,
        require_ur10e: bool = False,
        timeline: str = "diagnostic",
        seed_sensor_from_contact: bool = True,
    ) -> None:
        self.kinematics = kinematics if kinematics is not None else load_kinematics(require_ur10e=require_ur10e)
        self.surface = surface
        self.material_name = material
        self.material = material_spec(material)
        self.timeline = timeline
        self.servo_tau_s = finite_scalar(servo_tau_s, "servo_tau_s")
        self.cartesian_mass_kg = finite_scalar(cartesian_mass_kg, "cartesian_mass_kg")
        self.cartesian_inertia = finite_vector3(cartesian_inertia, "cartesian_inertia")
        self.joint_velocity_limit = finite_scalar(joint_velocity_limit_rad_s, "joint_velocity_limit_rad_s")
        self.force_sensor_tau_s = finite_scalar(force_sensor_tau_s, "force_sensor_tau_s")
        if min(self.servo_tau_s, self.cartesian_mass_kg, self.force_sensor_tau_s, self.joint_velocity_limit) <= 0.0:
            raise YieldSimulatorError("plant time constants, mass and joint cap must be positive")
        self.q = np.asarray(q, dtype=float).copy()
        if self.q.shape != (6,) or not np.all(np.isfinite(self.q)):
            raise YieldSimulatorError("plant state must be a finite 6-vector")
        pose = self.kinematics.pose_and_jacobian(self.q)
        self.qdot = np.zeros(6)
        self.qdot_cmd = np.zeros(6)
        self.servo_integral = np.zeros(6)
        self.rotation = np.array(pose["rotation"], dtype=float).copy()
        self.sensed_force = np.zeros(3)
        self.sensed_torque = np.zeros(3)
        self.true_wrench = np.zeros(6)
        self.time_s = 0.0
        binding = {"surface": asdict(surface), "material": self.material,
                   "kinematics": self.kinematics.kind, "calibration": self.kinematics.calibration_hash,
                   "servo_tau_s": self.servo_tau_s, "servo_model": "pi_velocity_v2",
                   "cartesian_mass_kg": self.cartesian_mass_kg, "cartesian_inertia": self.cartesian_inertia.tolist(),
                   "joint_limit": self.joint_velocity_limit, "sensor_tau": self.force_sensor_tau_s, "timeline": timeline}
        self.identity = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
        self.identity_payload = binding
        self.ur10e = isinstance(self.kinematics, Ur10eKinematics)
        if not self.ur10e and require_ur10e:
            raise YieldSimulatorError("UR10e kinematics were required but unavailable")
        if seed_sensor_from_contact:
            contact = self._contact_wrench(*self._pose_and_twist())
            self.sensed_force = np.array(contact["force_base_n"], dtype=float).copy()

    def snapshot(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "q": self.q.tolist(),
            "qdot": self.qdot.tolist(),
            "qdot_cmd": self.qdot_cmd.tolist(),
            "servo_integral": self.servo_integral.tolist(),
            "rotation": self.rotation.tolist(),
            "sensed_force": self.sensed_force.tolist(),
            "sensed_torque": self.sensed_torque.tolist(),
            "true_wrench": self.true_wrench.tolist(),
            "time_s": self.time_s,
            "kinematics_kind": self.kinematics.kind,
            "material": self.material_name,
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        if state.get("identity") != self.identity:
            raise YieldSimulatorError("plant snapshot identity differs")
        parsed = {}
        for name, size in (("q",6),("qdot",6),("qdot_cmd",6),("servo_integral",6),
                           ("sensed_force",3),("sensed_torque",3),("true_wrench",6)):
            value = np.asarray(state[name], dtype=float)
            if value.shape != (size,) or not np.isfinite(value).all():
                raise YieldSimulatorError("invalid plant snapshot " + name)
            parsed[name] = value.copy()
        parsed["rotation"] = require_rotation(state["rotation"]).copy()
        parsed["time_s"] = finite_scalar(state["time_s"], "time_s")
        if parsed["time_s"] < 0: raise YieldSimulatorError("invalid plant time")
        for name, value in parsed.items(): setattr(self, name, value)

    def _tcp_state(self) -> dict[str, Any]:
        pose = self.kinematics.pose_and_jacobian(self.q)
        if self.ur10e:
            jacobian = np.asarray(pose["jacobian"], dtype=float)
            twist = jacobian @ self.qdot
            position = np.asarray(pose["position_m"], dtype=float)
            rotation = np.asarray(pose["rotation"], dtype=float)
            self.rotation = rotation.copy()
        else:
            position = self.q[:3].copy()
            rotation = self.rotation.copy()
            jacobian = np.eye(6)
            twist = self.qdot.copy()
        return {
            "position_m": position,
            "rotation": rotation,
            "jacobian": jacobian,
            "twist_base": twist,
            "kind": pose["kind"],
            "claim_scope": pose["claim_scope"],
        }

    def _pose_and_twist(self) -> tuple[np.ndarray, np.ndarray]:
        tcp = self._tcp_state()
        return tcp["position_m"], tcp["twist_base"]

    def _contact_wrench(self, position: np.ndarray, twist: np.ndarray) -> dict[str, Any]:
        gap = self.surface.gap_m(position)
        outward = self.surface.true_outward_normal(position)
        inward = -outward
        velocity = twist[:3]
        closing = float(np.dot(velocity, inward))
        stiffness = self.material["stiffness_n_per_m"]
        damping = self.material["damping_n_s_per_m"]
        mu = self.material["friction_mu"]
        eps = self.material["regularization_m_s"]
        if gap >= 0.0:
            normal_load = 0.0
            force = np.zeros(3)
            contact = False
        else:
            penetration = -gap
            normal_load = max(0.0, stiffness * penetration + damping * closing)
            tangent_velocity = projector_tangent(outward) @ velocity
            speed = float(np.linalg.norm(tangent_velocity))
            regularized = tangent_velocity / math.sqrt(speed * speed + eps * eps)
            force = normal_load * outward - mu * normal_load * regularized
            contact = normal_load > 0.0
        return {
            "force_base_n": force,
            "torque_base_nm": np.zeros(3),
            "true_outward_normal_base": outward,
            "true_inward_normal_base": inward,
            "gap_m": gap,
            "normal_load_n": normal_load,
            "in_contact": contact,
        }

    def _external_wrench(
        self,
        *,
        scenario: str,
        path_time_s: float,
        position: np.ndarray,
        reference_velocity: np.ndarray,
    ) -> np.ndarray:
        spec = parse_scenario(scenario, timeline=self.timeline)
        envelope = perturbation_envelope(
            spec["kind"],
            path_time_s,
            start_s=spec["start_s"] or 0.0,
            width_s=spec["width_s"] or 1.0,
            hold_s=spec["hold_s"] or 0.0,
            release_s=spec["release_s"] or 0.0,
        )
        amplitude = spec["amplitude_n"] * envelope
        if amplitude == 0.0:
            return np.zeros(6)
        outward = self.surface.true_outward_normal(position)
        tangent_dir = projector_tangent(outward) @ reference_velocity
        tangent_norm = float(np.linalg.norm(tangent_dir))
        if tangent_norm < 1e-9:
            tangent_dir = projector_tangent(outward) @ np.array((1.0, 0.0, 0.0))
            tangent_norm = float(np.linalg.norm(tangent_dir))
        tangent = tangent_dir / max(tangent_norm, 1e-12)
        direction = spec["direction"]
        if direction == "normal":
            axis = outward
        elif direction == "tangent":
            axis = tangent
        else:
            axis = outward + tangent
            axis = axis / float(np.linalg.norm(axis))
        return np.concatenate((amplitude * axis, np.zeros(3)))

    def _integrate(self, wrench: np.ndarray, dt_s: float) -> None:
        jacobian = self._tcp_state()["jacobian"]
        tau_contact = jacobian.T @ wrench
        velocity_error = self.qdot_cmd - self.qdot
        self.servo_integral += dt_s * velocity_error
        # Critically damped PI velocity servo rejects constant contact loads.
        # This is a declared servo assumption, not an identified UR controller.
        qdot_dot_servo = velocity_error / self.servo_tau_s + self.servo_integral / (4*self.servo_tau_s**2)
        mass = self.kinematics.mass_matrix(self.q)
        if mass is not None:
            try:
                qdot_dot_force = np.linalg.solve(mass, tau_contact)
            except np.linalg.LinAlgError as error:
                raise YieldSimulatorError("URDF mass matrix is singular") from error
        else:
            inv_mass = np.diag(
                [
                    1.0 / self.cartesian_mass_kg,
                    1.0 / self.cartesian_mass_kg,
                    1.0 / self.cartesian_mass_kg,
                    1.0 / self.cartesian_inertia[0],
                    1.0 / self.cartesian_inertia[1],
                    1.0 / self.cartesian_inertia[2],
                ]
            )
            qdot_dot_force = inv_mass @ wrench
        self.qdot = self.qdot + dt_s * (qdot_dot_servo + qdot_dot_force)
        if self.ur10e:
            cap = self.joint_velocity_limit
            self.qdot = np.clip(self.qdot, -cap, cap)
            self.q = self.q + dt_s * self.qdot
            return
        linear = _cap(self.qdot[:3], 0.05)
        angular = _cap(self.qdot[3:], 0.2)
        self.qdot = np.concatenate((linear, angular))
        self.q[:3] = self.q[:3] + dt_s * self.qdot[:3]
        self.rotation = so3_exp(dt_s * self.qdot[3:]) @ self.rotation
        self.q[3:] = self.q[3:] + dt_s * self.qdot[3:]

    def _pack(
        self,
        *,
        software_injection_base_n: Any,
        state_age_s: float,
        path_time_s: float,
        scenario: str,
        reference_velocity_m_s: Any,
    ) -> dict[str, Any]:
        tcp = self._tcp_state()
        contact = self._contact_wrench(tcp["position_m"], tcp["twist_base"])
        injection = finite_vector3(software_injection_base_n, "software_injection_base_n")
        external = self._external_wrench(
            scenario=scenario,
            path_time_s=path_time_s,
            position=tcp["position_m"],
            reference_velocity=np.asarray(reference_velocity_m_s, dtype=float),
        )
        observation = {
            "time_s": self.time_s,
            "position_m": tuple(float(value) for value in tcp["position_m"]),
            "rotation": np.array(tcp["rotation"], dtype=float, copy=True),
            "raw_force_base_n": tuple(float(value) for value in self.sensed_force),
            "raw_torque_base_nm": tuple(float(value) for value in self.sensed_torque),
            "software_injection_base_n": tuple(float(value) for value in injection),
            "jacobian": np.array(tcp["jacobian"], dtype=float, copy=True),
            "joint_velocity_lower": tuple(-self.joint_velocity_limit for _ in range(6)),
            "joint_velocity_upper": tuple(self.joint_velocity_limit for _ in range(6)),
            "state_age_s": finite_scalar(state_age_s, "state_age_s"),
            "actual_q": tuple(float(value) for value in self.q),
            "actual_qdot": tuple(float(value) for value in self.qdot),
            "linear_velocity_base_m_s": tuple(float(value) for value in tcp["twist_base"][:3]),
        }
        evaluator = {
            "true_outward_normal_base": tuple(float(value) for value in contact["true_outward_normal_base"]),
            "true_inward_normal_base": tuple(float(value) for value in contact["true_inward_normal_base"]),
            "gap_m": contact["gap_m"],
            "true_normal_load_n": contact["normal_load_n"],
            "true_contact_force_base_n": tuple(float(value) for value in contact["force_base_n"]),
            "external_force_base_n": tuple(float(value) for value in external[:3]),
            "in_contact": contact["in_contact"],
            "kinematics_kind": self.kinematics.kind,
            "kinematics_claim_scope": self.kinematics.claim_scope,
            "claim_scope": CLAIM_SCOPE,
        }
        assert OBSERVATION_KEYS == frozenset(observation)
        return {
            "observation": observation,
            "evaluator": evaluator,
            "software_injection_is_human_evidence": False,
            "external_wrench_affects_dynamics": True,
        }

    def observe(
        self,
        *,
        state_age_s: float = 0.0,
        software_injection_base_n: Any = (0.0, 0.0, 0.0),
        scenario: str = "nominal",
        path_time_s: float = 0.0,
        reference_velocity_m_s: Any = (0.0, 0.0, 0.0),
    ) -> dict[str, Any]:
        return self._pack(
            software_injection_base_n=software_injection_base_n,
            state_age_s=state_age_s,
            path_time_s=path_time_s,
            scenario=scenario,
            reference_velocity_m_s=reference_velocity_m_s,
        )

    def step(
        self,
        *,
        dt_s: float,
        qdot_cmd: Any,
        scenario: str = "nominal",
        path_time_s: float = 0.0,
        reference_velocity_m_s: Any = (0.0, 0.0, 0.0),
        software_injection_base_n: Any = (0.0, 0.0, 0.0),
        state_age_s: float = 0.0,
    ) -> dict[str, Any]:
        elapsed = finite_scalar(dt_s, "dt_s")
        if not 0.0 < elapsed <= 0.004:
            raise YieldSimulatorError("simulator dt_s must be in (0, 4ms]")
        self.qdot_cmd = np.asarray(finite_vector6(qdot_cmd, "qdot_cmd"), dtype=float)
        tcp = self._tcp_state()
        contact = self._contact_wrench(tcp["position_m"], tcp["twist_base"])
        external = self._external_wrench(
            scenario=scenario,
            path_time_s=path_time_s,
            position=tcp["position_m"],
            reference_velocity=np.asarray(reference_velocity_m_s, dtype=float),
        )
        true_force = contact["force_base_n"] + external[:3]
        true_torque = contact["torque_base_nm"] + external[3:]
        true_wrench = np.concatenate((true_force, true_torque))
        self._integrate(true_wrench, elapsed)
        self.time_s += elapsed
        alpha = -math.expm1(-elapsed / self.force_sensor_tau_s)
        self.sensed_force = self.sensed_force + alpha * (true_force - self.sensed_force)
        self.sensed_torque = self.sensed_torque + alpha * (true_torque - self.sensed_torque)
        self.true_wrench = true_wrench
        return self._pack(
            software_injection_base_n=software_injection_base_n,
            state_age_s=state_age_s,
            path_time_s=path_time_s,
            scenario=scenario,
            reference_velocity_m_s=reference_velocity_m_s,
        )


def _cap(vector: np.ndarray, limit: float) -> np.ndarray:
    speed = float(np.linalg.norm(vector))
    if speed > limit:
        return vector * (limit / speed)
    return vector
