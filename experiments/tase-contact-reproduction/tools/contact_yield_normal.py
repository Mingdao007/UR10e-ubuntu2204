"""Common surface-normal estimator.  Never consumes simulator geometry truth.

Initialization uses the contact-approach direction.  Updates use the measured
local motion tangent constraint on the unit sphere, gated by contact load and
tangential excitation.  A bounded force-direction correction is applied only
inside a documented friction cone; it is a bias, not a truth update.
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
        contact_force_n: float = 1.0,
        excitation_m_s: float = 0.002,
        motion_gain: float = 8.0,
        force_correction_gain: float = 1.5,
        force_correction_max_rad: float = 0.004,
        assumed_friction_mu: float = 0.35,
        min_force_n: float = 0.5,
    ) -> None:
        self.normal = normalize_unit(approach_inward_base, name="approach_inward_base")
        self.approach = self.normal.copy()
        self.contact_force_n = finite_scalar(contact_force_n, "contact_force_n")
        self.excitation_m_s = finite_scalar(excitation_m_s, "excitation_m_s")
        self.motion_gain = finite_scalar(motion_gain, "motion_gain")
        self.force_correction_gain = finite_scalar(force_correction_gain, "force_correction_gain")
        self.force_correction_max_rad = finite_scalar(force_correction_max_rad, "force_correction_max_rad")
        self.assumed_friction_mu = finite_scalar(assumed_friction_mu, "assumed_friction_mu")
        self.min_force_n = finite_scalar(min_force_n, "min_force_n")
        if any(value <= 0.0 for value in (
            self.contact_force_n,
            self.excitation_m_s,
            self.motion_gain,
            self.min_force_n,
        )):
            raise NormalEstimatorError("normal-estimator thresholds and motion gain must be positive")
        if self.force_correction_max_rad < 0.0 or self.assumed_friction_mu < 0.0:
            raise NormalEstimatorError("friction-bias bounds must be nonnegative")
        if self.force_correction_gain < 0.0:
            raise NormalEstimatorError("force correction gain must be nonnegative")

    def parameters(self) -> dict[str, float]:
        return {
            "contact_force_n": self.contact_force_n,
            "excitation_m_s": self.excitation_m_s,
            "motion_gain": self.motion_gain,
            "force_correction_gain": self.force_correction_gain,
            "force_correction_max_rad": self.force_correction_max_rad,
            "assumed_friction_mu": self.assumed_friction_mu,
            "min_force_n": self.min_force_n,
        }

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
        if contact_gate and excitation_gate:
            # Projected gradient of 0.5 (n·v)^2 on the unit sphere.
            residual = float(np.dot(self.normal, velocity))
            candidate = self.normal - dt * self.motion_gain * residual * velocity
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
        return {
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
