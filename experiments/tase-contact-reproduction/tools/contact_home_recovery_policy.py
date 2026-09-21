"""Pure Home recovery policy: geometry plan, lift sample checks, relief force.

No device, network, or time-source I/O. Callers supply every sample and timestamp.
"""
from __future__ import annotations

from collections import deque
import math

import numpy as np

from contact_semantics import finite_vector6
from contact_yield_math import finite_scalar, require_rotation, so3_exp, so3_log
from contact_home_motion_profile import HOME_VERTICAL_SPEED_M_S


from contact_yield_task_frame import FIGURE8_CONTACT_HOME_XYZ_M

ORIGINAL_HOME_XYZ_M = FIGURE8_CONTACT_HOME_XYZ_M
MAX_SO3_ANGLE_RAD = 0.01
# A direct Home remains limited to the historical 10 mrad orientation
# corridor.  This larger bound is only for the explicitly staged route:
# vertical clearance is established first, force release is proved, and the
# attitude turn is then performed at clearance by the monitored Home owner.
STAGED_MAX_SO3_ANGLE_RAD = 0.020
MAX_TRANSFER_M = 0.08
MAX_RISE_M = 0.015
# The clearance-entry Home package has a separate, tighter 3 mm observed
# start-to-target bound.  Keep the initial staged plan compatible with that
# package after the vertical lift removes the rise component.
STAGED_MAX_LATERAL_M = 0.003
LIFT_LATERAL_M = 0.0005
LIFT_ANGULAR_RAD = 0.003
LIFT_SPEED_M_S = HOME_VERTICAL_SPEED_M_S
Z_BELOW_START_M = 0.0001
DOWNWARD_VZ_M_S = -0.0005
TASK_FORCE_LIMIT_N = 20.0
TORQUE_LIMIT_NM = 2.0
RELEASE_FORCE_N = 1.0
RELEASE_HOLD_S = 0.2
RELIEF_MIN_Z_N = 1.0
RELIEF_ALIGN_COS = math.cos(math.pi / 4.0)
NOISE_FLOOR_N = 0.5
NOISE_STD_GAIN = 5.0
NOISE_ALLOWANCE_MAX_N = 2.0
RATCHET_WINDOW = 5
# Corrected wrench is tool→base. Contact reaction is Base +Z; lifting +Z unloads it.
OUTWARD_Z = np.array((0.0, 0.0, 1.0))


def _copy6(values, name):
    return np.array(finite_vector6(values, name), dtype=float, copy=True)


def _so3_angle(rotvec_a, rotvec_b):
    return float(np.linalg.norm(so3_log(so3_exp(rotvec_a) @ so3_exp(rotvec_b).T)))


def _nonnegative6(values, name):
    vector = _copy6(values, name)
    if np.any(vector < 0.0):
        raise ValueError(f"{name} must be nonnegative")
    return vector


def plan_home_recovery(start_pose, home_pose):
    """Admit a vertical-first lift to Home Z without the obsolete 0.5 mm XY gate."""
    start = _copy6(start_pose, "start_pose")
    home = _copy6(home_pose, "home_pose")
    if not np.allclose(home[:3], ORIGINAL_HOME_XYZ_M, rtol=0.0, atol=1e-12):
        raise ValueError("original Home XYZ changed")
    transfer = float(np.linalg.norm(start[:3] - home[:3]))
    if transfer > MAX_TRANSFER_M:
        raise ValueError("Home transfer exceeds 80mm bound")
    if _so3_angle(home[3:], start[3:]) > MAX_SO3_ANGLE_RAD:
        raise ValueError("Home SO3 angle exceeds 10mrad")
    rise = float(home[2] - start[2])
    if rise > 0.0:
        if rise > MAX_RISE_M:
            raise ValueError("Home recovery rise exceeds 15mm")
        needs_lift = True
    else:
        needs_lift = False
    lift = start.copy()
    lift[2] = max(float(start[2]), float(home[2]))
    return {
        "needs_lift": needs_lift,
        "lift_pose": lift.tolist(),
        "home_pose": home.tolist(),
        "start_pose": start.tolist(),
    }


