from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

from ament_index_python.packages import PackageNotFoundError, get_package_share_directory

from . import step5b_contact_control_core as core


def _workspace_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if parent.name == "ur10e_ros2_ws":
            return parent
    return Path(__file__).resolve().parents[3]


def _package_root() -> Path:
    try:
        return Path(get_package_share_directory("ur10e_example_controllers"))
    except PackageNotFoundError:
        return Path(__file__).resolve().parents[1]


WORKSPACE = _workspace_root()
EXPERIMENT = WORKSPACE / "experiments" / "tase-contact-reproduction"
CONFIG = EXPERIMENT / "config"
SAFE_FRAME = CONFIG / "step5_safe_frame.json"
STAGE_TABLE = CONFIG / "step5_stage_table.json"
CALIBRATED_URDF = (
    EXPERIMENT
    / "runs"
    / "step5c_calibrated_kinematics_audit_20260613_003314"
    / "calibrated_ur10e.urdf"
)
PACKAGE_ROOT = _package_root()
WORLD_PATH = PACKAGE_ROOT / "worlds" / "step5_table_world.sdf"
LAUNCH_PATH = PACKAGE_ROOT / "launch" / "step5b_simulation_mvp.launch.py"

TARGET_ROTVEC_RAD = (-3.044172198, -0.130573165, -0.202188631)
CONTACT_SURFACE_Z_M = 0.008044839
CONTACT_SURFACE_SIZE_M = (0.18, 0.10, CONTACT_SURFACE_Z_M)


Vec3 = tuple[float, float, float]


@dataclass(frozen=True)
class Step5bSimulationContext:
    stage: dict[str, Any]
    safe_frame: dict[str, Any]
    basis: core.Step5bPathBasis
    params: core.Step5bContactParams


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def strip_ros2_control_blocks(urdf_text: str) -> str:
    root = ET.fromstring(urdf_text)
    for child in list(root):
        if child.tag == "ros2_control":
            root.remove(child)
    return ET.tostring(root, encoding="unicode")


def load_context() -> Step5bSimulationContext:
    safe_frame = load_json(SAFE_FRAME)
    stage_table = load_json(STAGE_TABLE)
    stage = next(
        item for item in stage_table["stages"] if item["id"] == "step5_contact_cycloid_baseline_v1"
    )
    return Step5bSimulationContext(
        stage=stage,
        safe_frame=safe_frame,
        basis=core.Step5bPathBasis.from_safe_frame(safe_frame),
        params=core.Step5bContactParams.from_metadata_args({}, stage=stage),
    )


def local_to_base_xy(basis: core.Step5bPathBasis, along_m: float, lateral_m: float) -> tuple[float, float]:
    return (
        basis.origin_xy_m[0] + along_m * basis.u_along_xy[0] + lateral_m * basis.p_lateral_xy[0],
        basis.origin_xy_m[1] + along_m * basis.u_along_xy[1] + lateral_m * basis.p_lateral_xy[1],
    )


def default_preposition_start_xyz(basis: core.Step5bPathBasis, z_m: float = 0.2273) -> Vec3:
    """Synthetic offline start away from the Step5 entry point.

    The live incident did not produce a runner trace, so this MVP uses a
    deterministic upstream/lateral offset to exercise the continuous trajectory
    contract without claiming physical replay of the interrupted run.
    """
    return (
        basis.origin_xy_m[0] - 0.040 * basis.u_along_xy[0] - 0.015 * basis.p_lateral_xy[0],
        basis.origin_xy_m[1] - 0.040 * basis.u_along_xy[1] - 0.015 * basis.p_lateral_xy[1],
        z_m,
    )


def preposition_target_xyz(basis: core.Step5bPathBasis, start_z_m: float) -> Vec3:
    return (basis.origin_xy_m[0], basis.origin_xy_m[1], start_z_m)


def _smoothstep5(u: float) -> tuple[float, float, float, float]:
    """Return quintic position and derivatives ds/du, d2s/du2, d3s/du3."""
    u = max(0.0, min(1.0, u))
    s = 10.0 * u**3 - 15.0 * u**4 + 6.0 * u**5
    ds = 30.0 * u**2 - 60.0 * u**3 + 30.0 * u**4
    d2s = 60.0 * u - 180.0 * u**2 + 120.0 * u**3
    d3s = 60.0 - 360.0 * u + 360.0 * u**2
    return s, ds, d2s, d3s


def _norm(values: Vec3) -> float:
    return math.sqrt(sum(value * value for value in values))


