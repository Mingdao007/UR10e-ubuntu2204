"""Common surface-normal estimator.  Never consumes simulator geometry truth.

Initialization uses the contact-approach direction.  Updates use the measured
local motion tangent constraint on the unit sphere, gated by contact load and
tangential excitation.  An optional coplanarity term uses unit f×v with those
same gates; |f×v| below coplanarity_cross_floor_n_m_s is numerical degeneracy,
not a force-noise robustness guarantee.  A bounded force-direction correction
is applied only inside a documented friction cone; it is a bias, not a truth
update, and is not covered by the combined motion/coplanarity rate cap.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np

from contact_semantics import finite_vector3, normalize_unit
from contact_yield_math import YieldMathError, finite_scalar, projector_tangent, require_unit_vector


class NormalEstimatorError(YieldMathError):
    pass


class NormalEstimator:
    """Unit inward normal in the fixed base frame."""

    def __init__(
        self,
        approach_inward_base: Any,
        *,
        initial_inward_normal_base: Any | None = None,
        contact_force_n: float = 1.0,
        excitation_m_s: float = 0.002,
        motion_gain: float = 8.0,
        force_correction_gain: float = 1.5,
        force_correction_max_rad: float = 0.004,
        assumed_friction_mu: float = 0.35,
        min_force_n: float = 0.5,
        motion_normalization_floor_m_s: float | None = None,
        motion_rate_cap_rad_s: float | None = None,
        coplanarity_gain_s_inv: float = 0.0,
        coplanarity_cross_floor_n_m_s: float = 1e-9,
    ) -> None:
        self.motion_normalization_floor_m_s = (None if motion_normalization_floor_m_s is None
            else finite_scalar(motion_normalization_floor_m_s, "motion_normalization_floor_m_s"))
        self.motion_rate_cap_rad_s = (None if motion_rate_cap_rad_s is None
            else finite_scalar(motion_rate_cap_rad_s, "motion_rate_cap_rad_s"))
        for value in (self.motion_normalization_floor_m_s, self.motion_rate_cap_rad_s):
            if value is not None and value <= 0:
                raise NormalEstimatorError("optional motion normalization and rate cap must be positive")
        self.approach = normalize_unit(approach_inward_base, name="approach_inward_base")
        self.initial_inward_normal_base = (None if initial_inward_normal_base is None
            else require_unit_vector(initial_inward_normal_base, "initial_inward_normal_base"))
        # This is estimator uncertainty only. The physical approach, path,
        # plant and contact surface retain their original frame.
        self.normal = (self.approach.copy() if self.initial_inward_normal_base is None
                       else self.initial_inward_normal_base.copy())
        self.contact_force_n = finite_scalar(contact_force_n, "contact_force_n")
        self.excitation_m_s = finite_scalar(excitation_m_s, "excitation_m_s")
        self.motion_gain = finite_scalar(motion_gain, "motion_gain")
        self.force_correction_gain = finite_scalar(force_correction_gain, "force_correction_gain")
        self.force_correction_max_rad = finite_scalar(force_correction_max_rad, "force_correction_max_rad")
        self.assumed_friction_mu = finite_scalar(assumed_friction_mu, "assumed_friction_mu")
        self.min_force_n = finite_scalar(min_force_n, "min_force_n")
        self.coplanarity_gain_s_inv = finite_scalar(coplanarity_gain_s_inv, "coplanarity_gain_s_inv")
        self.coplanarity_cross_floor_n_m_s = finite_scalar(
            coplanarity_cross_floor_n_m_s, "coplanarity_cross_floor_n_m_s")
        if any(value <= 0.0 for value in (
            self.contact_force_n,
            self.excitation_m_s,
            self.min_force_n,
        )):
            raise NormalEstimatorError("normal-estimator thresholds must be positive")
        if self.force_correction_max_rad < 0.0 or self.assumed_friction_mu < 0.0:
            raise NormalEstimatorError("friction-bias bounds must be nonnegative")
        if self.force_correction_gain < 0.0 or self.motion_gain < 0.0 or self.coplanarity_gain_s_inv < 0.0:
            raise NormalEstimatorError("normal-estimator gains must be nonnegative")
        if self.coplanarity_cross_floor_n_m_s <= 0.0:
            raise NormalEstimatorError("coplanarity_cross_floor_n_m_s must be positive")
        if self.coplanarity_gain_s_inv > 0.0 and self.motion_normalization_floor_m_s is None:
            raise NormalEstimatorError("coplanarity update requires motion_normalization_floor_m_s")

    def parameters(self) -> dict[str, Any]:
        parameters = {
            "contact_force_n": self.contact_force_n,
            "excitation_m_s": self.excitation_m_s,
            "motion_gain": self.motion_gain,
            "force_correction_gain": self.force_correction_gain,
            "force_correction_max_rad": self.force_correction_max_rad,
            "assumed_friction_mu": self.assumed_friction_mu,
            "min_force_n": self.min_force_n,
        }

        if self.initial_inward_normal_base is not None:
            parameters["initial_inward_normal_base"] = self.initial_inward_normal_base.tolist()
        if self.motion_normalization_floor_m_s is not None:
            parameters["motion_normalization_floor_m_s"] = self.motion_normalization_floor_m_s
        if self.motion_rate_cap_rad_s is not None:
            parameters["motion_rate_cap_rad_s"] = self.motion_rate_cap_rad_s
        if self.coplanarity_gain_s_inv > 0.0:
            parameters["coplanarity_gain_s_inv"] = self.coplanarity_gain_s_inv
            parameters["coplanarity_cross_floor_n_m_s"] = self.coplanarity_cross_floor_n_m_s
        return parameters

    def snapshot(self) -> dict[str, Any]:
        return {
            "inward_normal_base": self.normal.tolist(),
            "approach_inward_base": self.approach.tolist(),
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        if "inward_normal_base" not in state or "approach_inward_base" not in state:
            raise NormalEstimatorError("normal estimator snapshot is missing required keys")
        # Reject non-unit or nonfinite tokens; do not silently renormalize.
        self.normal = require_unit_vector(state["inward_normal_base"], "inward_normal_base")
        self.approach = require_unit_vector(state["approach_inward_base"], "approach_inward_base")

    def update(
        self,
        *,
        dt_s: float,
        measured_linear_velocity_base_m_s: Any,
        measured_force_base_n: Any,
        in_contact: bool,
    ) -> dict[str, Any]:
        dt = finite_scalar(dt_s, "dt_s")
        if not 0.0 < dt <= 0.004:
            raise NormalEstimatorError("estimator dt_s must be in (0, 4ms]")
        velocity = finite_vector3(measured_linear_velocity_base_m_s, "measured_linear_velocity_base_m_s")
        force = finite_vector3(measured_force_base_n, "measured_force_base_n")
        tangent_speed = float(np.linalg.norm(projector_tangent(self.normal) @ velocity))
        signed_load = float(np.dot(force, -self.normal))
        contact_gate = bool(in_contact) and signed_load >= self.contact_force_n
        excitation_gate = tangent_speed >= self.excitation_m_s
        motion_applied = False
        force_applied = False
        coplanarity_applied = False
        coplanarity_cross_norm = None
        coplanarity_residual = None
        if self.coplanarity_gain_s_inv > 0.0:
            # Combined motion+coplanarity gradient on one pre-update n.
            cross = np.cross(force, velocity)
            coplanarity_cross_norm = float(np.linalg.norm(cross))
            chat = None
            if coplanarity_cross_norm > self.coplanarity_cross_floor_n_m_s:
                chat = cross / coplanarity_cross_norm
                coplanarity_residual = float(np.dot(self.normal, chat))
            if contact_gate and excitation_gate:
                projector = projector_tangent(self.normal)
                denominator = max(float(velocity @ velocity), self.motion_normalization_floor_m_s**2)
                gradient = (
                    self.motion_gain
                    * float(np.dot(self.normal, velocity))
                    * (projector @ velocity)
                    / denominator
                )
                if chat is not None:
                    gradient = (
                        gradient
                        + self.coplanarity_gain_s_inv * coplanarity_residual * (projector @ chat)
                    )
                    coplanarity_applied = True
                if self.motion_gain > 0.0 or coplanarity_applied:
                    rate = float(np.linalg.norm(gradient))
                    if self.motion_rate_cap_rad_s is not None and rate > self.motion_rate_cap_rad_s:
                        gradient = gradient * (self.motion_rate_cap_rad_s / rate)
                    self.normal = normalize_unit(self.normal - dt * gradient, name="updated_normal")
                    motion_applied = self.motion_gain > 0.0
        elif contact_gate and excitation_gate and self.motion_gain > 0.0:
            # Projected gradient of 0.5 (n·v)^2 on the unit sphere.
            residual = float(np.dot(self.normal, velocity))
            if self.motion_normalization_floor_m_s is None:
                # Retain historical arithmetic exactly for original receipts.
                candidate = self.normal - dt * self.motion_gain * residual * velocity
            else:
                denominator = max(float(velocity @ velocity), self.motion_normalization_floor_m_s**2)
                gradient = self.motion_gain * residual * (projector_tangent(self.normal) @ velocity) / denominator
                rate = float(np.linalg.norm(gradient))
                if self.motion_rate_cap_rad_s is not None and rate > self.motion_rate_cap_rad_s:
                    gradient *= self.motion_rate_cap_rad_s / rate
                candidate = self.normal - dt * gradient
            self.normal = normalize_unit(candidate, name="updated_normal")
            motion_applied = True
        force_norm = float(np.linalg.norm(force))
        if (
            contact_gate
            and self.force_correction_gain > 0.0
            and self.force_correction_max_rad > 0.0
            and force_norm >= self.min_force_n
        ):
            # Environment-on-tool force points outward; inward candidate is -f.
            inward_force = normalize_unit(-force, name="inward_force")
            cosine = float(np.clip(np.dot(self.normal, inward_force), -1.0, 1.0))
            angle = math.acos(cosine)
            cone = math.atan(self.assumed_friction_mu) + 1e-9
            if angle <= cone:
                # Documented friction-bias correction, not geometry truth.
                tangent = inward_force - cosine * self.normal
                tangent_norm = float(np.linalg.norm(tangent))
                if tangent_norm > 1e-12:
                    step = min(
                        self.force_correction_max_rad,
                        self.force_correction_gain * dt * angle,
                    )
                    self.normal = normalize_unit(
                        self.normal + step * (tangent / tangent_norm),
                        name="friction_biased_normal",
                    )
                    force_applied = True
        diagnostic = {
            "inward_normal_base": tuple(float(value) for value in self.normal),
            "contact_gate": contact_gate,
            "excitation_gate": excitation_gate,
            "motion_update_applied": motion_applied,
            "force_bias_correction_applied": force_applied,
            "signed_normal_load_n": signed_load,
            "tangent_speed_m_s": tangent_speed,
            "friction_bias": {
                "assumed_mu": self.assumed_friction_mu,
                "max_step_rad": self.force_correction_max_rad,
                "claim": (
                    "bounded force-direction mix inside an assumed friction cone; "
                    "not a true-normal observation"
                ),
            },
        }
        if self.coplanarity_gain_s_inv > 0.0:
            # residual is n·(f×v)/|f×v| on the pre-update normal; None if degenerate.
            diagnostic["coplanarity_update_applied"] = coplanarity_applied
            diagnostic["coplanarity_cross_norm_n_m_s"] = coplanarity_cross_norm
            diagnostic["coplanarity_residual"] = coplanarity_residual
        return diagnostic