def plan_staged_home_recovery(start_pose, home_pose):
    """Plan the approved clearance-first route for a direct Home rejection.

    This is deliberately not a relaxed direct Home.  The start remains
    vertical-only until ``lift_pose`` reaches the existing Home Z; only then
    may the monitored Home program turn toward the approved attitude.  The
    initial lateral displacement is bounded to the clearance package's 3 mm
    corridor and the staged attitude bound is capped at 20 mrad.
    """
    start = _copy6(start_pose, "start_pose")
    home = _copy6(home_pose, "home_pose")
    if not np.allclose(home[:3], ORIGINAL_HOME_XYZ_M, rtol=0.0, atol=1e-12):
        raise ValueError("original Home XYZ changed")
    transfer = float(np.linalg.norm(start[:3] - home[:3]))
    if transfer > MAX_TRANSFER_M:
        raise ValueError("Home transfer exceeds 80mm bound")
    lateral = float(np.linalg.norm(start[:2] - home[:2]))
    if lateral > STAGED_MAX_LATERAL_M:
        raise ValueError("staged Home lateral transfer exceeds 3mm bound")
    angle = _so3_angle(home[3:], start[3:])
    if angle > STAGED_MAX_SO3_ANGLE_RAD:
        raise ValueError("staged Home SO3 angle exceeds 20mrad")
    # This route is used after the direct 10 mrad planner rejected the pose;
    # admitting an already-direct pose here would obscure which route ran.
    if angle <= MAX_SO3_ANGLE_RAD:
        raise ValueError("staged Home route is only for a direct SO3 rejection")
    rise = float(home[2] - start[2])
    if rise > MAX_RISE_M:
        raise ValueError("staged Home rise exceeds 15mm bound")
    lift = start.copy()
    lift[2] = max(float(start[2]), float(home[2]))
    return {
        "needs_lift": bool(rise > 0.0),
        "lift_pose": lift.tolist(),
        "home_pose": home.tolist(),
        "start_pose": start.tolist(),
        "staged_recovery": True,
        "route": "staged_clearance_orientation",
        "direct_home_max_so3_angle_rad": MAX_SO3_ANGLE_RAD,
        "staged_max_so3_angle_rad": STAGED_MAX_SO3_ANGLE_RAD,
        "staged_max_lateral_m": STAGED_MAX_LATERAL_M,
    }