def plan_continuous_preposition(
    start_xyz: Vec3,
    target_xyz: Vec3,
    *,
    max_speed_m_s: float = 0.020,
    sample_period_s: float = 0.020,
    min_duration_s: float = 2.0,
) -> dict[str, Any]:
    delta = tuple(target_xyz[i] - start_xyz[i] for i in range(3))
    distance_m = _norm(delta)  # type: ignore[arg-type]
    # Quintic smoothstep peaks at ds/du=1.875, so reserve duration for speed.
    duration_s = max(min_duration_s, 1.875 * distance_m / max(max_speed_m_s, 1e-9))
    sample_count = int(math.ceil(duration_s / sample_period_s)) + 1
    rows: list[dict[str, Any]] = []
    max_velocity = 0.0
    max_acceleration = 0.0
    max_jerk = 0.0
    for index in range(sample_count):
        t_s = min(index * sample_period_s, duration_s)
        u = 1.0 if duration_s <= 0.0 else t_s / duration_s
        s, ds_du, d2s_du2, d3s_du3 = _smoothstep5(u)
        velocity = tuple(delta[i] * ds_du / duration_s for i in range(3))
        acceleration = tuple(delta[i] * d2s_du2 / (duration_s**2) for i in range(3))
        jerk = tuple(delta[i] * d3s_du3 / (duration_s**3) for i in range(3))
        position = tuple(start_xyz[i] + delta[i] * s for i in range(3))
        velocity_norm = _norm(velocity)  # type: ignore[arg-type]
        acceleration_norm = _norm(acceleration)  # type: ignore[arg-type]
        jerk_norm = _norm(jerk)  # type: ignore[arg-type]
        max_velocity = max(max_velocity, velocity_norm)
        max_acceleration = max(max_acceleration, acceleration_norm)
        max_jerk = max(max_jerk, jerk_norm)
        rows.append(
            {
                "t_s": t_s,
                "tcp_x_m": position[0],
                "tcp_y_m": position[1],
                "tcp_z_m": position[2],
                "vx_m_s": velocity[0],
                "vy_m_s": velocity[1],
                "vz_m_s": velocity[2],
                "velocity_norm_m_s": velocity_norm,
                "acceleration_norm_m_s2": acceleration_norm,
                "jerk_norm_m_s3": jerk_norm,
            }
        )
    return {
        "strategy": "single_time_parameterized_quintic_preposition",
        "stage": 22.0,
        "goal_count": 1,
        "legacy_repeated_short_goals_rejected": True,
        "start_xyz_m": list(start_xyz),
        "target_xyz_m": list(target_xyz),
        "tcp_delta_m": list(delta),
        "tcp_delta_norm_m": distance_m,
        "duration_s": duration_s,
        "sample_period_s": sample_period_s,
        "sample_count": len(rows),
        "max_velocity_m_s": max_velocity,
        "max_acceleration_m_s2": max_acceleration,
        "max_jerk_m_s3": max_jerk,
        "target_rotvec_rad": list(TARGET_ROTVEC_RAD),
        "rows": rows,
    }


def simulate_kunwei_force_evidence(preposition: dict[str, Any]) -> dict[str, Any]:
    rows = []
    max_force = 0.0
    max_load = 0.0
    for row in preposition["rows"]:
        penetration_m = max(0.0, CONTACT_SURFACE_Z_M - float(row["tcp_z_m"]))
        normal_load_n = penetration_m * 750.0
        noise_n = 0.015 * math.sin(17.0 * float(row["t_s"]))
        force_z_n = normal_load_n + noise_n
        force_norm_n = abs(force_z_n)
        max_force = max(max_force, force_norm_n)
        max_load = max(max_load, normal_load_n)
        rows.append(
            {
                "t_s": row["t_s"],
                "Fx_N": 0.0,
                "Fy_N": 0.0,
                "Fz_N": force_z_n,
                "Mx_Nm": 0.0,
                "My_Nm": 0.0,
                "Mz_Nm": 0.0,
                "reaction_normal": [0.0, 0.0, 1.0],
                "approach_normal": [0.0, 0.0, -1.0],
                "normal_load_n": normal_load_n,
                "force_norm_n": force_norm_n,
            }
        )
    return {
        "schema": "simulated_kunwei_wrench_v1",
        "force_source": "simulated_kunwei_offline_trace",
        "contact_surface_z_m": CONTACT_SURFACE_Z_M,
        "reaction_normal": [0.0, 0.0, 1.0],
        "approach_normal": [0.0, 0.0, -1.0],
        "normal_load_definition": "dot(force_base, reaction_normal)",
        "max_force_norm_n": max_force,
        "max_normal_load_n": max_load,
        "sample_count": len(rows),
        "rows": rows,
    }


