"""Stateless R012 PATH constraints in the native task frame, before joint QP.

The actual published joint command supplies previous velocity; failed candidate
solves cannot advance guard state. Hard geometry is checked before soft CBF.
"""
import math
import numpy as np
from contact_semantics import finite_vector3, finite_vector6
from contact_yield_math import require_rotation
from step5d_autotune_v4_r012.safety_filter import SafetyFilterConfig, filter_path_error_twist


class YieldPathGuardError(RuntimeError):
    pass


class YieldPathGuard:
    def __init__(self, context):
        self.basis = require_rotation(context["basis"], "PATH basis")
        self.error = self.basis.T @ finite_vector3(context["error_base_m"], "PATH error")
        self.reference_velocity = self.basis.T @ finite_vector3(context["reference_velocity_base"], "PATH reference velocity")
        self.previous = self.basis.T @ finite_vector3(context["previous_velocity_base"], "PATH previous velocity")
        self.age = float(context["state_age_s"])
        self.config = SafetyFilterConfig()
        self.hard = float(sum((self.error[i] / self.config.ellipse_axes_m[i])**2 for i in range(2)))
        if not math.isfinite(self.age) or not 0 <= self.age <= self.config.max_state_age_s:
            raise YieldPathGuardError("stale or invalid PATH geometry")
        if self.hard >= 1.:
            raise YieldPathGuardError("PATH hard ellipse reached")
        self.soft = float(sum((self.error[i] / self.config.tightened_axes_m[i])**2 for i in range(2)))

    def project(self, twist):
        value = finite_vector6(twist, "PATH twist").copy()
        if self.soft < self.config.engage_deadband:
            return value
        local = self.basis.T @ value[:3]
        filtered, outcome = filter_path_error_twist(
            tuple(self.error[:2]), (0., 0.), tuple(self.reference_velocity[:2]),
            tuple(local) + tuple(value[3:]), tuple(self.previous[:2]),
            state_age_s=self.age, config=self.config)
        if not outcome.valid:
            raise YieldPathGuardError("PATH soft CBF infeasible: " + str(outcome.reason))
        value[:3] = self.basis @ np.asarray(filtered[:3])
        return value

    def validate(self, applied):
        checked = self.project(applied)
        if np.max(np.abs(checked - applied)) > 1e-8:
            raise YieldPathGuardError("joint QP output violates PATH CBF")

    def evidence(self, nominal, applied):
        return {"hard_ellipse_value": self.hard, "soft_ellipse_value": self.soft,
                "hard_axes_m": list(self.config.ellipse_axes_m),
                "soft_axes_m": list(self.config.tightened_axes_m),
                "intervention": bool(np.max(np.abs(applied - nominal)) > 1e-12),
                "constraint_frame": "native_task_basis", "state_age_s": self.age}
