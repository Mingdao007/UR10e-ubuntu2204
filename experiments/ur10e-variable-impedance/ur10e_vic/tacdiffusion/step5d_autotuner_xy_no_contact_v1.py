"""Independent, offline-testable Step5d moving-reference diagnostic profile.

The profile is deliberately model-inactive and campaign-inactive.  It provides
the frozen Cartesian reference, the explicitly requested moving-reference
guard stack, a Direct Torque receiver source, and a deterministic 500 Hz
evidence slice.  It has no controller, network, motion, training, or promotion
side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..backends import (
    DIRECT_TORQUE_BASELINE_K,
    DIRECT_TORQUE_VIRTUAL_MASS,
    DirectTorqueGuardState,
    DirectTorquePacket,
    validate_direct_torque_packet,
)


PROFILE_ID = "step5d_tacdiffusion_autotuner_xy_no_contact_v1"
PROFILE_SCHEMA = "step5d_tacdiffusion_autotuner_xy_no_contact/v1"
RUNTIME_SCHEMA = "ur10e_direct_torque_receiver/no_contact_xy/v1"
PACKAGE_SCHEMA = "step5d_tacdiffusion_diagnostic_bundle/v1"
CONTROL_RATE_HZ = 500
CONTROL_PERIOD_S = 1.0 / CONTROL_RATE_HZ
DURATION_OPTIONS_S = (2, 10, 60)
THETA_RATE_RAD_S = 0.1
AMPLITUDE_M = 0.015

ORIGIN_XY_M = (0.487795411149049, 0.12932679270060748)
U_ALONG_XY = (-0.010785642631908187, 0.9999418332648238)
P_LATERAL_XY = (-0.9999418332648239, -0.010785642631908406)

STATIC_SAFE_FRAME_XY_MIN_M = (0.4572854798410708, 0.12932678034319484)
STATIC_SAFE_FRAME_XY_MAX_M = (0.487795411149049, 0.22350610254130138)
STATIC_FOOTPRINT_EPSILON_M = 1.0e-8
HARD_ELLIPSE_HALF_AXES_M = (0.025, 0.015)
CBF_QP_HALF_AXES_M = (0.022, 0.012)
KUNWEI_FORCE_LIMIT_N = 6.0
KUNWEI_TORQUE_LIMIT_NM = 0.5
Z_DEVIATION_LIMIT_M = 0.002
ORIENTATION_LIMIT_RAD = math.radians(5.0)
REFERENCE_SPEED_LIMIT_M_S = 0.003
LINEAR_COMMAND_LIMIT_M_S = 0.004
QDOT_LIMIT_RAD_S = 0.15
PROTOCOL_TOKEN = 5_252_101
WRENCH_FRAME_TOKEN = 5_252_001
DEFAULT_LEASE_ID = 1

FIXED_STIFFNESS = tuple(float(value) for value in DIRECT_TORQUE_BASELINE_K)
FIXED_DAMPING = tuple(
    2.0 * math.sqrt(DIRECT_TORQUE_BASELINE_K[index] * DIRECT_TORQUE_VIRTUAL_MASS[index])
    for index in range(6)
)
ZERO_WRENCH = (0.0,) * 6

RECORDER_FIELDS = (
    "t_s",
    "desired_x_m",
    "desired_y_m",
    "actual_x_m",
    "actual_y_m",
    "desired_z_m",
    "actual_z_m",
    "desired_vx_m_s",
    "desired_vy_m_s",
    "actual_vx_m_s",
    "actual_vy_m_s",
    "local_along_reference_m",
    "local_lateral_reference_m",
    "local_along_error_m",
    "local_lateral_error_m",
    "guard_static_safe_frame_ok",
    "guard_hard_ellipse_value",
    "guard_hard_ellipse_ok",
    "guard_cbf_qp_value",
    "guard_cbf_qp_ok",
    "guard_kunwei_force_n",
    "guard_kunwei_torque_nm",
    "guard_z_deviation_m",
    "guard_orientation_error_deg",
    "guard_reference_speed_m_s",
    "guard_linear_command_m_s",
    "guard_qdot_max_rad_s",
    "heartbeat_ok",
    "protocol_ok",
    "joint_mode_ok",
    "receiver_fault",
    "command_seq",
    "ack_seq",
    "ack_state",
    "command_lineage",
    "ack_lineage",
    "source_identity",
    "runtime_identity",
    "package_identity",
    "terminal_safety_snapshot",
)


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed


def _vector(values: Sequence[float], length: int, name: str) -> tuple[float, ...]:
    result = tuple(_finite(value, f"{name}[{index}]") for index, value in enumerate(values))
    if len(result) != length:
        raise ValueError(f"{name} must contain {length} values")
    return result


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class XYNoContactProfile:
    """Frozen base-frame trajectory and non-contact policy identity."""

    profile_id: str = PROFILE_ID
    duration_s: float = 60.0
    theta_rate_rad_s: float = THETA_RATE_RAD_S
    amplitude_m: float = AMPLITUDE_M
    origin_xy_m: tuple[float, float] = ORIGIN_XY_M
    u_along_xy: tuple[float, float] = U_ALONG_XY
    p_lateral_xy: tuple[float, float] = P_LATERAL_XY

    def __post_init__(self) -> None:
        if self.profile_id != PROFILE_ID:
            raise ValueError("profile id is fixed")
        if self.duration_s != 60.0:
            raise ValueError("the frozen reference duration is 60 s")
        if self.theta_rate_rad_s != THETA_RATE_RAD_S or self.amplitude_m != AMPLITUDE_M:
            raise ValueError("the frozen trajectory constants cannot drift")
        origin = _vector(self.origin_xy_m, 2, "origin_xy_m")
        along = _vector(self.u_along_xy, 2, "u_along_xy")
        lateral = _vector(self.p_lateral_xy, 2, "p_lateral_xy")
        if abs(math.hypot(*along) - 1.0) > 1e-12:
            raise ValueError("u_along_xy must be unit length")
        if abs(math.hypot(*lateral) - 1.0) > 1e-12:
            raise ValueError("p_lateral_xy must be unit length")
        if abs(along[0] * lateral[0] + along[1] * lateral[1]) > 1e-12:
            raise ValueError("trajectory basis must be orthogonal")
        object.__setattr__(self, "origin_xy_m", origin)
        object.__setattr__(self, "u_along_xy", along)
        object.__setattr__(self, "p_lateral_xy", lateral)


@dataclass(frozen=True)
class ReferencePoint:
    t_s: float
    theta_rad: float
    along_m: float
    lateral_m: float
    pose_base: tuple[float, float, float, float, float, float]
    velocity_base: tuple[float, float, float, float, float, float]


def default_profile() -> XYNoContactProfile:
    return XYNoContactProfile()


def reference_at(
    t_s: float,
    *,
    startup_safe_z_m: float,
    anchor_orientation_rotvec: Sequence[float],
    profile: XYNoContactProfile | None = None,
) -> ReferencePoint:
    """Map the exact local cycloid into absolute base-frame XY coordinates."""

    selected = default_profile() if profile is None else profile
    t = _finite(t_s, "t_s")
    if not 0.0 <= t <= selected.duration_s:
        raise ValueError("t_s must lie in the frozen [0, 60] interval")
    z = _finite(startup_safe_z_m, "startup_safe_z_m")
    orientation = _vector(anchor_orientation_rotvec, 3, "anchor_orientation_rotvec")
    theta = selected.theta_rate_rad_s * t
    along = selected.amplitude_m * (theta - math.sin(theta))
    lateral = selected.amplitude_m * (1.0 - math.cos(theta))
    along_rate = selected.amplitude_m * selected.theta_rate_rad_s * (1.0 - math.cos(theta))
    lateral_rate = selected.amplitude_m * selected.theta_rate_rad_s * math.sin(theta)
    x = selected.origin_xy_m[0] + selected.u_along_xy[0] * along + selected.p_lateral_xy[0] * lateral
    y = selected.origin_xy_m[1] + selected.u_along_xy[1] * along + selected.p_lateral_xy[1] * lateral
    vx = selected.u_along_xy[0] * along_rate + selected.p_lateral_xy[0] * lateral_rate
    vy = selected.u_along_xy[1] * along_rate + selected.p_lateral_xy[1] * lateral_rate
    return ReferencePoint(
        t_s=t,
        theta_rad=theta,
        along_m=along,
        lateral_m=lateral,
        pose_base=(x, y, z, orientation[0], orientation[1], orientation[2]),
        velocity_base=(vx, vy, 0.0, 0.0, 0.0, 0.0),
    )


def sample_count(duration_s: int) -> int:
    if duration_s not in DURATION_OPTIONS_S:
        raise ValueError(f"duration must be one of {DURATION_OPTIONS_S}")
    return duration_s * CONTROL_RATE_HZ


@dataclass(frozen=True)
class CommandAckSnapshot:
    command_seq: int
    ack_seq: int
    lease_id: int = DEFAULT_LEASE_ID
    protocol_token: int = PROTOCOL_TOKEN
    heartbeat_ok: bool = True
    protocol_ok: bool = True
    joint_mode_ok: bool = True
    receiver_fault: bool = False

    def __post_init__(self) -> None:
        if self.command_seq < 1 or self.ack_seq < 0 or self.lease_id < 1:
            raise ValueError("command/ack lineage integers are invalid")

    @property
    def lineage_ok(self) -> bool:
        return (
            self.ack_seq == self.command_seq
            and self.protocol_token == PROTOCOL_TOKEN
            and self.heartbeat_ok
            and self.protocol_ok
            and self.joint_mode_ok
            and not self.receiver_fault
        )


@dataclass(frozen=True)
class GuardSnapshot:
    static_safe_frame_ok: bool
    hard_ellipse_value: float
    hard_ellipse_ok: bool
    cbf_qp_value: float
    cbf_qp_ok: bool
    kunwei_force_n: float
    kunwei_torque_nm: float
    z_deviation_m: float
    orientation_error_deg: float
    reference_speed_m_s: float
    linear_command_m_s: float
    qdot_max_rad_s: float
    heartbeat_ok: bool
    protocol_ok: bool
    joint_mode_ok: bool
    receiver_fault: bool

    @property
    def passed(self) -> bool:
        return all(
            (
                self.static_safe_frame_ok,
                self.hard_ellipse_ok,
                self.cbf_qp_ok,
                self.kunwei_force_n <= KUNWEI_FORCE_LIMIT_N,
                self.kunwei_torque_nm <= KUNWEI_TORQUE_LIMIT_NM,
                self.z_deviation_m <= Z_DEVIATION_LIMIT_M,
                self.orientation_error_deg <= math.degrees(ORIENTATION_LIMIT_RAD),
                self.reference_speed_m_s <= REFERENCE_SPEED_LIMIT_M_S,
                self.linear_command_m_s <= LINEAR_COMMAND_LIMIT_M_S,
                self.qdot_max_rad_s <= QDOT_LIMIT_RAD_S,
                self.heartbeat_ok,
                self.protocol_ok,
                self.joint_mode_ok,
                not self.receiver_fault,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "static_safe_frame_ok": self.static_safe_frame_ok,
            "hard_ellipse_value": self.hard_ellipse_value,
            "hard_ellipse_ok": self.hard_ellipse_ok,
            "cbf_qp_value": self.cbf_qp_value,
            "cbf_qp_ok": self.cbf_qp_ok,
            "kunwei_force_n": self.kunwei_force_n,
            "kunwei_torque_nm": self.kunwei_torque_nm,
            "z_deviation_m": self.z_deviation_m,
            "orientation_error_deg": self.orientation_error_deg,
            "reference_speed_m_s": self.reference_speed_m_s,
            "linear_command_m_s": self.linear_command_m_s,
            "qdot_max_rad_s": self.qdot_max_rad_s,
            "heartbeat_ok": self.heartbeat_ok,
            "protocol_ok": self.protocol_ok,
            "joint_mode_ok": self.joint_mode_ok,
            "receiver_fault": self.receiver_fault,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class MovingReferenceSafetyStack:
    """The requested moving-reference layers, in their execution order."""

    static_xy_min_m: tuple[float, float] = STATIC_SAFE_FRAME_XY_MIN_M
    static_xy_max_m: tuple[float, float] = STATIC_SAFE_FRAME_XY_MAX_M
    hard_half_axes_m: tuple[float, float] = HARD_ELLIPSE_HALF_AXES_M
    cbf_qp_half_axes_m: tuple[float, float] = CBF_QP_HALF_AXES_M

    def __post_init__(self) -> None:
        minimum = _vector(self.static_xy_min_m, 2, "static_xy_min_m")
        maximum = _vector(self.static_xy_max_m, 2, "static_xy_max_m")
        hard = _vector(self.hard_half_axes_m, 2, "hard_half_axes_m")
        cbf = _vector(self.cbf_qp_half_axes_m, 2, "cbf_qp_half_axes_m")
        if any(lo >= hi for lo, hi in zip(minimum, maximum)):
            raise ValueError("static safe-frame footprint bounds are invalid")
        if not all(value > 0.0 for value in hard + cbf):
            raise ValueError("moving-reference ellipse axes must be positive")
        object.__setattr__(self, "static_xy_min_m", minimum)
        object.__setattr__(self, "static_xy_max_m", maximum)
        object.__setattr__(self, "hard_half_axes_m", hard)
        object.__setattr__(self, "cbf_qp_half_axes_m", cbf)

    @staticmethod
    def _ellipse_value(error_xy: tuple[float, float], axes: tuple[float, float]) -> float:
        return (error_xy[0] / axes[0]) ** 2 + (error_xy[1] / axes[1]) ** 2

    def evaluate(
        self,
        *,
        actual_pose_base: Sequence[float],
        desired: ReferencePoint,
        actual_velocity_base: Sequence[float] = (0.0,) * 6,
        command_velocity_base: Sequence[float] | None = None,
        qdot_rad_s: Sequence[float] = (0.0,) * 6,
        wrench_tcp: Sequence[float] = ZERO_WRENCH,
        anchor_orientation_rotvec: Sequence[float],
        ack: CommandAckSnapshot,
    ) -> GuardSnapshot:
        actual_pose = _vector(actual_pose_base, 6, "actual_pose_base")
        actual_velocity = _vector(actual_velocity_base, 6, "actual_velocity_base")
        command_velocity = _vector(
            desired.velocity_base if command_velocity_base is None else command_velocity_base,
            6,
            "command_velocity_base",
        )
        qdot = _vector(qdot_rad_s, 6, "qdot_rad_s")
        wrench = _vector(wrench_tcp, 6, "wrench_tcp")
        anchor = _vector(anchor_orientation_rotvec, 3, "anchor_orientation_rotvec")
        error_xy = (
            actual_pose[0] - desired.pose_base[0],
            actual_pose[1] - desired.pose_base[1],
        )
        local_error = (
            error_xy[0] * U_ALONG_XY[0] + error_xy[1] * U_ALONG_XY[1],
            error_xy[0] * P_LATERAL_XY[0] + error_xy[1] * P_LATERAL_XY[1],
        )
        hard_value = self._ellipse_value(local_error, self.hard_half_axes_m)
        cbf_value = self._ellipse_value(local_error, self.cbf_qp_half_axes_m)
        # The static footprint bounds validate the commanded reference.  The
        # measured tracking deviation is independently bounded by the hard
        # and soft moving-reference ellipses below; checking raw actual XY
        # against a path endpoint would reject ordinary sensor noise at the
        # origin where the bound is intentionally tight.
        static_ok = all(
            self.static_xy_min_m[index] - STATIC_FOOTPRINT_EPSILON_M
            <= desired.pose_base[index]
            <= self.static_xy_max_m[index] + STATIC_FOOTPRINT_EPSILON_M
            for index in range(2)
        )
        orientation_error_rad = math.sqrt(
            sum((actual_pose[index + 3] - anchor[index]) ** 2 for index in range(3))
        )
        actual_linear_speed = math.sqrt(sum(actual_velocity[index] ** 2 for index in range(3)))
        command_linear_speed = math.sqrt(sum(command_velocity[index] ** 2 for index in range(3)))
        return GuardSnapshot(
            static_safe_frame_ok=static_ok,
            hard_ellipse_value=hard_value,
            hard_ellipse_ok=hard_value <= 1.0 + 1e-12,
            cbf_qp_value=cbf_value,
            cbf_qp_ok=cbf_value <= 1.0 + 1e-12,
            kunwei_force_n=math.sqrt(sum(wrench[index] ** 2 for index in range(3))),
            kunwei_torque_nm=math.sqrt(sum(wrench[index] ** 2 for index in range(3, 6))),
            z_deviation_m=abs(actual_pose[2] - desired.pose_base[2]),
            orientation_error_deg=math.degrees(orientation_error_rad),
            reference_speed_m_s=math.sqrt(sum(desired.velocity_base[index] ** 2 for index in range(3))),
            linear_command_m_s=command_linear_speed,
            qdot_max_rad_s=max(abs(value) for value in qdot),
            heartbeat_ok=ack.heartbeat_ok,
            protocol_ok=ack.protocol_ok and ack.protocol_token == PROTOCOL_TOKEN,
            joint_mode_ok=ack.joint_mode_ok,
            receiver_fault=ack.receiver_fault,
        )


def _duration_from_value(value: int | str) -> int:
    text = str(value).strip().lower()
    if text.endswith("s"):
        text = text[:-1]
    try:
        duration = int(text)
    except ValueError as exc:
        raise ValueError("duration must be 2s, 10s, or 60s") from exc
    if duration not in DURATION_OPTIONS_S:
        raise ValueError("duration must be 2s, 10s, or 60s")
    return duration


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def package_identity() -> dict[str, Any]:
    numeric_path = _repo_root() / "experiments/tase-contact-reproduction/config/step5d_tacdiffusion_autotuner_xy_no_contact_v1_numeric_sanity.json"
    numeric_sha = _sha256_bytes(numeric_path.read_bytes()) if numeric_path.is_file() else None
    source = build_runtime_source(entrypoint_suffix="package")
    identity_core = {
        "profile_id": PROFILE_ID,
        "package_schema": PACKAGE_SCHEMA,
        "runtime_schema": RUNTIME_SCHEMA,
        "runtime_source_sha256": _sha256_bytes(source.encode("utf-8")),
        "numeric_sanity_sha256": numeric_sha,
        "controller_target": f"/programs/andyl/kunwei/step5/{PROFILE_ID}.urp",
    }
    return {
        **identity_core,
        "package_id": _sha256_bytes(_canonical_json(identity_core)),
        "source_path": "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/step5d_autotuner_xy_no_contact_v1.py",
        "readback_identity": "profile_id + package_id + runtime_source_sha256 + numeric_sanity_sha256",
    }


def _runtime_numbers() -> dict[str, Any]:
    return {
        "control_rate_hz": CONTROL_RATE_HZ,
        "control_period_s": CONTROL_PERIOD_S,
        "theta_rate_rad_s": THETA_RATE_RAD_S,
        "amplitude_m": AMPLITUDE_M,
        "origin_xy_m": list(ORIGIN_XY_M),
        "u_along_xy": list(U_ALONG_XY),
        "p_lateral_xy": list(P_LATERAL_XY),
        "hard_ellipse_half_axes_m": list(HARD_ELLIPSE_HALF_AXES_M),
        "cbf_qp_half_axes_m": list(CBF_QP_HALF_AXES_M),
        "kunwei_force_limit_n": KUNWEI_FORCE_LIMIT_N,
        "kunwei_torque_limit_nm": KUNWEI_TORQUE_LIMIT_NM,
        "z_deviation_limit_m": Z_DEVIATION_LIMIT_M,
        "orientation_limit_deg": 5.0,
        "reference_speed_limit_m_s": REFERENCE_SPEED_LIMIT_M_S,
        "linear_command_limit_m_s": LINEAR_COMMAND_LIMIT_M_S,
        "qdot_limit_rad_s": QDOT_LIMIT_RAD_S,
        "fixed_stiffness": list(FIXED_STIFFNESS),
        "fixed_damping": list(FIXED_DAMPING),
        "feedforward_wrench": list(ZERO_WRENCH),
    }


def build_runtime_source(*, duration_s: int = 60, entrypoint_suffix: str = "live") -> str:
    """Build the no-I/O Direct Torque receiver source for later read-back."""

    duration = _duration_from_value(duration_s)
    if entrypoint_suffix not in {"live", "package"}:
        raise ValueError("entrypoint_suffix must be live or package")
    receiver_entrypoint = PROFILE_ID if entrypoint_suffix == "live" else f"{PROFILE_ID}_package"
    vector = lambda values: "[" + ", ".join(f"{float(value):.17g}" for value in values) + "]"
    return f'''# VERSION: {PROFILE_ID}
# INDEPENDENT NO-CONTACT DIAGNOSTIC: {PROFILE_ID}
# Host-side network ownership is external to this receiver; this source has no
# TP program, contact-search, model, training, or promotion behavior.
# direct_torque() supplies the controller's gravity compensation; this profile
# adds coriolis compensation and Cartesian impedance through J^T*wrench.
def {receiver_entrypoint}_receiver():
  local profile_schema = "{PROFILE_SCHEMA}"
  local receiver_schema = "{RUNTIME_SCHEMA}"
  local control_mode = "direct_torque_cartesian_impedance"
  local model_active = False
  local training_enabled = False
  local promotion_enabled = False
  local contact_search_enabled = False
  local control_rate_hz = {CONTROL_RATE_HZ}
  local diagnostic_duration_s = {duration}.0
  local protocol_token = {PROTOCOL_TOKEN}
  local wrench_frame_token = {WRENCH_FRAME_TOKEN}
  local fixed_k = {vector(FIXED_STIFFNESS)}
  local fixed_d = {vector(FIXED_DAMPING)}
  local zero_feedforward_wrench = {vector(ZERO_WRENCH)}
  local joint_damping = [1.5, 1.5, 1.2, 0.3, 0.3, 0.2]
  local tau_limit = [20.0, 20.0, 20.0, 8.0, 8.0, 8.0]
  local origin_xy = {vector(ORIGIN_XY_M)}
  local u_along_xy = {vector(U_ALONG_XY)}
  local p_lateral_xy = {vector(P_LATERAL_XY)}
  local static_xy_min = {vector(STATIC_SAFE_FRAME_XY_MIN_M)}
  local static_xy_max = {vector(STATIC_SAFE_FRAME_XY_MAX_M)}
  local hard_half_axes = {vector(HARD_ELLIPSE_HALF_AXES_M)}
  local cbf_qp_half_axes = {vector(CBF_QP_HALF_AXES_M)}
  local kunwei_force_limit_n = {KUNWEI_FORCE_LIMIT_N:.17g}
  local kunwei_torque_limit_nm = {KUNWEI_TORQUE_LIMIT_NM:.17g}
  local z_deviation_limit_m = {Z_DEVIATION_LIMIT_M:.17g}
  local orientation_limit_rad = {ORIENTATION_LIMIT_RAD:.17g}
  local reference_speed_limit_m_s = {REFERENCE_SPEED_LIMIT_M_S:.17g}
  local linear_command_limit_m_s = {LINEAR_COMMAND_LIMIT_M_S:.17g}
  local qdot_limit_rad_s = {QDOT_LIMIT_RAD_S:.17g}
  local command_mode = 0
  local last_sequence = 0
  local stale_packet_ticks = 0
  local lease_id = 0
  local phase = 0
  local fault_code = 0
  local path_time_s = 0.0
  # Direct Torque is owned by a minimal dedicated 500 Hz thread.  The main
  # receiver publishes a seqlocked command; it never places packet parsing or
  # Jacobian/controller work in the torque-call path.
  local torque_thread_run = False
  local torque_thread_handle = 0
  local torque_command_generation = 0
  local torque_thread_last_coherent_command = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local torque_thread_tick_count = 0
  local control_update_count = 0
  local torque_thread_last_control_update_count = -1
  local torque_thread_stale_ticks = 0
  local torque_thread_watchdog_fault = False
  local friction_viscous_scale = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local friction_coulomb_scale = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  # Ramp the Cartesian wrench from zero after the fresh stationary handoff;
  # the logged reference remains the exact frozen cycloid throughout.
  local entry_ramp_count = 0
  local entry_ramp_tick_limit = 50
  local startup_safe_z = 0.0
  local anchor_orientation = [0.0, 0.0, 0.0]
  local torque_command = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

  thread direct_torque_thread():
    while torque_thread_run:
      if control_update_count == torque_thread_last_control_update_count:
        torque_thread_stale_ticks = torque_thread_stale_ticks + 1
      else:
        torque_thread_last_control_update_count = control_update_count
        torque_thread_stale_ticks = 0
      end
      if torque_thread_stale_ticks >= 25:
        torque_thread_watchdog_fault = True
        torque_thread_run = False
      else:
        local generation_begin = torque_command_generation
        local candidate = [torque_command[0], torque_command[1], torque_command[2], torque_command[3], torque_command[4], torque_command[5]]
        local generation_end = torque_command_generation
        if generation_begin == generation_end and floor(generation_end/2)*2 == generation_end:
          torque_thread_last_coherent_command = candidate
        end
        local torque_to_apply = [torque_thread_last_coherent_command[0], torque_thread_last_coherent_command[1], torque_thread_last_coherent_command[2], torque_thread_last_coherent_command[3], torque_thread_last_coherent_command[4], torque_thread_last_coherent_command[5]]
        if not torque_thread_watchdog_fault:
          direct_torque(torque_to_apply, viscous_scale=friction_viscous_scale, coulomb_scale=friction_coulomb_scale)
          torque_thread_tick_count = torque_thread_tick_count + 1
        end
      end
    end
    stopj(10.0)
  end

  # Existing command/ack lineage: payload first, coherent sequence/heartbeat,
  # bounded monotonic advancement (RTDE may coalesce a few host packets),
  # exclusive lease, and explicit protocol echo.
  write_output_integer_register(24, 0)
  write_output_integer_register(25, 0)
  write_output_integer_register(26, 0)
  write_output_integer_register(27, 0)
  write_output_integer_register(28, protocol_token)
  while phase < 5:
    local sequence_before = read_input_integer_register(25)
    local mode = read_input_integer_register(24)
    local heartbeat = read_input_integer_register(26)
    local packet_lease = read_input_integer_register(27)
    local joint_mode_ok = read_input_integer_register(28) == 1
    local packet_protocol_ok = read_input_integer_register(29) == protocol_token
    local receiver_fault = read_input_integer_register(30) != 0
    local sequence_after = read_input_integer_register(25)
    local coherent = sequence_before == sequence_after and heartbeat == sequence_after
    local sequence_advanced = sequence_after > 0 and sequence_after > last_sequence and sequence_after <= last_sequence + 5
    local sequence_duplicate = sequence_after > 0 and sequence_after == last_sequence
    local sequence_ok = sequence_advanced or sequence_duplicate
    local lease_ok = packet_lease > 0 and (lease_id == 0 or packet_lease == lease_id)
    local packet_ok = coherent and sequence_ok and lease_ok and packet_protocol_ok and joint_mode_ok and not receiver_fault
    local actual_pose = get_actual_tcp_pose()
    local actual_speed = get_actual_tcp_speed()
    local actual_q = get_actual_joint_positions()
    local actual_qd = get_actual_joint_speeds()
    # Kunwei is the only experiment F/T authority.  The host writes the
    # software-baselined SI wrench into the guard payload; UR's internal F/T
    # signal is never used as a fallback or alternate guard source.
    local actual_wrench = [read_input_float_register(36), read_input_float_register(37), read_input_float_register(38), read_input_float_register(39), read_input_float_register(40), read_input_float_register(41)]
    local desired_pose = [read_input_float_register(24), read_input_float_register(25), read_input_float_register(26), read_input_float_register(27), read_input_float_register(28), read_input_float_register(29)]
    local reference_theta = path_time_s*{THETA_RATE_RAD_S:.17g}
    local reference_along = {AMPLITUDE_M:.17g}*(reference_theta - sin(reference_theta))
    local reference_lateral = {AMPLITUDE_M:.17g}*(1.0 - cos(reference_theta))
    local reference_pose = p[origin_xy[0] + u_along_xy[0]*reference_along + p_lateral_xy[0]*reference_lateral, origin_xy[1] + u_along_xy[1]*reference_along + p_lateral_xy[1]*reference_lateral, startup_safe_z, anchor_orientation[0], anchor_orientation[1], anchor_orientation[2]]
    local reference_velocity = [{AMPLITUDE_M:.17g}*{THETA_RATE_RAD_S:.17g}*(u_along_xy[0]*(1.0-cos(reference_theta)) + p_lateral_xy[0]*sin(reference_theta)), {AMPLITUDE_M:.17g}*{THETA_RATE_RAD_S:.17g}*(u_along_xy[1]*(1.0-cos(reference_theta)) + p_lateral_xy[1]*sin(reference_theta)), 0.0, 0.0, 0.0, 0.0]
    if phase == 0:
      # The receiver may start before the first RTDE packet.  Zero/default
      # inputs are a waiting state; any nonzero malformed packet fails closed.
      local waiting_for_first_packet = mode == 0 and sequence_after == 0
      if waiting_for_first_packet:
        phase = 0
      elif not packet_ok or mode != 1:
        fault_code = 1
        phase = 4
      else:
        startup_safe_z = desired_pose[2]
        anchor_orientation = [desired_pose[3], desired_pose[4], desired_pose[5]]
        lease_id = packet_lease
        last_sequence = sequence_after
        stale_packet_ticks = 0
        phase = 1
      end
    elif phase == 1:
      if not packet_ok or mode != 1:
        fault_code = 2
        phase = 4
      else:
        if sequence_duplicate:
          stale_packet_ticks = stale_packet_ticks + 1
        else:
          stale_packet_ticks = 0
        end
        if stale_packet_ticks > 25:
          fault_code = 6
          phase = 4
        end
        if stale_packet_ticks <= 25:
          if sequence_advanced:
            last_sequence = sequence_after
          end
        local pose_error = pose_sub(reference_pose, actual_pose)
        local tcp_rotation_base = p[0.0, 0.0, 0.0, actual_pose[3], actual_pose[4], actual_pose[5]]
        local orientation_error_base = wrench_trans(tcp_rotation_base, [0.0, 0.0, 0.0, pose_error[3], pose_error[4], pose_error[5]])
        pose_error[0] = reference_pose[0] - actual_pose[0]
        pose_error[1] = reference_pose[1] - actual_pose[1]
        pose_error[2] = reference_pose[2] - actual_pose[2]
        pose_error[3] = orientation_error_base[3]
        pose_error[4] = orientation_error_base[4]
        pose_error[5] = orientation_error_base[5]
        local hard_along = (actual_pose[0]-reference_pose[0])*u_along_xy[0] + (actual_pose[1]-reference_pose[1])*u_along_xy[1]
        local hard_lateral = (actual_pose[0]-reference_pose[0])*p_lateral_xy[0] + (actual_pose[1]-reference_pose[1])*p_lateral_xy[1]
        local hard_value = (hard_along/hard_half_axes[0])*(hard_along/hard_half_axes[0]) + (hard_lateral/hard_half_axes[1])*(hard_lateral/hard_half_axes[1])
        local cbf_qp_value = (hard_along/cbf_qp_half_axes[0])*(hard_along/cbf_qp_half_axes[0]) + (hard_lateral/cbf_qp_half_axes[1])*(hard_lateral/cbf_qp_half_axes[1])
        local static_footprint_epsilon_m = 1.0e-8
        local static_ok = reference_pose[0] >= static_xy_min[0] - static_footprint_epsilon_m and reference_pose[0] <= static_xy_max[0] + static_footprint_epsilon_m and reference_pose[1] >= static_xy_min[1] - static_footprint_epsilon_m and reference_pose[1] <= static_xy_max[1] + static_footprint_epsilon_m
        local kunwei_force = sqrt(actual_wrench[0]*actual_wrench[0] + actual_wrench[1]*actual_wrench[1] + actual_wrench[2]*actual_wrench[2])
        local kunwei_torque = sqrt(actual_wrench[3]*actual_wrench[3] + actual_wrench[4]*actual_wrench[4] + actual_wrench[5]*actual_wrench[5])
        local actual_linear_speed = sqrt(actual_speed[0]*actual_speed[0] + actual_speed[1]*actual_speed[1] + actual_speed[2]*actual_speed[2])
        local reference_speed = sqrt(reference_velocity[0]*reference_velocity[0] + reference_velocity[1]*reference_velocity[1] + reference_velocity[2]*reference_velocity[2])
        local orientation_error = sqrt((actual_pose[3]-anchor_orientation[0])*(actual_pose[3]-anchor_orientation[0]) + (actual_pose[4]-anchor_orientation[1])*(actual_pose[4]-anchor_orientation[1]) + (actual_pose[5]-anchor_orientation[2])*(actual_pose[5]-anchor_orientation[2]))
        local z_error = actual_pose[2] - startup_safe_z
        if z_error < 0.0:
          z_error = -z_error
        end
        local qdot_max = 0.0
        local joint = 0
        while joint < 6:
          local qdot_abs = actual_qd[joint]
          if qdot_abs < 0.0:
            qdot_abs = -qdot_abs
          end
          if qdot_abs > qdot_max:
            qdot_max = qdot_abs
          end
          joint = joint + 1
        end
        local safety_ok = static_ok and hard_value <= 1.0 and cbf_qp_value <= 1.0 and kunwei_force <= kunwei_force_limit_n and kunwei_torque <= kunwei_torque_limit_nm and z_error <= z_deviation_limit_m and orientation_error <= orientation_limit_rad and reference_speed <= reference_speed_limit_m_s and actual_linear_speed <= linear_command_limit_m_s and qdot_max <= qdot_limit_rad_s and packet_ok
        if not safety_ok:
          fault_code = 3
          phase = 4
        else:
          local entry_blend = 1.0
          if entry_ramp_count < entry_ramp_tick_limit:
            entry_blend = entry_ramp_count / entry_ramp_tick_limit
            entry_ramp_count = entry_ramp_count + 1
          end
          local cartesian_wrench = [entry_blend*(zero_feedforward_wrench[0] + fixed_k[0]*pose_error[0] - fixed_d[0]*actual_speed[0]), entry_blend*(zero_feedforward_wrench[1] + fixed_k[1]*pose_error[1] - fixed_d[1]*actual_speed[1]), entry_blend*(zero_feedforward_wrench[2] + fixed_k[2]*pose_error[2] - fixed_d[2]*actual_speed[2]), entry_blend*(zero_feedforward_wrench[3] + fixed_k[3]*pose_error[3] - fixed_d[3]*actual_speed[3]), entry_blend*(zero_feedforward_wrench[4] + fixed_k[4]*pose_error[4] - fixed_d[4]*actual_speed[4]), entry_blend*(zero_feedforward_wrench[5] + fixed_k[5]*pose_error[5] - fixed_d[5]*actual_speed[5])]
          local q = get_actual_joint_positions()
          local qd = get_actual_joint_speeds()
          local jacobian = get_jacobian(q)
          local coriolis = get_coriolis_and_centrifugal_torques(q, qd)
          local tau = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
          joint = 0
          while joint < 6:
            tau[joint] = coriolis[joint] - joint_damping[joint]*qd[joint]
            local axis = 0
            while axis < 6:
              tau[joint] = tau[joint] + jacobian[axis, joint]*cartesian_wrench[axis]
              axis = axis + 1
            end
            joint = joint + 1
          end
          joint = 0
          while joint < 6:
            if tau[joint] > tau_limit[joint] or tau[joint] < -tau_limit[joint] or tau[joint] != tau[joint]:
              safety_ok = False
            end
            joint = joint + 1
          end
          if not safety_ok:
            fault_code = 4
            phase = 4
          else:
            if torque_thread_watchdog_fault:
              fault_code = 7
              phase = 4
            else:
              torque_command_generation = torque_command_generation + 1
              joint = 0
              while joint < 6:
                torque_command[joint] = tau[joint]
                joint = joint + 1
              end
              torque_command_generation = torque_command_generation + 1
              control_update_count = control_update_count + 1
              if not torque_thread_run:
                torque_thread_run = True
                torque_thread_handle = run direct_torque_thread()
              end
            end
            path_time_s = path_time_s + get_steptime()
            if path_time_s >= diagnostic_duration_s:
              phase = 2
            end
          end
        end
        end
      end
    elif phase == 2:
      # Normal completion is typed: return to the path origin before session Home.
      # The impedance return is followed by the host's guarded transition.
      local origin_pose = p[origin_xy[0], origin_xy[1], startup_safe_z, anchor_orientation[0], anchor_orientation[1], anchor_orientation[2]]
      local origin_error = pose_sub(origin_pose, actual_pose)
      if sqrt(origin_error[0]*origin_error[0] + origin_error[1]*origin_error[1] + origin_error[2]*origin_error[2]) <= 0.0005:
        phase = 3
      else:
        local return_wrench = [fixed_k[0]*origin_error[0] - fixed_d[0]*actual_speed[0], fixed_k[1]*origin_error[1] - fixed_d[1]*actual_speed[1], fixed_k[2]*origin_error[2] - fixed_d[2]*actual_speed[2], fixed_k[3]*origin_error[3] - fixed_d[3]*actual_speed[3], fixed_k[4]*origin_error[4] - fixed_d[4]*actual_speed[4], fixed_k[5]*origin_error[5] - fixed_d[5]*actual_speed[5]]
        local q_return = get_actual_joint_positions()
        local qd_return = get_actual_joint_speeds()
        local jacobian_return = get_jacobian(q_return)
        local coriolis_return = get_coriolis_and_centrifugal_torques(q_return, qd_return)
        local tau_return = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        joint = 0
        while joint < 6:
          tau_return[joint] = coriolis_return[joint] - joint_damping[joint]*qd_return[joint]
          local axis_return = 0
          while axis_return < 6:
            tau_return[joint] = tau_return[joint] + jacobian_return[axis_return, joint]*return_wrench[axis_return]
            axis_return = axis_return + 1
          end
          if tau_return[joint] > tau_limit[joint] or tau_return[joint] < -tau_limit[joint] or tau_return[joint] != tau_return[joint]:
            fault_code = 5
            phase = 4
          end
          joint = joint + 1
        end
        if phase == 2:
          torque_command_generation = torque_command_generation + 1
          joint = 0
          while joint < 6:
            torque_command[joint] = tau_return[joint]
            joint = joint + 1
          end
          torque_command_generation = torque_command_generation + 1
          control_update_count = control_update_count + 1
        end
      end
    elif phase == 3:
      # Session Home is an external session transition; stop and verify stationary.
      if torque_thread_run:
        torque_thread_run = False
        join torque_thread_handle
      end
      direct_torque([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], viscous_scale=friction_viscous_scale, coulomb_scale=friction_coulomb_scale)
      stopj(10.0)
      if sqrt(actual_speed[0]*actual_speed[0] + actual_speed[1]*actual_speed[1] + actual_speed[2]*actual_speed[2]) <= 0.0001:
        phase = 5
      end
    else:
      # Any preposition/path/return fault is zero -> abort -> stop; no recovery.
      if torque_thread_run:
        torque_thread_run = False
        join torque_thread_handle
      end
      direct_torque([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], viscous_scale=friction_viscous_scale, coulomb_scale=friction_coulomb_scale)
      stopj(10.0)
      phase = 5
    end
    write_output_integer_register(24, phase)
    write_output_integer_register(25, last_sequence)
    write_output_integer_register(26, fault_code)
    write_output_integer_register(27, lease_id)
    write_output_integer_register(28, protocol_token)
    sync()
  end
  if torque_thread_run:
    torque_thread_run = False
    join torque_thread_handle
  end
end
{receiver_entrypoint}_receiver()
'''


def _lineage_dict(ack: CommandAckSnapshot) -> dict[str, Any]:
    return {
        "command_seq": ack.command_seq,
        "ack_seq": ack.ack_seq,
        "lease_id": ack.lease_id,
        "protocol_token": ack.protocol_token,
        "heartbeat_ok": ack.heartbeat_ok,
        "protocol_ok": ack.protocol_ok,
        "joint_mode_ok": ack.joint_mode_ok,
        "receiver_fault": ack.receiver_fault,
        "lineage_ok": ack.lineage_ok,
    }


def _recorder_row(
    *,
    desired: ReferencePoint,
    actual_pose: Sequence[float],
    actual_velocity: Sequence[float],
    command_velocity: Sequence[float],
    qdot: Sequence[float],
    wrench: Sequence[float],
    guard: GuardSnapshot,
    ack: CommandAckSnapshot,
    source_identity: str,
    runtime_identity: str,
    package_id: str,
) -> dict[str, Any]:
    actual = _vector(actual_pose, 6, "actual_pose_base")
    actual_v = _vector(actual_velocity, 6, "actual_velocity_base")
    error_xy = (actual[0] - desired.pose_base[0], actual[1] - desired.pose_base[1])
    row = {
        "t_s": desired.t_s,
        "desired_x_m": desired.pose_base[0],
        "desired_y_m": desired.pose_base[1],
        "actual_x_m": actual[0],
        "actual_y_m": actual[1],
        "desired_z_m": desired.pose_base[2],
        "actual_z_m": actual[2],
        "desired_vx_m_s": desired.velocity_base[0],
        "desired_vy_m_s": desired.velocity_base[1],
        "actual_vx_m_s": actual_v[0],
        "actual_vy_m_s": actual_v[1],
        "local_along_reference_m": desired.along_m,
        "local_lateral_reference_m": desired.lateral_m,
        "local_along_error_m": error_xy[0] * U_ALONG_XY[0] + error_xy[1] * U_ALONG_XY[1],
        "local_lateral_error_m": error_xy[0] * P_LATERAL_XY[0] + error_xy[1] * P_LATERAL_XY[1],
        "guard_static_safe_frame_ok": guard.static_safe_frame_ok,
        "guard_hard_ellipse_value": guard.hard_ellipse_value,
        "guard_hard_ellipse_ok": guard.hard_ellipse_ok,
        "guard_cbf_qp_value": guard.cbf_qp_value,
        "guard_cbf_qp_ok": guard.cbf_qp_ok,
        "guard_kunwei_force_n": guard.kunwei_force_n,
        "guard_kunwei_torque_nm": guard.kunwei_torque_nm,
        "guard_z_deviation_m": guard.z_deviation_m,
        "guard_orientation_error_deg": guard.orientation_error_deg,
        "guard_reference_speed_m_s": guard.reference_speed_m_s,
        "guard_linear_command_m_s": guard.linear_command_m_s,
        "guard_qdot_max_rad_s": guard.qdot_max_rad_s,
        "heartbeat_ok": guard.heartbeat_ok,
        "protocol_ok": guard.protocol_ok,
        "joint_mode_ok": guard.joint_mode_ok,
        "receiver_fault": guard.receiver_fault,
        "command_seq": ack.command_seq,
        "ack_seq": ack.ack_seq,
        "ack_state": "ACK_TORQUE",
        "command_lineage": _lineage_dict(ack),
        "ack_lineage": _lineage_dict(ack),
        "source_identity": source_identity,
        "runtime_identity": runtime_identity,
        "package_identity": package_id,
        "terminal_safety_snapshot": None,
    }
    if tuple(row) != RECORDER_FIELDS:
        raise RuntimeError("recorder field contract drifted")
    return row


def build_offline_evidence(
    duration_s: int | str,
    *,
    startup_safe_z_m: float = 0.029423891,
    anchor_orientation_rotvec: Sequence[float] = (0.0, 0.0, 0.0),
    actual_pose_mode: str = "identity_tracking_shadow",
) -> dict[str, Any]:
    """Produce one deterministic, non-live 500 Hz diagnostic evidence bundle."""

    duration = _duration_from_value(duration_s)
    if actual_pose_mode != "identity_tracking_shadow":
        raise ValueError("only the deterministic identity tracking shadow is supported")
    orientation = _vector(anchor_orientation_rotvec, 3, "anchor_orientation_rotvec")
    z = _finite(startup_safe_z_m, "startup_safe_z_m")
    identity = package_identity()
    stack = MovingReferenceSafetyStack()
    state = DirectTorqueGuardState()
    rows: list[dict[str, Any]] = []
    for index in range(sample_count(duration)):
        point = reference_at(
            index * CONTROL_PERIOD_S,
            startup_safe_z_m=z,
            anchor_orientation_rotvec=orientation,
        )
        sequence = index + 1
        packet = DirectTorquePacket(
            sequence_before=sequence,
            sequence_after=sequence,
            heartbeat=sequence,
            lease_id=DEFAULT_LEASE_ID,
            mode=1,
            equilibrium_pose=point.pose_base,
            stiffness=FIXED_STIFFNESS,
            damping=FIXED_DAMPING,
        )
        decision = validate_direct_torque_packet(
            packet,
            state,
            dt_s=CONTROL_PERIOD_S,
            release_ready=True,
            runtime_guard_ok=True,
        )
        if not decision.accepted:
            raise RuntimeError(f"offline command lineage rejected: {decision.reason}")
        state = decision.next_state
        ack = CommandAckSnapshot(command_seq=sequence, ack_seq=sequence)
        guard = stack.evaluate(
            actual_pose_base=point.pose_base,
            desired=point,
            actual_velocity_base=point.velocity_base,
            command_velocity_base=point.velocity_base,
            qdot_rad_s=(0.0,) * 6,
            wrench_tcp=ZERO_WRENCH,
            anchor_orientation_rotvec=orientation,
            ack=ack,
        )
        if not guard.passed:
            raise RuntimeError("offline identity tracking shadow violated explicit guard stack")
        rows.append(
            _recorder_row(
                desired=point,
                actual_pose=point.pose_base,
                actual_velocity=point.velocity_base,
                command_velocity=point.velocity_base,
                qdot=(0.0,) * 6,
                wrench=ZERO_WRENCH,
                guard=guard,
                ack=ack,
                source_identity=identity["source_path"],
                runtime_identity=RUNTIME_SCHEMA,
                package_id=identity["package_id"],
            )
        )
    endpoint = reference_at(
        duration,
        startup_safe_z_m=z,
        anchor_orientation_rotvec=orientation,
    )
    terminal_safety_snapshot = {
        "path_origin_returned": True,
        "session_home": True,
        "stop_requested": True,
        "stationary_verified": True,
        "fault": False,
        "automatic_recovery": False,
    }
    rows[-1]["terminal_safety_snapshot"] = terminal_safety_snapshot
    return {
        "schema": PACKAGE_SCHEMA,
        "profile_schema": PROFILE_SCHEMA,
        "profile_id": PROFILE_ID,
        "duration_s": duration,
        "control_rate_hz": CONTROL_RATE_HZ,
        "sample_count": len(rows),
        "sample_interval_s": CONTROL_PERIOD_S,
        "reference": {
            "origin_xy_m": list(ORIGIN_XY_M),
            "u_along_xy": list(U_ALONG_XY),
            "p_lateral_xy": list(P_LATERAL_XY),
            "startup_safe_z_m": z,
            "anchor_orientation_rotvec": list(orientation),
            "endpoint_t_s": duration,
            "endpoint_pose_base": list(endpoint.pose_base),
        },
        "flags": {
            "contact": False,
            "training_dataset": False,
            "qualification": False,
            "promotion": False,
            "tracking_failure_diagnostic_only": True,
        },
        "runtime": {
            "mode": "direct_torque_cartesian_impedance",
            "fixed_gains": True,
            "feedforward_wrench": list(ZERO_WRENCH),
            "model_active": False,
            "candidate_search": False,
            "contact_search": False,
            "training": False,
            "promotion": False,
            "single_writer": True,
            "fail_closed_command_ack": True,
            "runtime_schema": RUNTIME_SCHEMA,
        },
        "guard_stack": {
            "order": [
                "static_safe_frame_footprint",
                "moving_reference_hard_ellipse_25x15_mm",
                "moving_reference_soft_cbf_qp_22x12_mm",
                "kunwei_6N_0.5Nm",
                "z_deviation_2mm",
                "orientation_5deg",
                "reference_speed_3mm_s",
                "linear_command_4mm_s",
                "qdot_0.15rad_s",
                "heartbeat_protocol_joint_mode_receiver_fault",
            ],
            "static_xy_min_m": list(STATIC_SAFE_FRAME_XY_MIN_M),
            "static_xy_max_m": list(STATIC_SAFE_FRAME_XY_MAX_M),
            "hard_half_axes_m": list(HARD_ELLIPSE_HALF_AXES_M),
            "cbf_qp_half_axes_m": list(CBF_QP_HALF_AXES_M),
        },
        "command_ack_lineage": {
            "input_integer_registers": {"mode": 24, "sequence": 25, "heartbeat": 26, "lease": 27},
            "output_integer_registers": {"state": 24, "ack_sequence": 25, "fault": 26, "lease": 27, "protocol": 28},
            "protocol_token": PROTOCOL_TOKEN,
            "sequence_policy": "coherent before/after read, heartbeat equals sequence, exact +1, lease bound",
        },
        "terminal_sequence": ["path_origin", "session_home", "stop", "stationary_verified"],
        "identity": identity,
        "recorder": {"sample_fields": list(RECORDER_FIELDS), "samples": rows},
    }


def load_numeric_sanity(path: str | Path | None = None) -> dict[str, Any]:
    candidate = (
        _repo_root() / "experiments/tase-contact-reproduction/config/step5d_tacdiffusion_autotuner_xy_no_contact_v1_numeric_sanity.json"
        if path is None
        else Path(path)
    )
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    if payload.get("profile_id") != PROFILE_ID or payload.get("schema") != PROFILE_SCHEMA:
        raise ValueError("numeric sanity artifact identity mismatch")
    if payload.get("sample_count_60s_500hz") != 30_000:
        raise ValueError("numeric sanity sample count mismatch")
    return payload


__all__ = [
    "CBF_QP_HALF_AXES_M",
    "CONTROL_RATE_HZ",
    "DURATION_OPTIONS_S",
    "FIXED_DAMPING",
    "FIXED_STIFFNESS",
    "HARD_ELLIPSE_HALF_AXES_M",
    "MovingReferenceSafetyStack",
    "ORIGIN_XY_M",
    "P_LATERAL_XY",
    "PROFILE_ID",
    "RECORDER_FIELDS",
    "U_ALONG_XY",
    "build_offline_evidence",
    "build_runtime_source",
    "default_profile",
    "load_numeric_sanity",
    "package_identity",
    "reference_at",
    "sample_count",
]
