from __future__ import annotations

import argparse
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
from typing import Any

from ament_index_python.packages import PackageNotFoundError, get_package_share_directory

from . import step5b_simulation_mvp as step5b_mvp
from .step5b_simulation_mvp import strip_ros2_control_blocks


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
STEP5_SAFE_FRAME = CONFIG / "step5_safe_frame.json"
STEP5_STAGE_TABLE = CONFIG / "step5_stage_table.json"
STEP5A_SPEC = CONFIG / "step5a_local_control_spec.json"
STEP5B_AUTHORIZATION = CONFIG / "step5b_authorization_state.json"
STEP6_SAFE_FRAME = CONFIG / "step6_eight_safe_frame.json"
STEP6_STAGE_TABLE = CONFIG / "step6_stage_table.json"
TEXTBOOK_SPEC = CONFIG / "local_control_textbook_spec.json"
CALIBRATED_URDF = step5b_mvp.CALIBRATED_URDF
PACKAGE_ROOT = _package_root()
WORLD_PATH = PACKAGE_ROOT / "worlds" / "step5_table_world.sdf"
LAUNCH_PATH = PACKAGE_ROOT / "launch" / "step5b_simulation_mvp.launch.py"


Vec2 = tuple[float, float]


@dataclass(frozen=True)
class StageSpec:
    stage_id: str
    label: str
    source_stage_id: str
    mode: str
    live_robot_command_authorized: bool
    safe_frame_path: Path
    stage_table_path: Path
    shape: str
    contact: bool
    runner_status: str
    artifact_status: str
    gazebo_status: str
    known_blocker: str