def validate_lift_sample(plan, pose, twist, *, speed_limit_m_s=LIFT_SPEED_M_S):
    """Reject descent, sideways, or rotation while still below clearance.

    The default limit is the historical 40 mm/s vertical command. The live
    recovery owner may use a separately recorded, bounded transient allowance
    because UR RTDE can report a one-frame TCP-speed spike while the relief
    program is starting. Geometry, direction, force and package gates remain
    unchanged; callers must record the selected limit in the receipt.
    """
    if not isinstance(plan, dict):
        raise ValueError("lift plan is invalid")
    start = _copy6(plan["start_pose"], "plan.start_pose")
    lift = _copy6(plan["lift_pose"], "plan.lift_pose")
    current = _copy6(pose, "pose")
    speed = _copy6(twist, "twist")
    if current[2] < start[2] - Z_BELOW_START_M:
        raise ValueError("lift moved below start Z")
    if speed[2] < DOWNWARD_VZ_M_S:
        raise ValueError("lift downward velocity")
    at_clearance = bool(current[2] >= lift[2])
    if current[2] > lift[2] + 0.001:
        raise ValueError("lift exceeded clearance height")
    speed_limit = finite_scalar(speed_limit_m_s, "speed_limit_m_s")
    if speed_limit <= 0.0:
        raise ValueError("speed_limit_m_s must be positive")
    if float(np.linalg.norm(speed[:3])) > speed_limit:
        if math.isclose(speed_limit, LIFT_SPEED_M_S, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("lift speed exceeded historical 40mm/s")
        raise ValueError(f"lift speed exceeded {speed_limit:g}m/s")
    if float(np.linalg.norm(current[:2] - start[:2])) > LIFT_LATERAL_M:
        raise ValueError("lift left the 0.5mm lateral corridor")
    if _so3_angle(current[3:], start[3:]) > LIFT_ANGULAR_RAD:
        raise ValueError("lift orientation drifted more than 3mrad")
    return {"at_clearance": at_clearance}


class ReliefForceGuard:
    """Directional monitored relief above the 20 N task limit; failures are terminal."""

    def __init__(self, initial_raw_wrench, no_load_wrench, baseline_std_wrench, rotation):
        self._no_load = _copy6(no_load_wrench, "no_load_wrench")
        std = _nonnegative6(baseline_std_wrench, "baseline_std_wrench")
        self._rotation = np.array(require_rotation(rotation, "rotation"), dtype=float, copy=True)
        std_force_norm = float(np.linalg.norm(std[:3]))
        allowance = max(NOISE_FLOOR_N, NOISE_STD_GAIN * std_force_norm)
        if allowance > NOISE_ALLOWANCE_MAX_N:
            raise ValueError("baseline noise allowance exceeds 2N")
        self._allowance = allowance
        initial = _copy6(initial_raw_wrench, "initial_raw_wrench")
        raw_norm, corrected, corrected_norm, torque_norm = self._measure(initial)
        self._admit_entry(corrected, corrected_norm)
        self._raw_min = raw_norm
        self._corrected_min = corrected_norm
        self._raw_floor = float(np.linalg.norm(self._no_load[:3]))
        self._raw_ceiling = max(raw_norm, self._raw_floor) + allowance
        self._corrected_ceiling = corrected_norm + allowance
        self._raw_window = deque((raw_norm,), maxlen=RATCHET_WINDOW)
        self._corrected_window = deque((corrected_norm,), maxlen=RATCHET_WINDOW)
        self._last_time_s = None
        self._below_since_s = None
        self._released = False
        self._failed = False

    def update(self, raw_wrench, now_s):
        if self._failed:
            raise ValueError("relief force guard already failed")
        try:
            return self._update(raw_wrench, now_s)
        except Exception:
            self._failed = True
            raise

    def _update(self, raw_wrench, now_s):
        stamp = finite_scalar(now_s, "now_s")
        if self._last_time_s is not None and stamp <= self._last_time_s:
            raise ValueError("relief timestamps must be strictly increasing")
        raw = _copy6(raw_wrench, "raw_wrench")
        raw_norm, corrected, corrected_norm, torque_norm = self._measure(raw)
        if raw_norm > self._raw_ceiling or corrected_norm > self._corrected_ceiling:
            raise ValueError("relief force increased above ceiling")
        self._raw_window.append(raw_norm)
        self._corrected_window.append(corrected_norm)
        if len(self._raw_window) == RATCHET_WINDOW:
            raw_robust = float(np.median(self._raw_window))
            corrected_robust = float(np.median(self._corrected_window))
            if raw_robust < self._raw_min:
                self._raw_min = raw_robust
                self._raw_ceiling = max(self._raw_min, self._raw_floor) + self._allowance
            if corrected_robust < self._corrected_min:
                self._corrected_min = corrected_robust
                self._corrected_ceiling = self._corrected_min + self._allowance
        if corrected_norm < RELEASE_FORCE_N:
            if self._below_since_s is None:
                self._below_since_s = stamp
            if stamp - self._below_since_s >= RELEASE_HOLD_S:
                self._released = True
        else:
            self._below_since_s = None
            self._released = False
        self._last_time_s = stamp
        return {
            "raw_force_norm": raw_norm,
            "corrected_force_norm": corrected_norm,
            "corrected_force": corrected.tolist(),
            "torque_norm": torque_norm,
            "released": bool(self._released),
            "raw_force_ceiling": float(self._raw_ceiling),
            "corrected_force_ceiling": float(self._corrected_ceiling),
        }

    def _measure(self, raw):
        torque_norm = float(np.linalg.norm(raw[3:]))
        if torque_norm >= TORQUE_LIMIT_NM:
            raise ValueError("relief torque exceeds 2Nm")
        corrected = self._rotation @ (raw[:3] - self._no_load[:3])
        if not np.all(np.isfinite(corrected)):
            raise ValueError("corrected force is not finite")
        return float(np.linalg.norm(raw[:3])), corrected.copy(), float(np.linalg.norm(corrected)), torque_norm

    def _admit_entry(self, corrected, corrected_norm):
        if corrected_norm < RELEASE_FORCE_N:
            return
        if corrected[2] < RELIEF_MIN_Z_N:
            raise ValueError("overload is not directed into the workpiece")
        alignment = float(np.dot(corrected, OUTWARD_Z) / corrected_norm)
        if alignment < RELIEF_ALIGN_COS:
            raise ValueError("overload is not directed into the workpiece")