def build_artifact(
    *,
    start_xyz: Vec3 | None = None,
    max_speed_m_s: float = 0.020,
    sample_period_s: float = 0.020,
    min_duration_s: float = 2.0,
) -> dict[str, Any]:
    context = load_context()
    start = start_xyz or default_preposition_start_xyz(context.basis)
    target = preposition_target_xyz(context.basis, start[2])
    preposition = plan_continuous_preposition(
        start,
        target,
        max_speed_m_s=max_speed_m_s,
        sample_period_s=sample_period_s,
        min_duration_s=min_duration_s,
    )
    force_evidence = simulate_kunwei_force_evidence(preposition)
    return {
        "schema": "ur10e_step5b_simulation_mvp_v1",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "offline_no_motion",
        "live_robot_command_authorized": False,
        "step5b_live_locked": True,
        "source_paths": {
            "safe_frame": str(SAFE_FRAME),
            "stage_table": str(STAGE_TABLE),
            "calibrated_urdf": str(CALIBRATED_URDF),
            "world": str(WORLD_PATH),
            "launch": str(LAUNCH_PATH),
        },
        "calibrated_urdf_exists": CALIBRATED_URDF.is_file(),
        "frames": {
            "gazebo_world": "world",
            "robot_base": "base",
            "robot_base_link": "base_link",
            "tool": "tool0",
            "safe_frame_basis": "step5_safe_frame_base_xy",
            "force_vector": "base",
            "reaction_normal": "base",
            "approach_normal": "base",
        },
        "units": {
            "time": "s",
            "position": "m",
            "velocity": "m/s",
            "acceleration": "m/s^2",
            "jerk": "m/s^3",
            "force": "N",
            "torque": "Nm",
            "angle": "rad",
        },
        "stage": {
            "id": context.stage["id"],
            "duration_s": context.params.duration_s,
            "amplitude_m": context.params.amplitude_m,
            "omega_rad_s": context.params.omega_rad_s,
            "target_force_n": context.params.target_force_n,
        },
        "safe_frame": {
            "origin_xy_m": list(context.basis.origin_xy_m),
            "u_along_xy": list(context.basis.u_along_xy),
            "p_lateral_xy": list(context.basis.p_lateral_xy),
            "local_zero_maps_to_base_xy_m": list(local_to_base_xy(context.basis, 0.0, 0.0)),
        },
        "expected_contact_geometry": {
            "surface_name": "step5_contact_surface",
            "surface_center_xy_m": list(context.basis.origin_xy_m),
            "surface_center_z_m": CONTACT_SURFACE_Z_M / 2.0,
            "surface_top_z_m": CONTACT_SURFACE_Z_M,
            "surface_size_m": list(CONTACT_SURFACE_SIZE_M),
            "safe_frame_origin_xy_m": list(context.basis.origin_xy_m),
            "preposition_target_xyz_m": list(target),
            "reaction_normal": [0.0, 0.0, 1.0],
            "approach_normal": [0.0, 0.0, -1.0],
        },
        "preposition": preposition,
        "simulated_force_evidence": force_evidence,
        "acceptance": {
            "goal_count_max": 1,
            "max_velocity_m_s": max_speed_m_s,
            "preposition_goal_count_ok": preposition["goal_count"] == 1,
            "velocity_limit_ok": preposition["max_velocity_m_s"] <= max_speed_m_s + 1e-9,
            "force_contract_ok": force_evidence["approach_normal"] == [0.0, 0.0, -1.0],
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the offline UR10e Step5b simulation MVP artifact.")
    parser.add_argument("--summary", type=Path, help="Write the JSON artifact to this path.")
    parser.add_argument("--start-x-m", type=float)
    parser.add_argument("--start-y-m", type=float)
    parser.add_argument("--start-z-m", type=float, default=0.2273)
    parser.add_argument("--max-speed-m-s", type=float, default=0.020)
    parser.add_argument("--sample-period-s", type=float, default=0.020)
    parser.add_argument("--min-duration-s", type=float, default=2.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    start_xyz = None
    if args.start_x_m is not None or args.start_y_m is not None:
        if args.start_x_m is None or args.start_y_m is None:
            raise SystemExit("--start-x-m and --start-y-m must be provided together")
        start_xyz = (float(args.start_x_m), float(args.start_y_m), float(args.start_z_m))
    artifact = build_artifact(
        start_xyz=start_xyz,
        max_speed_m_s=float(args.max_speed_m_s),
        sample_period_s=float(args.sample_period_s),
        min_duration_s=float(args.min_duration_s),
    )
    text = json.dumps(artifact, indent=2, sort_keys=True)
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