STAGE_REGISTRY: "OrderedDict[str, StageSpec]" = OrderedDict(
    (
        (
            "step5a",
            StageSpec(
                "step5a",
                "Step5a no-contact cycloid",
                "step5a_cycloid_no_contact_v3",
                "offline_no_motion",
                False,
                STEP5_SAFE_FRAME,
                STEP5_STAGE_TABLE,
                "cycloid",
                False,
                "local_control_textbook_offline_shadow",
                "full_matrix_artifact",
                "shared_world_marker",
                "No live ROS2 air-motion authorization; 0.009 m/s is TP v3 command-vector clamp provenance only.",
            ),
        ),
        (
            "step5b",
            StageSpec(
                "step5b",
                "Step5b contact cycloid",
                "step5_contact_cycloid_baseline_v1",
                "offline_no_motion",
                False,
                STEP5_SAFE_FRAME,
                STEP5_STAGE_TABLE,
                "cycloid",
                True,
                "step5b_mvp_offline_shadow_live_locked",
                "full_matrix_artifact",
                "shared_world_contact_surface",
                "Step5b live runner remains locked after 2026-06-18 table vibration; no live retry.",
            ),
        ),
        (
            "step5c",
            StageSpec(
                "step5c",
                "Step5c quarantined joint/RNN lineage",
                "step5c_joint_rnn_cycloid_v1",
                "offline_no_motion",
                False,
                STEP5_SAFE_FRAME,
                STEP5_STAGE_TABLE,
                "cycloid",
                False,
                "quarantined_offline_only",
                "schematic_artifact",
                "shared_world_marker_only",
                "2026-06-13 live dry-run moved in the wrong XY/Z direction; DLS/MuJoCo Jacobian mapping is not trusted.",
            ),
        ),
        (
            "step5d",
            StageSpec(
                "step5d",
                "Step5d strict RNN live-prep retained evidence",
                "step5d_strict_rnn_liveprep_v15a",
                "offline_no_motion",
                False,
                STEP5_SAFE_FRAME,
                STEP5_STAGE_TABLE,
                "cycloid",
                True,
                "retained_evidence_offline_shadow_only",
                "schematic_artifact",
                "shared_world_contact_surface",
                "v15a stopped by step5d_contact_safety:hold_duty_limit after about 0.998 s of Stage25.",
            ),
        ),
        (
            "step6a",
            StageSpec(
                "step6a",
                "Step6a no-contact eight",
                "step6a_eight_no_contact_v1",
                "offline_no_motion",
                False,
                STEP6_SAFE_FRAME,
                STEP6_STAGE_TABLE,
                "eight",
                False,
                "local_control_textbook_offline_shadow",
                "full_matrix_artifact",
                "shared_world_marker",
                "Retained no-contact rehearsal only; no live ROS2 air-motion authorization.",
            ),
        ),
        (
            "step6b",
            StageSpec(
                "step6b",
                "Step6b contact eight",
                "step6_contact_eight_baseline_v2",
                "offline_no_motion",
                False,
                STEP6_SAFE_FRAME,
                STEP6_STAGE_TABLE,
                "eight",
                True,
                "contact_baseline_offline_shadow",
                "full_matrix_artifact",
                "shared_world_contact_surface",
                "Contact path is represented as offline simulated force evidence only; no live bridge/TP fallback.",
            ),
        ),
    )
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _stage_from_table(table_path: Path, source_stage_id: str) -> dict[str, Any]:
    table = load_json(table_path)
    for stage in table["stages"]:
        if stage.get("id") == source_stage_id:
            return stage
    raise KeyError(source_stage_id)


def _basis(frame: dict[str, Any]) -> dict[str, Any]:
    return frame["basis"]


def _rotation_map(frame: dict[str, Any], local_xy_m: Vec2) -> Vec2:
    basis = _basis(frame)
    origin = basis["origin_xy_m"]
    u_along = basis["u_along_xy"]
    p_lateral = basis["p_lateral_xy"]
    return (
        float(origin[0]) + local_xy_m[0] * float(u_along[0]) + local_xy_m[1] * float(p_lateral[0]),
        float(origin[1]) + local_xy_m[0] * float(u_along[1]) + local_xy_m[1] * float(p_lateral[1]),
    )


def _step5a_affine_map(spec: dict[str, Any], local_xy_m: Vec2) -> Vec2:
    affine = spec["tp_v3_task_space"]["frame_map"]["affine_map"]
    return (
        float(affine["origin_x_m"])
        + float(affine["m_xx"]) * local_xy_m[0]
        + float(affine["m_xy"]) * local_xy_m[1],
        float(affine["origin_y_m"])
        + float(affine["m_yx"]) * local_xy_m[0]
        + float(affine["m_yy"]) * local_xy_m[1],
    )


def _stage_common(spec: StageSpec, stage: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "ur10e_step56_stage_artifact_v1",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "stage_id": spec.stage_id,
        "stage_label": spec.label,
        "mode": spec.mode,
        "live_robot_command_authorized": spec.live_robot_command_authorized,
        "contact_motion_authorized": False,
        "zero_ftsensor_authorized": False,
        "payload_tcp_safety_writes_authorized": False,
        "runner_status": spec.runner_status,
        "artifact_status": spec.artifact_status,
        "gazebo_status": spec.gazebo_status,
        "known_blocker": spec.known_blocker,
        "source_paths": {
            "textbook_spec": str(TEXTBOOK_SPEC),
            "safe_frame": str(spec.safe_frame_path),
            "stage_table": str(spec.stage_table_path),
            "calibrated_urdf": str(CALIBRATED_URDF),
            "world": str(WORLD_PATH),
            "launch": str(LAUNCH_PATH),
        },
        "frames": {
            "gazebo_world": "world",
            "robot_base": "base",
            "robot_base_link": "base_link",
            "tool": "tool0",
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
            "source_stage_id": spec.source_stage_id,
            "stage_table_stage": stage.get("stage"),
            "owner": stage.get("owner"),
            "shape": spec.shape,
            "contact": spec.contact,
            "bridge": bool(stage.get("bridge")),
            "duration_s": stage.get("duration_s"),
            "fixed_base_z_m": stage.get("fixed_base_z_m"),
        },
    }


def _sample_cycloid(stage: dict[str, Any], *, samples: int = 121) -> list[tuple[float, Vec2, Vec2]]:
    duration_s = float(stage["duration_s"])
    amplitude_m = float(stage.get("amplitude_m", 0.015))
    phase = stage.get("phase_law", {})
    omega_rad_s = float(phase.get("omega_rad_s", 0.1))
    rows: list[tuple[float, Vec2, Vec2]] = []
    for index in range(samples):
        t_s = duration_s * index / (samples - 1)
        theta = omega_rad_s * t_s
        local = (amplitude_m * (theta - math.sin(theta)), amplitude_m * (1.0 - math.cos(theta)))
        vel = (amplitude_m * omega_rad_s * (1.0 - math.cos(theta)), amplitude_m * omega_rad_s * math.sin(theta))
        rows.append((t_s, local, vel))
    return rows


def _sample_eight(stage: dict[str, Any], *, samples: int = 151) -> list[tuple[float, Vec2, Vec2]]:
    path = stage["path"]
    duration_s = float(stage["duration_s"])
    along_amp = float(path["along_amplitude_m"])
    lateral_amp = float(path["lateral_amplitude_m"])
    omega = float(path["omega_rad_s"])
    rows: list[tuple[float, Vec2, Vec2]] = []
    for index in range(samples):
        t_s = duration_s * index / (samples - 1)
        local = (along_amp * math.sin(omega * t_s), lateral_amp * math.sin(2.0 * omega * t_s))
        vel = (
            along_amp * omega * math.cos(omega * t_s),
            lateral_amp * 2.0 * omega * math.cos(2.0 * omega * t_s),
        )
        rows.append((t_s, local, vel))
    return rows


def _trajectory_payload(
    *,
    shape: str,
    rows: list[tuple[float, Vec2, Vec2]],
    mapper: Any,
    fixed_z_m: float | None,
) -> dict[str, Any]:
    payload_rows = []
    max_speed = 0.0
    for t_s, local_xy, local_vxy in rows:
        base_xy = mapper(local_xy)
        speed = math.hypot(local_vxy[0], local_vxy[1])
        max_speed = max(max_speed, speed)
        payload_rows.append(
            {
                "t_s": t_s,
                "local_xy_m": [local_xy[0], local_xy[1]],
                "base_xy_m": [base_xy[0], base_xy[1]],
                "local_vxy_m_s": [local_vxy[0], local_vxy[1]],
                "reference_speed_m_s": speed,
                "base_z_m": fixed_z_m,
            }
        )
    return {
        "schema": "offline_task_space_reference_v1",
        "shape": shape,
        "sample_count": len(payload_rows),
        "max_reference_speed_m_s": max_speed,
        "rows": payload_rows,
    }


def _simulated_force(rows: list[dict[str, Any]], *, contact_surface_z_m: float) -> dict[str, Any]:
    force_rows = []
    max_force = 0.0
    for row in rows:
        t_s = float(row["t_s"])
        z_m = float(row["base_z_m"] if row["base_z_m"] is not None else contact_surface_z_m)
        penetration = max(0.0, contact_surface_z_m - z_m)
        load = max(0.0, 5.0 + 0.25 * math.sin(0.7 * t_s) + penetration * 500.0)
        max_force = max(max_force, load)
        force_rows.append(
            {
                "t_s": t_s,
                "Fx_N": 0.0,
                "Fy_N": 0.0,
                "Fz_N": load,
                "Mx_Nm": 0.0,
                "My_Nm": 0.0,
                "Mz_Nm": 0.0,
                "reaction_normal": [0.0, 0.0, 1.0],
                "approach_normal": [0.0, 0.0, -1.0],
                "normal_load_n": load,
                "force_norm_n": load,
            }
        )
    return {
        "schema": "simulated_kunwei_wrench_v1",
        "force_source": "simulated_kunwei_offline_trace",
        "contact_surface_z_m": contact_surface_z_m,
        "reaction_normal": [0.0, 0.0, 1.0],
        "approach_normal": [0.0, 0.0, -1.0],
        "normal_load_definition": "dot(force_base, reaction_normal)",
        "max_force_norm_n": max_force,
        "max_normal_load_n": max_force,
        "sample_count": len(force_rows),
        "rows": force_rows,
    }


def build_stage_artifact(stage_id: str) -> dict[str, Any]:
    if stage_id not in STAGE_REGISTRY:
        raise KeyError(stage_id)
    spec = STAGE_REGISTRY[stage_id]
    stage = _stage_from_table(spec.stage_table_path, spec.source_stage_id)
    artifact = _stage_common(spec, stage)
    safe_frame = load_json(spec.safe_frame_path)

    if stage_id == "step5a":
        step5a_spec = load_json(STEP5A_SPEC)
        trajectory = _trajectory_payload(
            shape="cycloid",
            rows=_sample_cycloid(stage),
            mapper=lambda local_xy: _step5a_affine_map(step5a_spec, local_xy),
            fixed_z_m=float(stage["fixed_base_z_m"]),
        )
        artifact["source_paths"]["step5a_local_control_spec"] = str(STEP5A_SPEC)
        artifact["stage"]["velocity_cap_m_s"] = float(stage["guard"]["velocity_cap_m_s"])
        artifact["stage"]["velocity_cap_semantics"] = (
            "TP v3 command-vector clamp provenance, not achieved-speed truth"
        )
        artifact["safe_frame"] = {
            "source": str(STEP5_SAFE_FRAME),
            "origin_xy_m": safe_frame["basis"]["origin_xy_m"],
            "frame_map": {
                "mode": "step5a_tp_v3_affine_map",
                "alignment_label": "preserved",
                "source": str(STEP5A_SPEC),
                "reason": "Active Local Control TP v3 baseline uses the affine map exact to shifted drag-teach start/mid/end.",
            },
        }
        artifact["trajectory"] = trajectory
        artifact["simulated_force_evidence"] = None
        artifact["acceptance"] = {
            "artifact_complete": True,
            "physics_closed_loop_claimed": False,
            "live_authorization_ok": not artifact["live_robot_command_authorized"],
        }
        return artifact

    if stage_id == "step5b":
        mvp = step5b_mvp.build_artifact()
        mvp.update(
            {
                "schema": "ur10e_step56_stage_artifact_v1",
                "stage_id": "step5b",
                "stage_label": spec.label,
                "runner_status": spec.runner_status,
                "artifact_status": spec.artifact_status,
                "gazebo_status": spec.gazebo_status,
                "known_blocker": spec.known_blocker,
            }
        )
        mvp["stage"]["source_stage_id"] = spec.source_stage_id
        mvp["stage"]["stage_table_stage"] = stage.get("stage")
        mvp["source_paths"]["textbook_spec"] = str(TEXTBOOK_SPEC)
        mvp["source_paths"]["step5b_authorization_state"] = str(STEP5B_AUTHORIZATION)
        mvp["contact_motion_authorized"] = False
        mvp["zero_ftsensor_authorized"] = False
        mvp["payload_tcp_safety_writes_authorized"] = False
        mvp["acceptance"]["physics_closed_loop_claimed"] = False
        return mvp

    if stage_id in {"step5c", "step5d"}:
        trajectory = _trajectory_payload(
            shape="cycloid",
            rows=_sample_cycloid(stage),
            mapper=lambda local_xy: _rotation_map(safe_frame, local_xy),
            fixed_z_m=None,
        )
        artifact["safe_frame"] = {
            "source": str(STEP5_SAFE_FRAME),
            "origin_xy_m": safe_frame["basis"]["origin_xy_m"],
            "frame_map": {
                "mode": "step5_safe_frame_rotation",
                "alignment_label": "changed_with_reason" if stage_id == "step5d" else "out_of_scope",
                "reason": "Offline schematic shadow; not a live executor and not a strict RNN physics claim.",
            },
        }
        artifact["trajectory"] = trajectory
        if spec.contact:
            artifact["simulated_force_evidence"] = _simulated_force(
                trajectory["rows"],
                contact_surface_z_m=step5b_mvp.CONTACT_SURFACE_Z_M,
            )
        else:
            artifact["simulated_force_evidence"] = None
        artifact["acceptance"] = {
            "artifact_complete": True,
            "physics_closed_loop_claimed": False,
            "live_authorization_ok": not artifact["live_robot_command_authorized"],
            "runner_not_runnable_by_design": True,
        }
        return artifact

    if stage_id in {"step6a", "step6b"}:
        trajectory = _trajectory_payload(
            shape="eight",
            rows=_sample_eight(stage),
            mapper=lambda local_xy: _rotation_map(safe_frame, local_xy),
            fixed_z_m=stage.get("fixed_base_z_m"),
        )
        artifact["safe_frame"] = {
            "source": str(STEP6_SAFE_FRAME),
            "origin_xy_m": safe_frame["basis"]["origin_xy_m"],
            "waypoint_count": len(safe_frame.get("waypoints", [])),
            "frame_map": {
                "mode": "step6_eight_safe_frame_rotation",
                "alignment_label": "preserved",
                "policy": safe_frame["policy"]["fit"],
            },
        }
        artifact["trajectory"] = trajectory
        if spec.contact:
            artifact["simulated_force_evidence"] = _simulated_force(
                trajectory["rows"],
                contact_surface_z_m=step5b_mvp.CONTACT_SURFACE_Z_M,
            )
        else:
            artifact["simulated_force_evidence"] = None
        artifact["acceptance"] = {
            "artifact_complete": True,
            "physics_closed_loop_claimed": False,
            "live_authorization_ok": not artifact["live_robot_command_authorized"],
        }
        return artifact

    raise AssertionError(stage_id)


def write_stage_artifact(stage_id: str, output_dir: Path) -> Path:
    artifact = build_stage_artifact(stage_id)
    path = output_dir / stage_id / "summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def run_matrix(stage: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stage_ids = list(STAGE_REGISTRY) if stage == "all" else [stage]
    if stage != "all" and stage not in STAGE_REGISTRY:
        raise SystemExit(f"unknown stage {stage!r}; expected all or one of {', '.join(STAGE_REGISTRY)}")
    stage_paths = {stage_id: write_stage_artifact(stage_id, output_dir) for stage_id in stage_ids}
    if stage != "all":
        return stage_paths[stage]

    summary = {
        "schema": "ur10e_step56_simulation_matrix_v1",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "offline_no_motion",
        "live_robot_command_authorized": False,
        "stage_count": len(stage_paths),
        "source_paths": {
            "textbook_spec": str(TEXTBOOK_SPEC),
            "step5_stage_table": str(STEP5_STAGE_TABLE),
            "step6_stage_table": str(STEP6_STAGE_TABLE),
            "step5_safe_frame": str(STEP5_SAFE_FRAME),
            "step6_eight_safe_frame": str(STEP6_SAFE_FRAME),
        },
        "stages": [
            {
                "stage_id": stage_id,
                "summary": str(path),
                "runner_status": STAGE_REGISTRY[stage_id].runner_status,
                "gazebo_status": STAGE_REGISTRY[stage_id].gazebo_status,
                "known_blocker": STAGE_REGISTRY[stage_id].known_blocker,
            }
            for stage_id, path in stage_paths.items()
        ],
    }
    matrix_path = output_dir / "matrix_summary.json"
    matrix_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return matrix_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate offline UR10e Step5/Step6 simulation matrix artifacts.")
    parser.add_argument("--stage", default="all", choices=["all", *STAGE_REGISTRY.keys()])
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = run_matrix(args.stage, args.output_dir)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
