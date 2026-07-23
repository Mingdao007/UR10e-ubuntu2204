"""Shared helper primitives for Step5d remote-control engine."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping, Sequence
from dataclasses import dataclass


FloatSeq = Sequence[float]
Vector3 = tuple[float, float, float]
Vector6 = tuple[float, float, float, float, float, float]


def _as_float(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{path}: expected float")
    value_f = float(value)
    if not math.isfinite(value_f):
        raise ValueError(f"{path}: non-finite")
    return value_f


def _as_float_list(value: Any, n: int, path: str) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or len(value) != n:
        raise ValueError(f"{path}: expected len {n}")
    out = tuple(_as_float(v, f"{path}[{i}]") for i, v in enumerate(value))
    return out


def _norm(value: Sequence[float]) -> float:
    return math.sqrt(sum(float(v) * float(v) for v in value))


def tcp_point_kinematics(
    *,
    tool0_rotation: Sequence[Sequence[float]],
    tool0_translation: FloatSeq,
    tool0_jacobian: Sequence[Sequence[float]],
    positive_offset: FloatSeq,
) -> tuple[tuple[float, float, float], tuple[tuple[float, ...], ...]]:
    if len(tool0_rotation) != 3:
        raise ValueError("tool0_rotation: expected 3x3")
    if len(tool0_jacobian) != 6:
        raise ValueError("tool0_jacobian: expected 6x6")
    R = [_as_float_list(row, 3, "tool0_rotation") for row in tool0_rotation]
    t0 = _as_float_list(tool0_translation, 3, "tool0_translation")
    J = [_as_float_list(row, 6, "tool0_jacobian") for row in tool0_jacobian]
    off = _as_float_list(positive_offset, 3, "positive_offset")
    offset = (
        sum(R[0][k] * off[k] for k in range(3)),
        sum(R[1][k] * off[k] for k in range(3)),
        sum(R[2][k] * off[k] for k in range(3)),
    )
    tcp_position = (t0[0] + offset[0], t0[1] + offset[1], t0[2] + offset[2])

    jv = [row for row in J[:3]]
    jw = [row for row in J[3:]]
    skew = [
        (0.0, -offset[2], offset[1]),
        (offset[2], 0.0, -offset[0]),
        (-offset[1], offset[0], 0.0),
    ]
    jv_tcp: list[tuple[float, ...]] = []
    for i in range(3):
        row = []
        for j in range(6):
            correction = (
                skew[i][0] * jw[0][j]
                + skew[i][1] * jw[1][j]
                + skew[i][2] * jw[2][j]
            )
            row.append(jv[i][j] - correction)
        jv_tcp.append(tuple(float(v) for v in row))
    jacobian_tcp = tuple(jv_tcp + [tuple(jw[0]), tuple(jw[1]), tuple(jw[2])])
    return tuple(float(v) for v in tcp_position), jacobian_tcp


def cycloid_reference_velocity(cfg: Mapping[str, Any], elapsed_s: float) -> Vector6:
    if elapsed_s < 0.0:
        raise ValueError("elapsed_s must be non-negative")
    basis = cfg["path"]["basis"]
    amplitude = _as_float(cfg["path"]["amplitude_m"], "path.amplitude_m")
    omega = _as_float(cfg["path"]["omega_rad_s"], "path.omega_rad_s")
    phi = omega * float(elapsed_s)
    ux, uy = _as_float_list(basis["u_along_xy"], 2, "path.basis.u_along_xy")
    px, py = _as_float_list(basis["p_lateral_xy"], 2, "path.basis.p_lateral_xy")
    dx = amplitude * omega * (1.0 - math.cos(phi))
    dy = amplitude * math.sin(phi)
    v = (ux * dx + px * dy, uy * dx + py * dy, 0.0, 0.0, 0.0, 0.0)
    return tuple(float(x) for x in v)


def free_space_twist(cfg: Mapping[str, Any], elapsed_s: float) -> Vector6:
    if elapsed_s < 0.0:
        raise ValueError("elapsed_s must be non-negative")
    duration = _as_float(cfg["canary"]["free_space_s"], "canary.free_space_s")
    speed = _as_float(cfg["canary"]["free_space_linear_limit_m_s"], "canary.free_space_linear_limit_m_s")
    if duration <= 0.0:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    if elapsed_s >= duration:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    ux, uy = _as_float_list(cfg["path"]["basis"]["u_along_xy"], 2, "path.basis.u_along_xy")
    reaction = _as_float_list(cfg["frame"]["reaction_normal_b"], 3, "frame.reaction_normal_b")
    direction = (ux, uy, 0.0)
    normal_component = sum(direction[index] * reaction[index] for index in range(3))
    tangent = tuple(
        direction[index] - normal_component * reaction[index] for index in range(3)
    )
    tangent_norm = _norm(tangent)
    if tangent_norm <= 1e-12:
        raise ValueError("path basis is degenerate against reaction normal")
    tangent_unit = tuple(value / tangent_norm for value in tangent)
    signed = 1.0 if elapsed_s < duration * 0.5 else -1.0
    return (
        signed * speed * tangent_unit[0],
        signed * speed * tangent_unit[1],
        signed * speed * tangent_unit[2],
        0.0,
        0.0,
        0.0,
    )


@dataclass(frozen=True)
class GuardWrench:
    ok: bool
    raw_signed_fz_n: float
    abs_signed_fz_n: float
    force_norm_n: float
    torque_norm_nm: float
    reasons: tuple[str, ...]


def guard_wrench(cfg: Mapping[str, Any], wrench_tcp_n: FloatSeq) -> GuardWrench:
    wrench = _as_float_list(wrench_tcp_n, 6, "wrench_tcp_n")
    force = wrench[:3]
    torque = wrench[3:]
    raw_fz = float(force[2])
    reasons: list[str] = []
    hard = _as_float(cfg["force"]["hard_normal_force_n"], "force.hard_normal_force_n")
    force_max = _as_float(cfg["force"]["max_force_norm_n"], "force.max_force_norm_n")
    torque_max = _as_float(cfg["force"]["max_torque_norm_nm"], "force.max_torque_norm_nm")
    if abs(raw_fz) > hard:
        reasons.append("raw_fz_hard_limit")
    force_norm = _norm(force)
    if force_norm > force_max:
        reasons.append("force_norm_limit")
    torque_norm = _norm(torque)
    if torque_norm > torque_max:
        reasons.append("torque_norm_limit")
    return GuardWrench(
        ok=len(reasons) == 0,
        raw_signed_fz_n=raw_fz,
        abs_signed_fz_n=abs(raw_fz),
        force_norm_n=float(force_norm),
        torque_norm_nm=float(torque_norm),
        reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class CanaryStage:
    name: str
    duration_s: float
    intent: str


def build_canary_stage_plan(cfg: Mapping[str, Any]) -> tuple[CanaryStage, ...]:
    return (
        CanaryStage("zero", _as_float(cfg["canary"]["zero_s"], "canary.zero_s"), "ready"),
        CanaryStage("free_space", _as_float(cfg["canary"]["free_space_s"], "canary.free_space_s"), "search"),
        CanaryStage("guarded_contact", _as_float(cfg["canary"]["guarded_contact_s"], "canary.guarded_contact_s"), "track"),
    )


def build_canary_evidence(
    *, params_sha256: str, boot_id: str, created_at_epoch_s: float, stage_statuses: Mapping[str, str]
) -> dict[str, Any]:
    if not isinstance(params_sha256, str) or not params_sha256:
        raise ValueError("params_sha256 must be str")
    if not isinstance(boot_id, str) or not boot_id:
        raise ValueError("boot_id must be str")
    created_at = _as_float(created_at_epoch_s, "created_at_epoch_s")
    if created_at < 0.0:
        raise ValueError("created_at_epoch_s must be non-negative")
    allowed = {
        "zero",
        "free_space",
        "guarded_contact",
        "post_canary_safe_pose",
    }
    if set(stage_statuses.keys()) != allowed:
        raise ValueError(
            "stages must be exactly "
            "zero/free_space/guarded_contact/post_canary_safe_pose"
        )
    status = {}
    for key in allowed:
        value = stage_statuses[key]
        if value != "passed":
            raise ValueError(f"stage {key} not passed")
        status[key] = "passed"
    return {
        "ok": True,
        "params_sha256": params_sha256,
        "boot_id": boot_id,
        "created_at_epoch_s": created_at,
        "stages": status,
    }


DASHBOARD_COMMANDS = ("is in remote control", "safetymode", "robotmode", "running")


def build_driver_command(cfg: Mapping[str, Any], *, reverse: bool = True) -> tuple[str, ...]:
    calibration_path = Path(str(cfg["kinematics"]["calibration_yaml"]))
    if not calibration_path.is_absolute():
        calibration_path = (Path(__file__).resolve().parents[4] / calibration_path).resolve()
    parts: list[str] = [
        "ros2",
        "launch",
        "ur_robot_driver",
        "ur_control.launch.py",
        "ur_type:=ur10e",
        f"robot_ip:={cfg['robot']['ip']}",
        f"reverse_ip:={cfg['robot']['reverse_ip']}",
        "headless_mode:=true",
        "activate_joint_controller:=false",
        "launch_rviz:=false",
        f"kinematics_params_file:={calibration_path}",
    ]
    if not reverse:
        parts = [part for part in parts if not part.startswith("reverse_ip:=")]
    return tuple(parts)


def build_spawner_command(cfg: Mapping[str, Any], temp_yaml: str | Path) -> tuple[str, ...]:
    temp_yaml_text = str(temp_yaml)
    return (
        "ros2",
        "run",
        "controller_manager",
        "spawner",
        "step5d_watchdog_controller",
        "-c",
        str(cfg["robot"]["controller_manager"]),
        "-p",
        temp_yaml_text,
        "-t",
        "ur10e_step5d_remote_watchdog/WatchdogController",
        "--inactive",
    )
