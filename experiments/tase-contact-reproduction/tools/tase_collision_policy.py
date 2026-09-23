"""Offline dual-space collision decision seam for the UR10e contact task.

The output is an intent for a future single bounded velocity writer, never a
live command. External joint torque must have an independently qualified
calibration and freshness path before this seam may be considered for live use.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

import numpy as np

from tase_sfc_fusion import FusionError, TaseSfcFusion, normal_tangent_projectors


POLICY_ID = "tase-dual-space-collision-offline-v1"


class CollisionPolicyError(ValueError):
    """An offline collision decision received invalid or missing evidence."""


def _vector(value: Any, length: int, name: str) -> np.ndarray:
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CollisionPolicyError(f"{name} must be a finite length-{length} vector") from exc
    if array.shape != (length,) or not np.isfinite(array).all():
        raise CollisionPolicyError(f"{name} must be a finite length-{length} vector")
    return array


@dataclass(frozen=True)
class CollisionIntent:
    mode: str
    cartesian_twist: tuple[float, ...] | None
    joint_yield_preference_rad_s: tuple[float, ...] | None
    tangent_frozen: bool
    recovery_requested: bool
    reason: str
    diagnostics: dict[str, Any]


class OfflineDualSpaceCollisionPolicy:
    """Stage task relaxation and projected joint admittance before realization."""

    def __init__(self, normal: Any, *, virtual_inertia: float,
                 joint_damping: float, joint_stiffness: float,
                 joint_velocity_cap_rad_s: float = .05):
        self._fusion = TaseSfcFusion(normal)
        for name, value in (("virtual_inertia", virtual_inertia),
                            ("joint_damping", joint_damping),
                            ("joint_stiffness", joint_stiffness),
                            ("joint_velocity_cap_rad_s", joint_velocity_cap_rad_s)):
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise CollisionPolicyError(f"{name} must be positive")
        self.virtual_inertia = float(virtual_inertia)
        self.joint_damping = float(joint_damping)
        self.joint_stiffness = float(joint_stiffness)
        self.joint_velocity_cap_rad_s = float(joint_velocity_cap_rad_s)
        self._mode = "NOMINAL"
        self._joint_reference: np.ndarray | None = None
        self._joint_rate = np.zeros(6)
        self._settled_s = 0.0

    @property
    def mode(self) -> str:
        return self._mode

    def reset_at_home(self, *, home_verified: bool) -> None:
        """Clear collision state only at a separately verified Home boundary."""
        if home_verified is not True:
            raise CollisionPolicyError("fresh verified joint Home required to reset collision state")
        self._fusion.reset("home")
        self._joint_reference = None
        self._joint_rate.fill(0.0)
        self._settled_s = 0.0
        self._mode = "NOMINAL"

    def _recovery(self, reason: str) -> CollisionIntent:
        self._mode = "RECOVERY_REQUEST"
        self._fusion.reset("abort")
        self._joint_rate.fill(0.0)
        return CollisionIntent(
            mode=self._mode, cartesian_twist=None, joint_yield_preference_rad_s=None,
            tangent_frozen=True, recovery_requested=True, reason=reason,
            diagnostics={"policy_id": POLICY_ID, "command_authority": "none",
                         "recovery_owner_required": True},
        )

    def step(self, *, phase: str, dt_s: float, normal: Any, tase_twist: Any,
             sfc_twist: Any, jacobian: Any, q_rad: Any,
             collision_site: str | None, collision_qualified: bool,
             external_joint_torque_nm: Any | None, normal_force_n: float,
             lateral_error_m: float, corridor_radius_m: float | None,
             guard_trip: bool = False,
             joint_observer_receipt: Mapping[str, Any] | None = None) -> CollisionIntent:
        """Return task intent; no qdot is dispatched from this object."""
        if self._mode == "RECOVERY_REQUEST":
            return self._recovery("recovery_owner_pending")
        try:
            dt = float(dt_s)
            force = float(normal_force_n)
            lateral = float(lateral_error_m)
            q = _vector(q_rad, 6, "q_rad")
            jac = np.asarray(jacobian, dtype=float)
            if jac.shape != (6, 6) or not np.isfinite(jac).all():
                raise CollisionPolicyError("jacobian must be finite 6x6")
            if not all(math.isfinite(x) for x in (dt, force, lateral)) or dt <= 0:
                raise CollisionPolicyError("dt and contact measurements must be finite")
            if not isinstance(phase, str) or not phase:
                raise CollisionPolicyError("phase is required")
            if collision_site not in {None, "link", "tool"} or type(collision_qualified) is not bool:
                raise CollisionPolicyError("collision identity is invalid")
            if type(guard_trip) is not bool:
                raise CollisionPolicyError("guard_trip must be bool")
            projectors = normal_tangent_projectors(normal)
            _vector(tase_twist, 6, "tase_twist")
            _vector(sfc_twist, 6, "sfc_twist")
        except (CollisionPolicyError, FusionError, ValueError) as exc:
            return self._recovery(f"invalid_input:{exc}")
        if dt > .02 or guard_trip:
            return self._recovery("stale_cycle_or_guard_trip")
        if collision_site is not None and not collision_qualified:
            return self._recovery("unqualified_collision_observation")
        responding = collision_site is not None or self._mode != "NOMINAL"
        if responding:
            if corridor_radius_m is None:
                return self._recovery("mark_corridor_unqualified")
            corridor = float(corridor_radius_m)
            if not math.isfinite(corridor) or corridor <= 0:
                return self._recovery("mark_corridor_unqualified")
            if force >= 1.0 and abs(lateral) > corridor:
                return self._recovery("loaded_tool_outside_mark_corridor")
            if phase.lower() != "path":
                return self._recovery("collision_outside_path")

        if collision_site is None and self._mode == "NOMINAL":
            try:
                fused = self._fusion.fuse(tase_twist, sfc_twist, normal, phase=phase)
            except FusionError as exc:
                return self._recovery(f"fusion_invalid:{exc}")
            return CollisionIntent(
                mode="NOMINAL", cartesian_twist=fused.twist,
                joint_yield_preference_rad_s=None, tangent_frozen=False,
                recovery_requested=False, reason="nominal_task",
                diagnostics={"policy_id": POLICY_ID, "fusion": fused.diagnostics,
                             "command_authority": "offline_intent_only"},
            )

        if collision_site is not None:
            self._settled_s = 0.0
            if self._mode == "NOMINAL":
                self._joint_reference = q.copy()
                self._joint_rate.fill(0.0)
            self._mode = "LINK_YIELD" if collision_site == "link" else "TOOL_HOLD"
        else:
            self._settled_s += dt
            if self._settled_s >= .5:
                self._mode = "NOMINAL"
                self._joint_reference = None
                self._joint_rate.fill(0.0)
                try:
                    fused = self._fusion.fuse(tase_twist, sfc_twist, normal, phase=phase)
                except FusionError as exc:
                    return self._recovery(f"fusion_invalid:{exc}")
                return CollisionIntent(
                    mode="NOMINAL", cartesian_twist=fused.twist,
                    joint_yield_preference_rad_s=None, tangent_frozen=False,
                    recovery_requested=False, reason="settled_after_collision",
                    diagnostics={"policy_id": POLICY_ID,
                                 "command_authority": "offline_intent_only"},
                )

        # The tangent path yields first. The normal and orientation task is
        # still only an intent; a future bounded realization may reject it.
        try:
            fused = self._fusion.fuse(tase_twist, np.zeros(6), normal, phase="PATH")
        except FusionError as exc:
            return self._recovery(f"fusion_invalid:{exc}")
        joint_preference = None
        if self._mode == "LINK_YIELD":
            receipt = joint_observer_receipt or {}
            if (not isinstance(receipt, Mapping)
                    or receipt.get("admission_passed") is not True
                    or receipt.get("sample_fresh") is not True
                    or not all(isinstance(receipt.get(key), str) and receipt[key]
                               for key in ("calibration_sha256", "dynamics_model_sha256",
                                           "timestamps_sha256"))):
                return self._recovery("joint_observer_not_qualified")
            if external_joint_torque_nm is None:
                return self._recovery("joint_torque_observation_missing")
            try:
                torque = _vector(external_joint_torque_nm, 6, "external_joint_torque_nm")
            except CollisionPolicyError as exc:
                return self._recovery(f"invalid_joint_torque:{exc}")
            if self._joint_reference is None:
                return self._recovery("joint_reference_missing")
            acceleration = (torque - self.joint_damping * self._joint_rate
                            - self.joint_stiffness * (q - self._joint_reference)) / self.virtual_inertia
            self._joint_rate = np.clip(
                self._joint_rate + dt * acceleration,
                -self.joint_velocity_cap_rad_s, self.joint_velocity_cap_rad_s,
            )
            # Preserve one normal translation and three orientation axes.
            # On a six-axis arm the remaining two directions are tangent task
            # slack, not a null space of the complete end-effector task.
            normal_row = np.asarray(projectors.normal) @ jac[:3, :]
            primary = np.vstack((normal_row, jac[3:, :]))
            if np.linalg.matrix_rank(primary, tol=1e-6) < 4:
                return self._recovery("primary_task_rank_lost")
            null_projector = np.eye(6) - np.linalg.pinv(primary) @ primary
            joint_preference = tuple(float(x) for x in null_projector @ self._joint_rate)
        return CollisionIntent(
            mode=self._mode, cartesian_twist=fused.twist,
            joint_yield_preference_rad_s=joint_preference,
            tangent_frozen=True, recovery_requested=False,
            reason="qualified_collision_task_relaxation",
            diagnostics={"policy_id": POLICY_ID, "fusion": fused.diagnostics,
                         "primary_task": "normal_translation_plus_orientation",
                         "command_authority": "offline_intent_only"},
        )


__all__ = ["POLICY_ID", "CollisionPolicyError", "CollisionIntent",
           "OfflineDualSpaceCollisionPolicy"]
