"""Pure control-math core for the ROS2-remote Step5b live-contact runner.

Increment 1a of the staged ROS2-remote Step5b runner. This module is a FAITHFUL
port of the proven Step5b contact-control primitives from
`experiments/tase-contact-reproduction/tools/kunwei_rtde_bridge.py` (the 500Hz
RTDE speedl force loop that is the executable spec). It contains ONLY pure
functions — no `rclpy`, no sockets, no RTDE, no robot motion — so a future ROS2
node (increment 2) and the offline replay validator (increment 1b) both call the
same code.

Scope of 1a: the frame/vector helpers, the cycloid reference math, the live
normal candidate, and the v31 filtered-live normal — i.e. the layer where the
Step5d v4 force/frame semantic bug lived. The contact latch (`cmd_valid`) and the
force-tracking commanded twist live in `compute_bridge_values` and are ported in
increment 1b, where correctness is proven by replaying the bridge CSVs.

Faithfulness: each function below preserves the bridge's math, signs, units, and
fallbacks exactly. Provenance line numbers refer to `kunwei_rtde_bridge.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Any

# Step5b cycloid reference constants (kunwei_rtde_bridge.py step4e_path_reference,
# "cycloid" branch, lines ~618-627): phase = 0.1 * path_time_s, amplitude 0.015 m.
CYCLOID_OMEGA_RAD_S = 0.1
CYCLOID_AMPLITUDE_M = 0.015

# v31 filtered-live normal defaults (v31_filtered_live_normal, lines ~816-841).
V31_FILTER_ALPHA = 0.35
V31_MIN_FORCE_N = 2.0

Vec3 = tuple[float, float, float]


def clamp(value: float, lo: float, hi: float) -> float:
    """kunwei_rtde_bridge.py:448."""
    return lo if value < lo else hi if value > hi else value


def dot3(a: Vec3 | list[float], b: Vec3 | list[float]) -> float:
    """kunwei_rtde_bridge.py:652."""
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross3(a: Vec3 | list[float], b: Vec3 | list[float]) -> Vec3:
    """kunwei_rtde_bridge.py:656."""
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def norm3(values: Vec3 | list[float]) -> float:
    """kunwei_rtde_bridge.py:667."""
    return math.sqrt(dot3(values, values))


def normalize3(values: Vec3 | list[float], fallback: Vec3 = (0.0, 0.0, 1.0)) -> Vec3:
    """kunwei_rtde_bridge.py:671."""
    length = norm3(values)
    if length < 1e-9:
        return fallback
    return (values[0] / length, values[1] / length, values[2] / length)


def mat_vec3(matrix: list[list[float]], vector: Vec3 | list[float]) -> Vec3:
    """kunwei_rtde_bridge.py:681."""
    return (dot3(matrix[0], vector), dot3(matrix[1], vector), dot3(matrix[2], vector))


def rotvec_to_matrix(rx: float, ry: float, rz: float) -> list[list[float]]:
    """kunwei_rtde_bridge.py:696 (Rodrigues)."""
    theta = math.sqrt(rx * rx + ry * ry + rz * rz)
    if theta < 1e-12:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    kx, ky, kz = rx / theta, ry / theta, rz / theta
    c = math.cos(theta)
    s = math.sin(theta)
    v = 1.0 - c
    return [
        [c + kx * kx * v, kx * ky * v - kz * s, kx * kz * v + ky * s],
        [ky * kx * v + kz * s, c + ky * ky * v, ky * kz * v - kx * s],
        [kz * kx * v - ky * s, kz * ky * v + kx * s, c + kz * kz * v],
    ]


def cycloid_reference(path_time_s: float) -> dict[str, float]:
    """Step5b cycloid reference in along/lateral frame coordinates.

    Faithful to kunwei_rtde_bridge.py step4e_path_reference "cycloid" branch
    (lines ~619-623). Returned along/lateral values are later mapped into the
    base frame via the Step5 safe-frame basis (ported in increment 1b alongside
    the latch/twist, where it is validated against the logged _step4e_desired_*
    columns). Kept frame-agnostic here so the math is independently testable.
    """
    phase = CYCLOID_OMEGA_RAD_S * path_time_s
    return {
        "phase_rad": phase,
        "along_m": CYCLOID_AMPLITUDE_M * (phase - math.sin(phase)),
        "lateral_m": CYCLOID_AMPLITUDE_M * (1.0 - math.cos(phase)),
        "along_v_m_s": CYCLOID_OMEGA_RAD_S * CYCLOID_AMPLITUDE_M * (1.0 - math.cos(phase)),
        "lateral_v_m_s": CYCLOID_OMEGA_RAD_S * CYCLOID_AMPLITUDE_M * math.sin(phase),
    }


def reaction_normal(filtered_normal_b: Vec3) -> Vec3:
    """Contact force-frame contract: reaction_normal carries positive load.

    The filtered live normal IS the reaction normal (unit vector along the
    sensed reaction force). Source contract:
    `/home/andy/.codex/context/ur-contact-force-frame-contract.md`.
    """
    return normalize3(filtered_normal_b)


def approach_normal(filtered_normal_b: Vec3) -> Vec3:
    """approach_normal = -reaction_normal (posture/press direction).

    This sign is the exact site of the Step5d v4 semantic bug; it is asserted in
    the unit tests. Do NOT normalize a raw force into a posture target.
    """
    r = reaction_normal(filtered_normal_b)
    return (-r[0], -r[1], -r[2])


def normal_load_n(force_base: Vec3, filtered_normal_b: Vec3) -> float:
    """normal_load_n = dot(force_base, reaction_normal). Positive = load."""
    return dot3(force_base, reaction_normal(filtered_normal_b))


def live_normal_candidate(force_b: Vec3, *, friction_projection: bool) -> tuple[Vec3, Vec3, float]:
    """kunwei_rtde_bridge.py:800. Returns (raw_normal_b, candidate_b, candidate_force_n).

    `tangent_b` uses the Step4e line unit; for the Step5b cycloid baseline the
    bridge runs with friction_projection disabled (filtered_live), so the
    tangent branch is inert. Kept faithful for parity with the source.
    """
    raw_normal_b = normalize3(force_b)
    candidate_force_b: Vec3 = force_b
    if friction_projection:
        tangent_b = (_STEP4E_LINE_UNIT_XY[0], _STEP4E_LINE_UNIT_XY[1], 0.0)
        tangent_load = dot3(force_b, tangent_b)
        candidate_force_b = tuple(force_b[idx] - tangent_load * tangent_b[idx] for idx in range(3))  # type: ignore[assignment]
    candidate_force_n = norm3(candidate_force_b)
    candidate_b = normalize3(candidate_force_b, raw_normal_b)
    return raw_normal_b, candidate_b, candidate_force_n


def v31_filtered_live_normal(
    filtered_current_b: Vec3,
    live_candidate_b: Vec3,
    live_candidate_force_n: float,
    *,
    sensor_ok: float,
    alpha: float = V31_FILTER_ALPHA,
    min_force_n: float = V31_MIN_FORCE_N,
) -> tuple[Vec3, str]:
    """kunwei_rtde_bridge.py:816. Holds on stale/low-force/reverse, else EMA-blends.

    The three hold conditions are the proven Step5b safety behavior; preserve
    them exactly.
    """
    if sensor_ok <= 0.5:
        return filtered_current_b, "hold_stale"
    if live_candidate_force_n < min_force_n:
        return filtered_current_b, "hold_low_force"
    if dot3(filtered_current_b, live_candidate_b) < 0.0:
        return filtered_current_b, "hold_reverse"
    alpha = clamp(alpha, 0.0, 1.0)
    blended = tuple(
        (1.0 - alpha) * filtered_current_b[idx] + alpha * live_candidate_b[idx] for idx in range(3)
    )
    return normalize3(blended, filtered_current_b), "filtered_live_alpha"  # type: ignore[arg-type]


STEP4E_START_XY = (0.43301, 0.10802)
STEP4E_END_XY = (0.49274, 0.23877)
STEP4E_LINE_DX = STEP4E_END_XY[0] - STEP4E_START_XY[0]
STEP4E_LINE_DY = STEP4E_END_XY[1] - STEP4E_START_XY[1]
STEP4E_LINE_LENGTH_M = math.hypot(STEP4E_LINE_DX, STEP4E_LINE_DY)
STEP4E_LINE_UNIT_XY: tuple[float, float] = (
    STEP4E_LINE_DX / STEP4E_LINE_LENGTH_M,
    STEP4E_LINE_DY / STEP4E_LINE_LENGTH_M,
)
_STEP4E_LINE_UNIT_XY = STEP4E_LINE_UNIT_XY


@dataclass(frozen=True)
class Step5bContactParams:
    """Pure Step5b bridge parameter bundle.

    Defaults match the retained Step5b bridge runs. The CLI owns metadata file
    I/O; this class only normalizes an already-loaded `metadata["args"]` mapping.
    """

    bridge_mode: str = "line"
    bridge_profile: str = "step5b_v1"
    bridge_path_shape: str = "cycloid"
    target_force_n: float = 5.0
    rtde_hz: float = 500.0
    bridge_line_settle_s: float = 0.0
    bridge_path_p_gain: float = 1.5
    bridge_motion_limit_m_s: float = 0.004
    bridge_total_linear_limit_m_s: float = 0.006
    bridge_normal_velocity_limit_m_s: float = 0.003
    bridge_force_p_gain: float = 0.0007
    bridge_force_i_gain: float = 0.00008
    bridge_force_damping: float = 0.35
    bridge_normal_command_sign: float = 1.0
    bridge_integral_limit_n_s: float = 10.0
    bridge_min_force_for_control_n: float = 1.0
    bridge_acquire_grace_s: float = 0.25
    bridge_reacquire_velocity_m_s: float = 0.001
    bridge_orientation_gain: float = 0.20
    bridge_orientation_wx_sign: float = 1.0
    bridge_orientation_wy_sign: float = 1.0
    bridge_angular_limit_rad_s: float = 0.015
    bridge_integrate_stage25_only: bool = False
    bridge_normal_follow_mode: str = "filtered_live"
    bridge_normal_filter_alpha: float = V31_FILTER_ALPHA
    bridge_normal_min_force_n: float = V31_MIN_FORCE_N
    bridge_normal_friction_projection: str = "on"
    duration_s: float = 60.0
    amplitude_m: float = CYCLOID_AMPLITUDE_M
    omega_rad_s: float = CYCLOID_OMEGA_RAD_S
    stage_id: str = "step5_contact_cycloid_baseline_v1"

    @property
    def sample_period_s(self) -> float:
        return 1.0 / self.rtde_hz

    @classmethod
    def from_metadata_args(
        cls,
        args: dict[str, Any],
        *,
        stage: dict[str, Any] | None = None,
    ) -> "Step5bContactParams":
        def value(name: str, default: Any) -> Any:
            aliases = (name, name.replace("bridge_", "step4e_", 1))
            for key in aliases:
                if key in args:
                    return args[key]
            return default

        def f(name: str, default: float) -> float:
            return float(value(name, default))

        def b(name: str, default: bool) -> bool:
            raw = value(name, default)
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                return raw.lower() in {"1", "true", "yes", "on"}
            return bool(raw)

        duration_s = cls.duration_s
        amplitude_m = cls.amplitude_m
        omega_rad_s = cls.omega_rad_s
        stage_id = cls.stage_id
        if stage is not None:
            stage_id = str(stage.get("id", stage_id))
            duration_s = float(stage.get("duration_s", duration_s))
            amplitude_m = float(stage.get("amplitude_m", amplitude_m))
            phase_law = stage.get("phase_law", {})
            if isinstance(phase_law, dict):
                omega_rad_s = float(phase_law.get("omega_rad_s", omega_rad_s))

        return cls(
            bridge_mode=str(value("bridge_mode", cls.bridge_mode)),
            bridge_profile=str(value("bridge_profile", value("bridge_version", value("step4e_version", cls.bridge_profile)))),
            bridge_path_shape=str(value("bridge_path_shape", cls.bridge_path_shape)),
            target_force_n=f("target_force_n", cls.target_force_n),
            rtde_hz=f("rtde_hz", cls.rtde_hz),
            bridge_line_settle_s=f("bridge_line_settle_s", cls.bridge_line_settle_s),
            bridge_path_p_gain=f("bridge_path_p_gain", cls.bridge_path_p_gain),
            bridge_motion_limit_m_s=f("bridge_motion_limit_m_s", cls.bridge_motion_limit_m_s),
            bridge_total_linear_limit_m_s=f("bridge_total_linear_limit_m_s", cls.bridge_total_linear_limit_m_s),
            bridge_normal_velocity_limit_m_s=f("bridge_normal_velocity_limit_m_s", cls.bridge_normal_velocity_limit_m_s),
            bridge_force_p_gain=f("bridge_force_p_gain", cls.bridge_force_p_gain),
            bridge_force_i_gain=f("bridge_force_i_gain", cls.bridge_force_i_gain),
            bridge_force_damping=f("bridge_force_damping", cls.bridge_force_damping),
            bridge_normal_command_sign=f("bridge_normal_command_sign", cls.bridge_normal_command_sign),
            bridge_integral_limit_n_s=f("bridge_integral_limit_n_s", cls.bridge_integral_limit_n_s),
            bridge_min_force_for_control_n=f("bridge_min_force_for_control_n", cls.bridge_min_force_for_control_n),
            bridge_acquire_grace_s=f("bridge_acquire_grace_s", cls.bridge_acquire_grace_s),
            bridge_reacquire_velocity_m_s=f("bridge_reacquire_velocity_m_s", cls.bridge_reacquire_velocity_m_s),
            bridge_orientation_gain=f("bridge_orientation_gain", cls.bridge_orientation_gain),
            bridge_orientation_wx_sign=f("bridge_orientation_wx_sign", cls.bridge_orientation_wx_sign),
            bridge_orientation_wy_sign=f("bridge_orientation_wy_sign", cls.bridge_orientation_wy_sign),
            bridge_angular_limit_rad_s=f("bridge_angular_limit_rad_s", cls.bridge_angular_limit_rad_s),
            bridge_integrate_stage25_only=b("bridge_integrate_stage25_only", cls.bridge_integrate_stage25_only),
            bridge_normal_follow_mode=str(value("bridge_normal_follow_mode", cls.bridge_normal_follow_mode)),
            bridge_normal_filter_alpha=f("bridge_normal_filter_alpha", cls.bridge_normal_filter_alpha),
            bridge_normal_min_force_n=f("bridge_normal_min_force_n", cls.bridge_normal_min_force_n),
            bridge_normal_friction_projection=str(value("bridge_normal_friction_projection", cls.bridge_normal_friction_projection)),
            duration_s=duration_s,
            amplitude_m=amplitude_m,
            omega_rad_s=omega_rad_s,
            stage_id=stage_id,
        )


@dataclass(frozen=True)
class Step5bPathBasis:
    origin_xy_m: tuple[float, float]
    u_along_xy: tuple[float, float]
    p_lateral_xy: tuple[float, float]

    @classmethod
    def from_safe_frame(cls, frame: dict[str, Any]) -> "Step5bPathBasis":
        basis = frame["basis"]
        return cls(
            origin_xy_m=tuple(float(v) for v in basis["origin_xy_m"]),  # type: ignore[arg-type]
            u_along_xy=tuple(float(v) for v in basis["u_along_xy"]),  # type: ignore[arg-type]
            p_lateral_xy=tuple(float(v) for v in basis["p_lateral_xy"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class Step5bContactState:
    integral_error_n_s: float = 0.0
    normal_velocity_m_s: float = 0.0
    latched_normal_b: Vec3 | None = None
    filtered_normal_b: Vec3 | None = None
    latched_normal_locked: bool = False
    normal_acquired: bool = False
    line_stage_s: float = 0.0
    last_robot_stage: float | None = None

    def reset_line_contact(self) -> "Step5bContactState":
        return Step5bContactState()


@dataclass(frozen=True)
class Step5bSample:
    tcp_pose: tuple[float, float, float, float, float, float]
    tcp_wrench: tuple[float, float, float, float, float, float]
    sensor_ok: float
    robot_stage: float
    dt_s: float
    tcp_speed: tuple[float, float, float, float, float, float] | None = None


@dataclass(frozen=True)
class Step5bContactResult:
    cmd_valid: float
    cmd_vx_m_s: float
    cmd_vy_m_s: float
    cmd_vz_m_s: float
    cmd_wx_rad_s: float
    cmd_wy_rad_s: float
    cmd_wz_rad_s: float
    progress_m: float
    path_time_s: float
    force_error_n: float
    orientation_error_rad: float
    controller_state: float
    normal_load_n: float
    force_norm_n: float
    line_stage_s: float
    desired_x_m: float
    desired_y_m: float
    desired_vx_m_s: float
    desired_vy_m_s: float
    path_error_x_m: float
    path_error_y_m: float
    normal_filter_source: str
    normal_follow_mode: str
    hold_reason: str
    latched_normal_b: Vec3 | None
    filtered_normal_b: Vec3 | None
    control_normal_b: Vec3
    reaction_normal_b: Vec3
    approach_normal_b: Vec3
    live_normal_candidate_force_n: float
    live_normal_candidate_angle_rad: float | None
    live_normal_angle_from_latch_rad: float | None

    @property
    def twist(self) -> tuple[float, float, float, float, float, float]:
        return (
            self.cmd_vx_m_s,
            self.cmd_vy_m_s,
            self.cmd_vz_m_s,
            self.cmd_wx_rad_s,
            self.cmd_wy_rad_s,
            self.cmd_wz_rad_s,
        )


def _xy_from_basis(
    basis: Step5bPathBasis,
    along_m: float,
    lateral_m: float,
) -> tuple[float, float]:
    return (
        basis.origin_xy_m[0] + along_m * basis.u_along_xy[0] + lateral_m * basis.p_lateral_xy[0],
        basis.origin_xy_m[1] + along_m * basis.u_along_xy[1] + lateral_m * basis.p_lateral_xy[1],
    )


def step5b_path_reference(
    pose_xy: tuple[float, float],
    elapsed_s: float,
    params: Step5bContactParams,
    basis: Step5bPathBasis,
) -> dict[str, Any]:
    t_s = clamp(float(elapsed_s), 0.0, params.duration_s)
    phase = params.omega_rad_s * t_s
    along_m = params.amplitude_m * (phase - math.sin(phase))
    lateral_m = params.amplitude_m * (1.0 - math.cos(phase))
    along_v = params.amplitude_m * params.omega_rad_s * (1.0 - math.cos(phase))
    lateral_v = params.amplitude_m * params.omega_rad_s * math.sin(phase)
    desired_x, desired_y = _xy_from_basis(basis, along_m, lateral_m)
    desired_vx = along_v * basis.u_along_xy[0] + lateral_v * basis.p_lateral_xy[0]
    desired_vy = along_v * basis.u_along_xy[1] + lateral_v * basis.p_lateral_xy[1]
    return {
        "stage_id": params.stage_id,
        "progress": t_s,
        "path_time_s": t_s,
        "phase_rad": phase,
        "desired_xy": (desired_x, desired_y),
        "desired_velocity_xy": (desired_vx, desired_vy),
        "path_error_xy": (desired_x - pose_xy[0], desired_y - pose_xy[1]),
        "local": {
            "path_time_s": t_s,
            "progress": t_s,
            "phase_rad": phase,
            "local_x_m": along_m,
            "local_y_m": lateral_m,
            "local_vx_m_s": along_v,
            "local_vy_m_s": lateral_v,
        },
    }


def angle_between_unit(source: Vec3, target: Vec3) -> float:
    src = normalize3(source)
    dst = normalize3(target)
    return math.acos(clamp(dot3(src, dst), -1.0, 1.0))


def _stage_flags(robot_stage: float, params: Step5bContactParams) -> dict[str, bool]:
    line_mode = params.bridge_mode == "line"
    angular_speedl_profile = params.bridge_profile == "step5b_v1"
    detached_profile = angular_speedl_profile
    first_search = line_mode and angular_speedl_profile and (
        abs(robot_stage - 24.0) < 0.05 or abs(robot_stage - 24.2) < 0.05
    )
    latch = line_mode and detached_profile and abs(robot_stage - 25.05) < 0.03
    detach = line_mode and detached_profile and abs(robot_stage - 25.1) < 0.03
    orient = line_mode and detached_profile and abs(robot_stage - 25.2) < 0.05
    acquire = line_mode and angular_speedl_profile and abs(robot_stage - 25.3) < 0.05
    line = line_mode and abs(robot_stage - 25.0) < 0.05
    line_entry = angular_speedl_profile and acquire
    control = latch or detach or orient or acquire or line
    return {
        "first_search": first_search,
        "latch": latch,
        "detach": detach,
        "orient": orient,
        "acquire": acquire,
        "line": line,
        "line_entry": line_entry,
        "control": control,
        "detached": detached_profile,
        "angular_speedl": angular_speedl_profile,
    }


def _zero_result(
    state: Step5bContactState,
    sample: Step5bSample,
    params: Step5bContactParams,
    basis: Step5bPathBasis,
    *,
    controller_state: float,
    normal_filter_source: str = "inactive",
    hold_reason: str = "not_control_allowed",
) -> Step5bContactResult:
    rotation = rotvec_to_matrix(sample.tcp_pose[3], sample.tcp_pose[4], sample.tcp_pose[5])
    force_t = sample.tcp_wrench[:3]
    force_b = mat_vec3(rotation, force_t)
    n_reaction_b = normalize3(force_b)
    n_control_b = state.latched_normal_b if state.latched_normal_b is not None else n_reaction_b
    ref = step5b_path_reference((sample.tcp_pose[0], sample.tcp_pose[1]), state.line_stage_s, params, basis)
    approach_b = (-n_control_b[0], -n_control_b[1], -n_control_b[2])
    return Step5bContactResult(
        cmd_valid=0.0,
        cmd_vx_m_s=0.0,
        cmd_vy_m_s=0.0,
        cmd_vz_m_s=0.0,
        cmd_wx_rad_s=0.0,
        cmd_wy_rad_s=0.0,
        cmd_wz_rad_s=0.0,
        progress_m=float(ref["progress"]),
        path_time_s=float(ref["path_time_s"]),
        force_error_n=params.target_force_n,
        orientation_error_rad=0.0,
        controller_state=controller_state,
        normal_load_n=0.0,
        force_norm_n=norm3(force_t),
        line_stage_s=state.line_stage_s,
        desired_x_m=float(ref["desired_xy"][0]),
        desired_y_m=float(ref["desired_xy"][1]),
        desired_vx_m_s=float(ref["desired_velocity_xy"][0]),
        desired_vy_m_s=float(ref["desired_velocity_xy"][1]),
        path_error_x_m=float(ref["path_error_xy"][0]),
        path_error_y_m=float(ref["path_error_xy"][1]),
        normal_filter_source=normal_filter_source,
        normal_follow_mode=params.bridge_normal_follow_mode,
        hold_reason=hold_reason,
        latched_normal_b=state.latched_normal_b,
        filtered_normal_b=state.filtered_normal_b,
        control_normal_b=n_control_b,
        reaction_normal_b=n_reaction_b,
        approach_normal_b=approach_b,
        live_normal_candidate_force_n=0.0,
        live_normal_candidate_angle_rad=None,
        live_normal_angle_from_latch_rad=None,
    )


def compute_step5b_contact_sample(
    sample: Step5bSample,
    state: Step5bContactState,
    params: Step5bContactParams,
    basis: Step5bPathBasis,
) -> tuple[Step5bContactResult, Step5bContactState]:
    """Pure Step5b contact-control sample update.

    Ported from `kunwei_rtde_bridge.compute_bridge_values` for the Step5b
    profile only. It performs no I/O and never sends a command.
    """
    robot_stage = float(sample.robot_stage)
    if params.bridge_mode not in {"line", "axis_iso"} or (
        math.isfinite(robot_stage) and (robot_stage < 24.0 or robot_stage >= 26.0)
    ):
        state = state.reset_line_contact()

    flags = _stage_flags(robot_stage, params)
    if math.isfinite(robot_stage):
        if state.last_robot_stage is None or abs(robot_stage - state.last_robot_stage) >= 0.03:
            state = replace(state, line_stage_s=0.0, last_robot_stage=robot_stage)
    if flags["control"]:
        state = replace(state, line_stage_s=state.line_stage_s + max(0.0, sample.dt_s))

    force_t = sample.tcp_wrench[:3]
    rotation = rotvec_to_matrix(sample.tcp_pose[3], sample.tcp_pose[4], sample.tcp_pose[5])
    force_b = mat_vec3(rotation, force_t)
    force_abs = norm3(force_t)
    n_reaction_b = normalize3(force_b)
    raw_live_normal_b, live_candidate_b, live_candidate_force_n = live_normal_candidate(
        force_b,
        friction_projection=params.bridge_normal_friction_projection == "on",
    )

    if (
        flags["detached"]
        and (flags["latch"] or flags["first_search"])
        and sample.sensor_ok > 0.5
        and force_abs >= params.bridge_min_force_for_control_n
        and not state.latched_normal_locked
    ):
        state = replace(
            state,
            latched_normal_b=n_reaction_b,
            latched_normal_locked=True,
            normal_acquired=True,
        )
    if state.latched_normal_b is not None and state.filtered_normal_b is None:
        state = replace(state, filtered_normal_b=state.latched_normal_b)

    n_control_b = state.latched_normal_b if state.latched_normal_b is not None else n_reaction_b
    normal_filter_source = "locked"
    hold_reason = ""
    live_candidate_angle_rad: float | None = None
    live_candidate_angle_from_latch_rad: float | None = None
    normal_follow_active = (
        params.bridge_profile == "step5b_v1"
        and params.bridge_normal_follow_mode == "filtered_live"
        and flags["line"]
        and state.normal_acquired
        and state.latched_normal_b is not None
    )
    if normal_follow_active:
        filtered_current = state.filtered_normal_b if state.filtered_normal_b is not None else state.latched_normal_b
        live_candidate_angle_rad = angle_between_unit(filtered_current, live_candidate_b)
        live_candidate_angle_from_latch_rad = angle_between_unit(state.latched_normal_b, live_candidate_b)
        filtered_normal_b, normal_filter_source = v31_filtered_live_normal(
            filtered_current,
            live_candidate_b,
            live_candidate_force_n,
            sensor_ok=sample.sensor_ok,
            alpha=params.bridge_normal_filter_alpha,
            min_force_n=params.bridge_normal_min_force_n,
        )
        hold_reason = normal_filter_source if normal_filter_source.startswith("hold_") else ""
        state = replace(state, filtered_normal_b=filtered_normal_b)
        n_control_b = filtered_normal_b
    elif params.bridge_profile == "step5b_v1" and params.bridge_normal_follow_mode == "filtered_live":
        normal_filter_source = "locked_no_latch" if flags["line"] else "locked_pre_line"

    normal_load = max(0.0, dot3(force_b, n_control_b)) if state.normal_acquired else 0.0
    tcp_z_axis_b = (rotation[0][2], rotation[1][2], rotation[2][2])
    orientation_target_axis_b = (-n_control_b[0], -n_control_b[1], -n_control_b[2])
    orientation_axis = cross3(tcp_z_axis_b, orientation_target_axis_b)
    orientation_error = math.atan2(
        norm3(orientation_axis),
        clamp(dot3(tcp_z_axis_b, orientation_target_axis_b), -1.0, 1.0),
    )
    orientation_cmd: Vec3 = (
        params.bridge_orientation_gain * params.bridge_orientation_wx_sign * orientation_axis[0],
        params.bridge_orientation_gain * params.bridge_orientation_wy_sign * orientation_axis[1],
        params.bridge_orientation_gain * orientation_axis[2],
    )
    orientation_norm = norm3(orientation_cmd)
    if orientation_norm > params.bridge_angular_limit_rad_s:
        scale = params.bridge_angular_limit_rad_s / orientation_norm
        orientation_cmd = tuple(value * scale for value in orientation_cmd)  # type: ignore[assignment]

    path_ref = step5b_path_reference((sample.tcp_pose[0], sample.tcp_pose[1]), state.line_stage_s, params, basis)
    progress = float(path_ref["progress"])
    desired_x, desired_y = path_ref["desired_xy"]
    desired_vx, desired_vy = path_ref["desired_velocity_xy"]
    path_error = (float(path_ref["path_error_xy"][0]), float(path_ref["path_error_xy"][1]), 0.0)
    desired_velocity_xy = (float(desired_vx), float(desired_vy)) if flags["line"] else (0.0, 0.0)
    if flags["line"] and state.line_stage_s <= params.bridge_line_settle_s:
        desired_velocity_xy = (0.0, 0.0)
    if flags["detached"] and not flags["line"]:
        base_motion: Vec3 = (0.0, 0.0, 0.0)
    else:
        base_motion = (
            desired_velocity_xy[0] + params.bridge_path_p_gain * path_error[0],
            desired_velocity_xy[1] + params.bridge_path_p_gain * path_error[1],
            0.0,
        )
    normal_projection = dot3(base_motion, n_control_b)
    motion_cmd: Vec3 = tuple(base_motion[idx] - normal_projection * n_control_b[idx] for idx in range(3))  # type: ignore[assignment]
    motion_norm = norm3(motion_cmd)
    if motion_norm > params.bridge_motion_limit_m_s:
        scale = params.bridge_motion_limit_m_s / motion_norm
        motion_cmd = tuple(value * scale for value in motion_cmd)  # type: ignore[assignment]

    controlled_force_n = normal_load if params.bridge_mode == "line" else force_abs
    force_error = params.target_force_n - controlled_force_n
    line_grace_valid = (
        params.bridge_mode == "line"
        and not flags["detached"]
        and flags["control"]
        and not state.normal_acquired
        and state.line_stage_s <= params.bridge_acquire_grace_s
    )
    control_allowed = sample.sensor_ok > 0.5 and (
        (flags["latch"] and state.normal_acquired)
        or ((flags["detach"] or flags["orient"] or flags["acquire"] or flags["line"]) and state.normal_acquired)
        or line_grace_valid
    )
    if params.bridge_integrate_stage25_only and params.bridge_mode == "line" and not flags["control"]:
        control_allowed = False

    cmd: Vec3 = (0.0, 0.0, 0.0)
    cmd_valid = 0.0
    if control_allowed:
        if params.bridge_mode == "line" and not state.normal_acquired:
            cmd = (0.0, 0.0, 0.0)
            orientation_cmd = (0.0, 0.0, 0.0)
        elif flags["latch"]:
            cmd = (0.0, 0.0, 0.0)
            orientation_cmd = (0.0, 0.0, 0.0)
        elif flags["orient"]:
            cmd = (0.0, 0.0, 0.0)
        elif flags["line_entry"]:
            state = replace(state, integral_error_n_s=0.0, normal_velocity_m_s=0.0)
            cmd = (0.0, 0.0, 0.0)
            orientation_cmd = (0.0, 0.0, 0.0)
        else:
            integral_error = clamp(
                state.integral_error_n_s + force_error * sample.dt_s,
                -params.bridge_integral_limit_n_s,
                params.bridge_integral_limit_n_s,
            )
            accel_like = (
                params.bridge_force_p_gain * force_error
                + params.bridge_force_i_gain * integral_error
                - params.bridge_force_damping * state.normal_velocity_m_s
            )
            normal_velocity = clamp(
                state.normal_velocity_m_s + accel_like * sample.dt_s,
                -params.bridge_normal_velocity_limit_m_s,
                params.bridge_normal_velocity_limit_m_s,
            )
            if (
                params.bridge_mode == "line"
                and state.normal_acquired
                and normal_load < params.bridge_min_force_for_control_n
                and force_error > 0.0
            ):
                normal_velocity = max(
                    normal_velocity,
                    min(params.bridge_reacquire_velocity_m_s, params.bridge_normal_velocity_limit_m_s),
                )
                hold_reason = "reacquire_low_load"
            state = replace(state, integral_error_n_s=integral_error, normal_velocity_m_s=normal_velocity)
            force_cmd = tuple(
                -params.bridge_normal_command_sign * n_control_b[idx] * normal_velocity
                for idx in range(3)
            )
            cmd = tuple(motion_cmd[idx] + force_cmd[idx] for idx in range(3))  # type: ignore[assignment]
            if flags["acquire"]:
                orientation_cmd = (0.0, 0.0, 0.0)
        cmd_norm = norm3(cmd)
        if cmd_norm > params.bridge_total_linear_limit_m_s:
            scale = params.bridge_total_linear_limit_m_s / cmd_norm
            cmd = tuple(value * scale for value in cmd)  # type: ignore[assignment]
        cmd_valid = 0.0 if params.bridge_mode == "preview" else 1.0
    else:
        state = replace(state, integral_error_n_s=0.0, normal_velocity_m_s=0.0)
        hold_reason = hold_reason or "control_not_allowed"

    controller_state = (
        33.0
        if flags["latch"]
        else 35.0
        if flags["detach"]
        else 31.0
        if flags["orient"]
        else 36.0
        if flags["line_entry"]
        else {"preview": 10.0, "hold": 20.0, "line": 30.0, "axis_iso": 34.0}.get(params.bridge_mode, 1.0)
    )
    approach_b = (-n_control_b[0], -n_control_b[1], -n_control_b[2])
    return (
        Step5bContactResult(
            cmd_valid=cmd_valid,
            cmd_vx_m_s=cmd[0],
            cmd_vy_m_s=cmd[1],
            cmd_vz_m_s=cmd[2],
            cmd_wx_rad_s=orientation_cmd[0],
            cmd_wy_rad_s=orientation_cmd[1],
            cmd_wz_rad_s=orientation_cmd[2],
            progress_m=progress,
            path_time_s=float(path_ref["path_time_s"]),
            force_error_n=force_error,
            orientation_error_rad=orientation_error,
            controller_state=controller_state if cmd_valid > 0.5 else 1.0,
            normal_load_n=normal_load,
            force_norm_n=force_abs,
            line_stage_s=state.line_stage_s,
            desired_x_m=float(desired_x),
            desired_y_m=float(desired_y),
            desired_vx_m_s=desired_velocity_xy[0],
            desired_vy_m_s=desired_velocity_xy[1],
            path_error_x_m=path_error[0],
            path_error_y_m=path_error[1],
            normal_filter_source=normal_filter_source,
            normal_follow_mode=params.bridge_normal_follow_mode,
            hold_reason=hold_reason,
            latched_normal_b=state.latched_normal_b,
            filtered_normal_b=state.filtered_normal_b,
            control_normal_b=n_control_b,
            reaction_normal_b=n_reaction_b,
            approach_normal_b=approach_b,
            live_normal_candidate_force_n=live_candidate_force_n,
            live_normal_candidate_angle_rad=live_candidate_angle_rad,
            live_normal_angle_from_latch_rad=live_candidate_angle_from_latch_rad,
        ),
        state,
    )
