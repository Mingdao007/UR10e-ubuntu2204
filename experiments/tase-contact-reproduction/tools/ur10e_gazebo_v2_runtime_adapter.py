#!/usr/bin/env python3
"""Velocity-only Gazebo Fortress runtime for the shared Step5d v30 seam.

The module keeps ROS imports inside :func:`run_ros_runtime` so the frame,
manifest, and fail-closed contracts remain unit-testable without ROS.  It may
only publish the ``RegisterCommand.qdot`` produced by ``Step5dSimulatorAdapter``.
It never imports RTDE, Dashboard, or a real-controller transport.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import signal
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))
if str(EXPERIMENT_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(EXPERIMENT_ROOT / "tools"))

from ur10e_example_controllers.ur10e_gazebo_v2 import (  # noqa: E402
    ACTIVE_TCP_OFFSET_TOOL0_M,
    EOAT_CONTACT_COLLISION,
    EOAT_FIXED_JOINT,
    JOINT_NAMES,
    NATIVE_CONTACT_TOPIC,
    NATIVE_FT_TOPIC,
)


RUNTIME_SCHEMA = "ur10e_gazebo_v2_velocity_runtime_v1"
PROFILE_ID = "step5d_strict_rnn_press_probe_velocity_v1"
BACKEND = "velocity"
COMMAND_TOPIC = "/gazebo_v2_velocity_controller/commands"
JOINT_STATE_TOPIC = "/joint_states"
CAMERA_VIEWS = ("wide", "oblique", "close", "contact")
CAMERA_TOPICS = {name: f"/ur10e/gazebo_v2/camera/{name}" for name in CAMERA_VIEWS}
CONTROL_PERIOD_S = 0.002
CONTACT_STALE_S = 0.010
SENSOR_STALE_S = 0.100
BASE_FROM_WORLD = np.diag((-1.0, -1.0, 1.0))
DEFAULT_REACTION_NORMAL_BASE = np.asarray((0.0, 0.0, 1.0), dtype=float)
PREWARM_EXECUTE_COUNT = 100
COMMAND_RESPONSE_TOLERANCE_RAD_S = 0.002
ZERO6 = (0.0,) * 6
SOURCE_BINDING_PATHS = {
    "runtime_adapter": Path(__file__).resolve(),
    "simulator_adapter": EXPERIMENT_ROOT / "tools" / "step5d_simulator_adapter.py",
    "control_contract": EXPERIMENT_ROOT / "tools" / "step5d_control_contract.py",
    "strict_rnn": EXPERIMENT_ROOT / "tools" / "step5c_strict_rnn.py",
    "p0_target": EXPERIMENT_ROOT / "tools" / "step5d_p0_v8_control_core.py",
    "gazebo_lane": PACKAGE / "ur10e_example_controllers" / "ur10e_gazebo_v2.py",
    "gazebo_world": PACKAGE / "worlds" / "ur10e_gazebo_v2_fortress.sdf",
    "velocity_controller": PACKAGE / "config" / "gazebo_v2_velocity_controllers.yaml",
    "lane_contract": EXPERIMENT_ROOT / "config" / "gazebo_v2_lane_contract.json",
    "tick_schema": EXPERIMENT_ROOT / "config" / "schemas" / "ur10e_gazebo_v2_tick_v1.schema.json",
}


class RuntimeContractError(ValueError):
    """Raised when a native message cannot be canonicalized safely."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite_vector(values: Sequence[float], size: int, *, label: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.shape != (size,) or not np.all(np.isfinite(array)):
        raise RuntimeContractError(f"{label}_not_finite_vector{size}")
    return array


def _finite_rotation(values: Sequence[Sequence[float]], *, label: str) -> np.ndarray:
    rotation = np.asarray(values, dtype=float)
    if rotation.shape != (3, 3) or not np.all(np.isfinite(rotation)):
        raise RuntimeContractError(f"{label}_not_finite_rotation")
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6) or not math.isclose(
        float(np.linalg.det(rotation)), 1.0, abs_tol=1e-6
    ):
        raise RuntimeContractError(f"{label}_not_orthonormal")
    return rotation


def transform_wrench_rotation_only(
    wrench: Sequence[float],
    rotation_target_from_source: Sequence[Sequence[float]],
) -> list[float]:
    """Rotate force and torque without silently changing their origin."""

    value = _finite_vector(wrench, 6, label="wrench")
    rotation = _finite_rotation(rotation_target_from_source, label="wrench_rotation")
    return [float(item) for item in np.r_[rotation @ value[:3], rotation @ value[3:]]]


def shift_wrench_origin(
    wrench: Sequence[float],
    target_from_source_m: Sequence[float],
) -> list[float]:
    """Express a wrench at a target origin in the same oriented frame.

    ``target_from_source_m`` points from the source origin to the target origin.
    Therefore ``tau_target = tau_source - r_source_target x force``.
    """

    value = _finite_vector(wrench, 6, label="wrench")
    lever = _finite_vector(target_from_source_m, 3, label="target_from_source")
    shifted = value.copy()
    shifted[3:] = value[3:] - np.cross(lever, value[:3])
    return [float(item) for item in shifted]


def canonical_contact_row(
    *,
    run_id: str,
    sim_time_s: float,
    collision1: str,
    collision2: str,
    body1_wrench: Sequence[float],
    body2_wrench: Sequence[float],
    contact_normal_world: Sequence[float],
    raw_frame_id: str,
) -> dict[str, Any]:
    """Select the attached EOAT body and express its native wrench in ``base``."""

    if not run_id or not math.isfinite(float(sim_time_s)) or float(sim_time_s) < 0.0:
        raise RuntimeContractError("contact_identity_or_time_invalid")
    if EOAT_CONTACT_COLLISION in collision1 and "step5_contact_surface" in collision2:
        selected_side = 1
        selected = body1_wrench
        contact_collision, surface_collision = collision1, collision2
    elif EOAT_CONTACT_COLLISION in collision2 and "step5_contact_surface" in collision1:
        selected_side = 2
        selected = body2_wrench
        contact_collision, surface_collision = collision2, collision1
    else:
        raise RuntimeContractError("contact_collision_pair_not_attached_eoat_surface")
    if raw_frame_id and "world" not in raw_frame_id.lower():
        raise RuntimeContractError("contact_wrench_frame_unknown")
    normal_world = _finite_vector(contact_normal_world, 3, label="contact_normal_world")
    normal_norm = float(np.linalg.norm(normal_world))
    if normal_norm <= 1e-12:
        raise RuntimeContractError("contact_normal_world_zero")
    comparison = transform_wrench_rotation_only(selected, BASE_FROM_WORLD)
    normal_base = BASE_FROM_WORLD @ (normal_world / normal_norm)
    if float(np.dot(np.asarray(comparison[:3]), normal_base)) < 0.0:
        normal_base = -normal_base
    normal_projection = float(np.dot(np.asarray(comparison[:3]), normal_base))
    if normal_projection <= 0.0:
        raise RuntimeContractError("contact_wrench_reaction_sign_mismatch")
    return {
        "run_id": run_id,
        "sim_time_s": float(sim_time_s),
        "source": "gazebo_native_contact",
        "topic": NATIVE_CONTACT_TOPIC,
        "contact_collision": contact_collision,
        "surface_collision": surface_collision,
        "selected_body_side": selected_side,
        "native_wrench": [float(value) for value in selected],
        "native_wrench_frame": raw_frame_id or "gazebo_contact_body_wrench_world_semantics",
        "frame_source": "raw_joint_wrench_header" if raw_frame_id else "manifest_bound_gazebo_contacts_world_semantics",
        "comparison_frame": "base",
        "comparison_origin": "eoat_joint_child",
        "comparison_convention": "force_on_eoat_along_reaction_normal",
        "comparison_wrench_on_eoat": comparison,
        "reaction_normal": [float(value) for value in normal_base],
        "reaction_normal_source": "gazebo_contact_message_normal_oriented_by_selected_eoat_body_wrench",
        "normal_force_projection_n": normal_projection,
    }


def canonical_ft_row(
    *,
    run_id: str,
    sim_time_s: float,
    raw_wrench_sensor: Sequence[float],
    tare_wrench_sensor: Sequence[float],
    rotation_base_from_sensor: Sequence[Sequence[float]],
    raw_frame_id: str,
    reaction_normal_base: Sequence[float],
) -> dict[str, Any]:
    """Tare and rotate the manifest-bound fixed-joint FT measurement to base."""

    if not run_id or not math.isfinite(float(sim_time_s)) or float(sim_time_s) < 0.0:
        raise RuntimeContractError("ft_identity_or_time_invalid")
    raw = _finite_vector(raw_wrench_sensor, 6, label="raw_ft")
    tare = _finite_vector(tare_wrench_sensor, 6, label="tare_ft")
    if raw_frame_id and not any(
        token in raw_frame_id.lower()
        for token in ("real_aligned_eoat_visual_stack", "gazebo_v2_native_ft", "child")
    ):
        raise RuntimeContractError("ft_wrench_frame_unknown")
    compensated = raw - tare
    comparison = transform_wrench_rotation_only(compensated, rotation_base_from_sensor)
    reaction = _finite_vector(reaction_normal_base, 3, label="reaction_normal_base")
    norm = float(np.linalg.norm(reaction))
    if norm <= 1e-12:
        raise RuntimeContractError("reaction_normal_base_zero")
    reaction = reaction / norm
    if float(np.dot(np.asarray(comparison[:3]), reaction)) <= 0.0:
        raise RuntimeContractError("ft_wrench_reaction_sign_mismatch")
    return {
        "run_id": run_id,
        "sim_time_s": float(sim_time_s),
        "source": "gazebo_native_ft",
        "topic": NATIVE_FT_TOPIC,
        "sensor_joint": EOAT_FIXED_JOINT,
        "frame": "child",
        "measure_direction": "child_to_parent",
        "native_wrench": [float(value) for value in raw],
        "simulated_tare_wrench": [float(value) for value in tare],
        "native_wrench_frame": raw_frame_id or "eoat_joint_child",
        "frame_source": "raw_wrench_header" if raw_frame_id else "manifest_bound_fixed_joint_ft_sensor",
        "comparison_frame": "base",
        "comparison_origin": "eoat_joint_child",
        "comparison_convention": "force_on_eoat_along_reaction_normal",
        "comparison_wrench_on_eoat": comparison,
        "reaction_normal": [float(value) for value in reaction],
        "reaction_normal_source": "same_run_gazebo_contact_message_normal",
    }


def materialize_source_bindings(run_dir: Path) -> dict[str, Any]:
    binding_dir = run_dir / "runtime_bindings"
    binding_dir.mkdir(parents=True, exist_ok=True)
    rows: dict[str, dict[str, Any]] = {}
    for source_id, source in sorted(SOURCE_BINDING_PATHS.items()):
        if not source.is_file():
            raise FileNotFoundError(f"runtime source binding missing ({source_id}): {source}")
        suffix = "".join(source.suffixes) or ".bin"
        target = binding_dir / f"{source_id}{suffix}"
        shutil.copy2(source, target)
        rows[source_id] = {
            "artifact_path": str(target.relative_to(run_dir)),
            "size": target.stat().st_size,
            "sha256": sha256_file(target),
        }
    material = "\n".join(f"{key}:{row['sha256']}" for key, row in sorted(rows.items()))
    return {
        "files": rows,
        "composite_sha256": hashlib.sha256(material.encode("utf-8")).hexdigest(),
    }


def validate_runtime_manifest(run_dir: Path, payload: Mapping[str, Any], *, run_id: str, backend: str) -> list[str]:
    issues: list[str] = []
    if payload.get("schema") != RUNTIME_SCHEMA:
        issues.append("runtime_manifest:schema_mismatch")
    if payload.get("run_id") != run_id:
        issues.append("runtime_manifest:run_id_mismatch")
    if payload.get("backend") != backend or backend != BACKEND:
        issues.append("runtime_manifest:backend_not_velocity")
    if payload.get("status") != "complete":
        issues.append("runtime_manifest:status_not_complete")
    profile = payload.get("profile") if isinstance(payload.get("profile"), Mapping) else {}
    expected_profile = {
        "id": PROFILE_ID,
        "backend": "cupy",
        "inner_iterations": 512,
        "epsilon": 0.010,
        "sigr_exponent_r": 0.8,
        "qdot_cap_rad_s": 0.05,
        "dls_runtime_fallback_allowed": False,
    }
    for key, expected in expected_profile.items():
        if profile.get(key) != expected:
            issues.append(f"runtime_manifest:profile_mismatch:{key}")
    prewarm = payload.get("prewarm") if isinstance(payload.get("prewarm"), Mapping) else {}
    if (
        prewarm.get("complete") is not True
        or prewarm.get("branch") != "unmeasured_execute_path_no_command_publish"
        or prewarm.get("execute_count") != PREWARM_EXECUTE_COUNT
        or prewarm.get("required_execute_count") != PREWARM_EXECUTE_COUNT
        or prewarm.get("commands_published") is not False
        or prewarm.get("solver_state_reset_after") is not True
    ):
        issues.append("runtime_manifest:prewarm_contract_invalid")
    sources = payload.get("source_bindings") if isinstance(payload.get("source_bindings"), Mapping) else {}
    files = sources.get("files") if isinstance(sources.get("files"), Mapping) else {}
    missing = set(SOURCE_BINDING_PATHS) - set(files)
    issues.extend(f"runtime_manifest:source_missing:{key}" for key in sorted(missing))
    material_rows: list[tuple[str, str]] = []
    for source_id, row in sorted(files.items()):
        if not isinstance(row, Mapping):
            issues.append(f"runtime_manifest:source_row_invalid:{source_id}")
            continue
        relative = Path(str(row.get("artifact_path") or ""))
        path = (run_dir / relative).resolve()
        try:
            path.relative_to(run_dir.resolve())
        except ValueError:
            issues.append(f"runtime_manifest:source_path_escape:{source_id}")
            continue
        if not path.is_file():
            issues.append(f"runtime_manifest:source_file_missing:{source_id}")
            continue
        actual = sha256_file(path)
        if row.get("size") != path.stat().st_size:
            issues.append(f"runtime_manifest:source_size_mismatch:{source_id}")
        if row.get("sha256") != actual:
            issues.append(f"runtime_manifest:source_hash_mismatch:{source_id}")
        material_rows.append((str(source_id), actual))
    material = "\n".join(f"{key}:{digest}" for key, digest in material_rows)
    if sources.get("composite_sha256") != hashlib.sha256(material.encode("utf-8")).hexdigest():
        issues.append("runtime_manifest:source_composite_mismatch")
    counters = payload.get("counters") if isinstance(payload.get("counters"), Mapping) else {}
    positive = ("tick_count", "command_publish_count", "native_ft_row_count")
    for key in positive:
        if not isinstance(counters.get(key), int) or int(counters.get(key) or 0) <= 0:
            issues.append(f"runtime_manifest:counter_not_positive:{key}")
    for key in (
        "sequence_gap_count",
        "period_miss_count",
        "nonfinite_count",
        "command_publish_failure_count",
        "command_response_mismatch_count",
        "rejected_nonzero_command_count",
    ):
        if counters.get(key) != 0:
            issues.append(f"runtime_manifest:counter_not_zero:{key}")
    if counters.get("native_contact_row_count", 0) <= 0:
        issues.append("runtime_manifest:native_contact_rows_zero")
    if counters.get("camera_view_count") != len(CAMERA_VIEWS):
        issues.append("runtime_manifest:camera_views_incomplete")
    if payload.get("all_rejected_commands_exact_zero") is not True:
        issues.append("runtime_manifest:rejected_command_not_exact_zero")
    if payload.get("controller_delivery_proven") is not True:
        issues.append("runtime_manifest:controller_delivery_not_proven")
    if counters.get("command_response_count") != counters.get("command_publish_count"):
        issues.append("runtime_manifest:command_response_count_mismatch")
    artifact_rows: dict[str, list[dict[str, Any]]] = {}
    for name in ("tick_trace.jsonl", "native_contact.jsonl", "native_ft.jsonl"):
        path = run_dir / name
        if not path.is_file():
            issues.append(f"runtime_manifest:artifact_missing:{name}")
            continue
        rows: list[dict[str, Any]] = []
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                issues.append(f"runtime_manifest:{name}:json_invalid:{index}")
                continue
            try:
                stamp = float(row.get("sim_time_s", math.nan)) if isinstance(row, dict) else math.nan
            except (TypeError, ValueError):
                stamp = math.nan
            if not isinstance(row, dict) or not math.isfinite(stamp):
                issues.append(f"runtime_manifest:{name}:sim_time_invalid:{index}")
                continue
            rows.append(row)
        artifact_rows[name] = rows
    tick_times = [float(row["sim_time_s"]) for row in artifact_rows.get("tick_trace.jsonl", [])]
    if tick_times:
        window = (min(tick_times), max(tick_times))
        for name in ("native_contact.jsonl", "native_ft.jsonl"):
            for index, row in enumerate(artifact_rows.get(name, [])):
                stamp = float(row["sim_time_s"])
                if not window[0] <= stamp <= window[1]:
                    issues.append(f"runtime_manifest:{name}:outside_tick_window:{index}")
        camera_path = run_dir / "camera_manifest.json"
        if not camera_path.is_file():
            issues.append("runtime_manifest:camera_manifest_missing")
        else:
            camera = json.loads(camera_path.read_text(encoding="utf-8"))
            views = camera.get("views") if isinstance(camera, Mapping) else {}
            for name in CAMERA_VIEWS:
                row = views.get(name) if isinstance(views, Mapping) else None
                stamp = row.get("sim_time_s") if isinstance(row, Mapping) else None
                if not isinstance(stamp, (int, float)) or not window[0] <= float(stamp) <= window[1]:
                    issues.append(f"runtime_manifest:camera_outside_tick_window:{name}")
    else:
        issues.append("runtime_manifest:tick_window_missing")
    blockers = payload.get("blockers")
    if blockers != []:
        issues.append("runtime_manifest:blockers_present")
    return list(dict.fromkeys(issues))


@dataclass
class _StampedVector:
    stamp_s: float
    values: np.ndarray
    frame_id: str = ""


def _stamp_s(header: Any) -> float:
    stamp = getattr(header, "stamp", None)
    if stamp is None:
        return math.nan
    return float(getattr(stamp, "sec", 0)) + float(getattr(stamp, "nanosec", 0)) * 1e-9


def _json_dumpable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_dumpable(item) for key, item in value.items()}
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_dumpable(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def run_ros_runtime(args: argparse.Namespace) -> int:  # noqa: C901 - runtime lifecycle is intentionally centralized.
    import pinocchio as pin
    import rclpy
    from geometry_msgs.msg import WrenchStamped
    from rclpy.node import Node
    from rclpy.parameter import Parameter
    from rclpy.qos import qos_profile_sensor_data
    from ros_gz_interfaces.msg import Contacts
    from sensor_msgs.msg import Image, JointState
    from std_msgs.msg import Float64MultiArray

    import step5c_calibrated_kinematics_audit as calibrated
    from kunwei_rtde_bridge import step5d_omega_bounds, step5d_tcp_jacobian_base
    from run_step5d_p0_v8_mujoco import make_solver, warm_solver
    from step5d_control_contract import DeferredV30Diagnostics, SafetyEnvelope, StrictRnnControlPolicy
    from step5d_p0_v8_control_core import build_p0_v8_target
    from step5d_simulator_adapter import (
        FrameLineage,
        SimulatorState,
        Step5dIngressGuard,
        Step5dSimulatorAdapter,
    )

    run_dir = args.run_dir.resolve()
    if not run_dir.is_dir():
        raise SystemExit(f"run directory must already exist: {run_dir}")
    if args.backend != BACKEND:
        raise SystemExit("Gazebo v2 runtime currently supports velocity only")
    source_bindings = materialize_source_bindings(run_dir)
    model_bundle = calibrated.build_calibrated_model()
    tcp_offset = np.asarray(ACTIVE_TCP_OFFSET_TOOL0_M, dtype=float)
    solver = make_solver(EXPERIMENT_ROOT)
    stop = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop["requested"] = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    class RuntimeNode(Node):
        def __init__(self) -> None:
            super().__init__(
                "ur10e_gazebo_v2_velocity_runtime",
                parameter_overrides=[Parameter("use_sim_time", value=True)],
            )
            self.joint: _StampedVector | None = None
            self.ft: _StampedVector | None = None
            self.ft_tare_samples: list[np.ndarray] = []
            self.ft_tare: np.ndarray | None = None
            self.latest_contacts: Any = None
            self.latest_contact_stamp_s = -math.inf
            self.latest_reaction_normal_base: np.ndarray | None = None
            self.camera_messages: dict[str, Any] = {}
            self.sequence = 0
            self.start_sim_s: float | None = None
            self.last_sim_s: float | None = None
            self.last_joint_stamp_s: float | None = None
            self.adapter: Step5dSimulatorAdapter | None = None
            self.pending_tick: dict[str, Any] | None = None
            self.pending_command: np.ndarray | None = None
            self.pending_published = False
            self.prewarm_complete = False
            self.prewarm_execute_count = 0
            self.finished = False
            self.blockers: list[str] = []
            self.counters = {
                "tick_count": 0,
                "accepted_count": 0,
                "rejected_count": 0,
                "sequence_gap_count": 0,
                "period_miss_count": 0,
                "nonfinite_count": 0,
                "command_publish_count": 0,
                "command_publish_failure_count": 0,
                "command_response_count": 0,
                "command_response_mismatch_count": 0,
                "rejected_nonzero_command_count": 0,
                "native_contact_row_count": 0,
                "native_ft_row_count": 0,
                "camera_view_count": 0,
            }
            self.tick_handle = (run_dir / "tick_trace.jsonl").open("w", encoding="utf-8")
            self.contact_handle = (run_dir / "native_contact.jsonl").open("w", encoding="utf-8")
            self.ft_handle = (run_dir / "native_ft.jsonl").open("w", encoding="utf-8")
            self.publisher = self.create_publisher(Float64MultiArray, COMMAND_TOPIC, 1)
            self.create_subscription(JointState, JOINT_STATE_TOPIC, self.on_joint, qos_profile_sensor_data)
            self.create_subscription(WrenchStamped, NATIVE_FT_TOPIC, self.on_ft, qos_profile_sensor_data)
            self.create_subscription(Contacts, NATIVE_CONTACT_TOPIC, self.on_contacts, qos_profile_sensor_data)
            for name, topic in CAMERA_TOPICS.items():
                self.create_subscription(
                    Image,
                    topic,
                    lambda message, view=name: self.on_camera(view, message),
                    qos_profile_sensor_data,
                )
            self.create_timer(CONTROL_PERIOD_S, self.on_tick)

        def now_s(self) -> float:
            return self.get_clock().now().nanoseconds * 1e-9

        def on_joint(self, message: JointState) -> None:
            positions = dict(zip(message.name, message.position))
            velocities = dict(zip(message.name, message.velocity))
            if not all(name in positions and name in velocities for name in JOINT_NAMES):
                self.blockers.append("joint_state_missing_required_joint")
                return
            values = np.asarray(
                [positions[name] for name in JOINT_NAMES] + [velocities[name] for name in JOINT_NAMES],
                dtype=float,
            )
            if not np.all(np.isfinite(values)):
                self.counters["nonfinite_count"] += 1
                return
            stamp = _stamp_s(message.header)
            if not math.isfinite(stamp):
                stamp = self.now_s()
            if self.last_joint_stamp_s is not None and abs(stamp - self.last_joint_stamp_s - CONTROL_PERIOD_S) > 5e-5:
                self.counters["sequence_gap_count"] += 1
            self.last_joint_stamp_s = stamp
            if self.pending_tick is not None and self.pending_command is not None:
                response_error = float(np.max(np.abs(values[6:] - self.pending_command)))
                response_ok = bool(
                    self.pending_published
                    and stamp > float(self.pending_tick["sim_time_s"])
                    and response_error <= COMMAND_RESPONSE_TOLERANCE_RAD_S
                )
                self.pending_tick["command"]["applied"] = response_ok
                self.pending_tick["candidate"]["diagnostics"]["command_response_max_abs_error_rad_s"] = response_error
                if response_ok:
                    self.counters["command_response_count"] += 1
                else:
                    self.counters["command_response_mismatch_count"] += 1
                self.tick_handle.write(
                    json.dumps(_json_dumpable(self.pending_tick), sort_keys=True) + "\n"
                )
                self.counters["tick_count"] += 1
                self.pending_tick = None
                self.pending_command = None
                self.pending_published = False
            self.joint = _StampedVector(stamp, values)

        def on_ft(self, message: WrenchStamped) -> None:
            wrench = message.wrench
            values = np.asarray(
                [
                    wrench.force.x,
                    wrench.force.y,
                    wrench.force.z,
                    wrench.torque.x,
                    wrench.torque.y,
                    wrench.torque.z,
                ],
                dtype=float,
            )
            if not np.all(np.isfinite(values)):
                self.counters["nonfinite_count"] += 1
                return
            stamp = _stamp_s(message.header)
            if not math.isfinite(stamp):
                stamp = self.now_s()
            self.ft = _StampedVector(stamp, values, str(message.header.frame_id or ""))
            if self.ft_tare is None:
                self.ft_tare_samples.append(values.copy())
                if len(self.ft_tare_samples) >= args.tare_samples:
                    self.ft_tare = np.mean(np.vstack(self.ft_tare_samples), axis=0)

        def on_contacts(self, message: Contacts) -> None:
            stamp = _stamp_s(message.header)
            if not math.isfinite(stamp):
                stamp = self.now_s()
            self.latest_contacts = message
            self.latest_contact_stamp_s = stamp

        def on_camera(self, view: str, message: Image) -> None:
            if self.start_sim_s is not None and view not in self.camera_messages:
                self.camera_messages[view] = message

        def tool_rotation(self, q: np.ndarray) -> np.ndarray:
            pin.forwardKinematics(model_bundle.model, model_bundle.data, q)
            pin.updateFramePlacements(model_bundle.model, model_bundle.data)
            base = model_bundle.data.oMf[model_bundle.base_frame_id]
            tool = model_bundle.data.oMf[model_bundle.tool0_frame_id]
            return np.asarray((base.inverse() * tool).rotation, dtype=float)

        def build_state(self, sim_s: float) -> SimulatorState | None:
            if self.joint is None or self.ft is None or self.ft_tare is None:
                return None
            q = self.joint.values[:6].copy()
            qd = self.joint.values[6:].copy()
            rotation = self.tool_rotation(q)
            pin.forwardKinematics(model_bundle.model, model_bundle.data, q)
            pin.updateFramePlacements(model_bundle.model, model_bundle.data)
            base = model_bundle.data.oMf[model_bundle.base_frame_id]
            tool = model_bundle.data.oMf[model_bundle.tool0_frame_id]
            base_to_tool = base.inverse() * tool
            tcp_position = np.asarray(base_to_tool.translation, dtype=float) + rotation @ tcp_offset
            tcp_rotation = rotation
            tcp_rotvec = np.asarray(pin.log3(tcp_rotation), dtype=float)
            jacobian = step5d_tcp_jacobian_base(model_bundle, q, tcp_offset)
            twist = jacobian @ qd
            compensated_sensor = self.ft.values - self.ft_tare
            wrench_base_sensor = np.asarray(
                transform_wrench_rotation_only(compensated_sensor, rotation), dtype=float
            )
            wrench_base_tcp = np.asarray(
                shift_wrench_origin(wrench_base_sensor, rotation @ tcp_offset), dtype=float
            )
            reaction = (
                self.latest_reaction_normal_base
                if self.latest_reaction_normal_base is not None
                else DEFAULT_REACTION_NORMAL_BASE
            )
            normal_load = max(0.0, float(np.dot(wrench_base_tcp[:3], reaction)))
            target = build_p0_v8_target(
                tcp_pose_base=tuple(float(value) for value in np.r_[tcp_position, tcp_rotvec]),
                tcp_speed_base=tuple(float(value) for value in twist),
                force_tcp_n=tuple(float(value) for value in wrench_base_tcp[:3]),
                reaction_normal_b=tuple(float(value) for value in reaction),
                normal_load_n=normal_load,
                force_error_n=1.0 - normal_load,
                jacobian=jacobian,
                dt_s=CONTROL_PERIOD_S,
                qdot_cap_rad_s=0.05,
            )
            lower, upper = step5d_omega_bounds(
                q,
                model_bundle.model.lowerPositionLimit,
                model_bundle.model.upperPositionLimit,
                alpha_s_inv=1.0,
                qdot_limit_rad_s=0.05,
            )
            contact_count = 0
            if sim_s - self.latest_contact_stamp_s <= CONTACT_STALE_S and self.latest_contacts is not None:
                contact_count = len(self.latest_contacts.contacts)
            lineage_material = json.dumps(
                {
                    "calibration_hash": model_bundle.calibration_hash,
                    "source_composite": source_bindings["composite_sha256"],
                    "base_from_world": BASE_FROM_WORLD.tolist(),
                    "tcp_offset": list(ACTIVE_TCP_OFFSET_TOOL0_M),
                },
                sort_keys=True,
            )
            lineage = FrameLineage(
                command_frame="base",
                pose_frame="base",
                twist_frame="base",
                wrench_frame="base",
                jacobian_frame="base",
                normal_frame="base",
                transform_chain=(
                    "gazebo joint_states->calibrated_pinocchio",
                    "manifest-bound fixed-joint FT child->base",
                    "tool0->active_tcp(manifest)",
                ),
                sha256=hashlib.sha256(lineage_material.encode("utf-8")).hexdigest(),
            )
            age = max(0.0, sim_s - min(self.joint.stamp_s, self.ft.stamp_s))
            return SimulatorState(
                engine="gazebo_fortress",
                engine_version="6.18.0",
                sequence=self.sequence,
                sim_time_s=sim_s,
                wall_time_s=time.monotonic(),
                observation_age_s=age,
                q=tuple(float(value) for value in q),
                qd=tuple(float(value) for value in qd),
                tcp_pose=tuple(float(value) for value in np.r_[tcp_position, tcp_rotvec]),
                tcp_twist=tuple(float(value) for value in twist),
                wrench=tuple(float(value) for value in wrench_base_tcp),
                command_jacobian=tuple(tuple(float(value) for value in row) for row in jacobian),
                desired_twist=target.desired_twist,
                reaction_normal=tuple(float(value) for value in reaction),
                approach_normal=tuple(float(-value) for value in reaction),
                frame_lineage=lineage,
                calibration_hash=model_bundle.calibration_hash,
                model_hash=source_bindings["composite_sha256"],
                omega_minus=tuple(float(value) for value in lower),
                omega_plus=tuple(float(value) for value in upper),
                path_time_s=max(0.0, sim_s - (self.start_sim_s or sim_s)),
                force_error_n=1.0 - normal_load,
                orientation_error_rad=float(target.outer_diagnostics["outer_orientation_angle_rad"]),
                native_contact_count=contact_count,
                cage_collision_count=0,
                tcp_inside_cage=True,
                metadata={"normal_load_n": normal_load},
            )

        def write_native_rows(self, sim_s: float, q: np.ndarray) -> None:
            message = self.latest_contacts
            if message is None or sim_s - self.latest_contact_stamp_s > CONTACT_STALE_S:
                return
            wrote_contact = False
            for contact in message.contacts:
                if not contact.wrenches or not contact.normals:
                    continue
                wrench = contact.wrenches[0]
                try:
                    row = canonical_contact_row(
                        run_id=args.run_id,
                        sim_time_s=sim_s,
                        collision1=str(contact.collision1.name),
                        collision2=str(contact.collision2.name),
                        body1_wrench=(
                            wrench.body_1_wrench.force.x,
                            wrench.body_1_wrench.force.y,
                            wrench.body_1_wrench.force.z,
                            wrench.body_1_wrench.torque.x,
                            wrench.body_1_wrench.torque.y,
                            wrench.body_1_wrench.torque.z,
                        ),
                        body2_wrench=(
                            wrench.body_2_wrench.force.x,
                            wrench.body_2_wrench.force.y,
                            wrench.body_2_wrench.force.z,
                            wrench.body_2_wrench.torque.x,
                            wrench.body_2_wrench.torque.y,
                            wrench.body_2_wrench.torque.z,
                        ),
                        contact_normal_world=(
                            contact.normals[0].x,
                            contact.normals[0].y,
                            contact.normals[0].z,
                        ),
                        raw_frame_id=str(wrench.header.frame_id or ""),
                    )
                except RuntimeContractError as exc:
                    self.blockers.append(str(exc))
                    continue
                self.latest_reaction_normal_base = np.asarray(row["reaction_normal"], dtype=float)
                self.contact_handle.write(json.dumps(row, sort_keys=True) + "\n")
                self.counters["native_contact_row_count"] += 1
                wrote_contact = True
            if (
                wrote_contact
                and self.ft is not None
                and self.ft_tare is not None
                and self.latest_reaction_normal_base is not None
            ):
                try:
                    ft_row = canonical_ft_row(
                        run_id=args.run_id,
                        sim_time_s=sim_s,
                        raw_wrench_sensor=self.ft.values,
                        tare_wrench_sensor=self.ft_tare,
                        rotation_base_from_sensor=self.tool_rotation(q),
                        raw_frame_id=self.ft.frame_id,
                        reaction_normal_base=self.latest_reaction_normal_base,
                    )
                except RuntimeContractError as exc:
                    self.blockers.append(str(exc))
                    return
                self.ft_handle.write(json.dumps(ft_row, sort_keys=True) + "\n")
                self.counters["native_ft_row_count"] += 1

        def on_tick(self) -> None:
            sim_s = self.now_s()
            if sim_s <= 0.0 or self.joint is None or self.ft_tare is None:
                return
            if self.start_sim_s is None:
                state = self.build_state(sim_s)
                if state is None:
                    return
                warm_solver(solver, state)
                warm_adapter = Step5dSimulatorAdapter(
                    policy=StrictRnnControlPolicy(solver),
                    safety_envelope=SafetyEnvelope(),
                    deferred_diagnostics=DeferredV30Diagnostics(capacity=PREWARM_EXECUTE_COUNT),
                    ingress_guard=Step5dIngressGuard(forbid_contact=False),
                )
                for index in range(PREWARM_EXECUTE_COUNT):
                    warm_state = replace(
                        state,
                        sequence=index,
                        sim_time_s=float(state.sim_time_s) + index * CONTROL_PERIOD_S,
                        path_time_s=index * CONTROL_PERIOD_S,
                    )
                    warm_adapter.step(warm_state)
                    self.prewarm_execute_count += 1
                solver.reset_state()
                warm_solver(solver, state)
                self.adapter = Step5dSimulatorAdapter(
                    policy=StrictRnnControlPolicy(solver),
                    safety_envelope=SafetyEnvelope(),
                    deferred_diagnostics=DeferredV30Diagnostics(
                        capacity=max(100, int(args.duration_s / CONTROL_PERIOD_S) + 100)
                    ),
                    ingress_guard=Step5dIngressGuard(forbid_contact=False),
                )
                self.prewarm_complete = True
                self.start_sim_s = sim_s
                self.last_sim_s = None
            if sim_s - self.start_sim_s >= args.duration_s and self.pending_tick is None:
                self.finished = True
                return
            if self.pending_tick is not None:
                return
            if self.last_sim_s is not None:
                period = sim_s - self.last_sim_s
                if abs(period - CONTROL_PERIOD_S) > 5e-5:
                    self.counters["period_miss_count"] += 1
            self.last_sim_s = sim_s
            state = self.build_state(sim_s)
            if state is None or self.adapter is None:
                return
            result = self.adapter.step(state)
            command = Float64MultiArray()
            command.data = list(result.simulation_command.qdot)
            delivered = self.publisher.get_subscription_count() > 0
            if delivered:
                self.publisher.publish(command)
                self.counters["command_publish_count"] += 1
            else:
                self.counters["command_publish_failure_count"] += 1
            if result.control.decision.accepted:
                self.counters["accepted_count"] += 1
            else:
                self.counters["rejected_count"] += 1
                if tuple(float(value) for value in result.simulation_command.qdot) != ZERO6:
                    self.counters["rejected_nonzero_command_count"] += 1
            self.write_native_rows(sim_s, np.asarray(state.q, dtype=float))
            tick = {
                "schema": "ur10e_gazebo_v2_tick_v1",
                "run_id": args.run_id,
                "sequence": self.sequence,
                "sim_time_s": sim_s,
                "backend": BACKEND,
                "state": {
                    "q_rad": list(state.q),
                    "qd_rad_s": list(state.qd),
                    "tcp_pose_base": list(state.tcp_pose),
                    "tcp_twist_base": list(state.tcp_twist),
                    "native_ft_tcp": list(state.wrench),
                    "native_contact_count": state.native_contact_count,
                    "frame_lineage": {
                        "world": "gazebo_world",
                        "spawn_root": "base_link",
                        "control_base": "base",
                        "tool": "tool0",
                        "active_tcp": "active_tcp",
                    },
                },
                "candidate": {
                    "values": list(result.control.candidate.qdot),
                    "source": "strict_rnn",
                    "valid": math.isfinite(float(result.control.candidate.residual_norm)),
                    "diagnostics": {
                        "solver_status": result.control.candidate.solver_status,
                        "residual_norm": result.control.candidate.residual_norm,
                        "active_bounds_count": result.control.candidate.active_bounds_count,
                        "dls_shadow_only": True,
                    },
                },
                "decision": {
                    "action": "hold" if result.control.decision.action == "safe_hold" else result.control.decision.action,
                    "reason": result.control.decision.reason,
                    "accepted": result.control.decision.accepted,
                },
                "command": {
                    "interface": "velocity",
                    "values": list(result.simulation_command.qdot),
                    "sequence": self.sequence,
                    "applied": False,
                },
            }
            self.pending_tick = tick
            self.pending_command = np.asarray(result.simulation_command.qdot, dtype=float)
            self.pending_published = delivered
            self.sequence += 1

        def close_outputs(self) -> None:
            for handle in (self.tick_handle, self.contact_handle, self.ft_handle):
                handle.flush()
                handle.close()

    rclpy.init(args=None)
    node = RuntimeNode()
    try:
        wall_deadline = time.monotonic() + args.duration_s + args.startup_timeout_s
        while rclpy.ok() and not stop["requested"] and not node.finished and time.monotonic() < wall_deadline:
            rclpy.spin_once(node, timeout_sec=0.05)
        if not node.finished:
            node.blockers.append("runtime_duration_not_completed")
    finally:
        zero = Float64MultiArray()
        zero.data = [0.0] * 6
        if node.publisher.get_subscription_count() > 0:
            node.publisher.publish(zero)
        node.close_outputs()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

    camera_views: dict[str, Any] = {}
    for name, message in node.camera_messages.items():
        try:
            from PIL import Image as PilImage

            if message.encoding not in {"rgb8", "bgr8"}:
                raise RuntimeContractError(f"camera_encoding_unsupported:{message.encoding}")
            array = np.frombuffer(message.data, dtype=np.uint8).reshape(message.height, message.step)
            array = array[:, : message.width * 3].reshape(message.height, message.width, 3)
            if message.encoding == "bgr8":
                array = array[:, :, ::-1]
            path = run_dir / f"{name}.png"
            PilImage.fromarray(array).save(path)
            camera_views[name] = {
                "path": path.name,
                "topic": CAMERA_TOPICS[name],
                "sim_time_s": _stamp_s(message.header),
                "sha256": sha256_file(path),
            }
        except Exception as exc:  # noqa: BLE001 - artifact records exact blocker.
            node.blockers.append(f"camera_capture_failed:{name}:{type(exc).__name__}")
    node.counters["camera_view_count"] = len(camera_views)
    (run_dir / "camera_manifest.json").write_text(
        json.dumps({"run_id": args.run_id, "views": camera_views}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manual_checks = {
        "run_id": args.run_id,
        "reviewer": "",
        "reviewed_at": "",
        "review_lane": "independent_observer",
        "camera_manifest_sha256": sha256_file(run_dir / "camera_manifest.json"),
        "view_sha256": {name: row["sha256"] for name, row in camera_views.items()},
        "checks": {
            "ur10e_arm_visible": False,
            "eoat_chain_attached_to_arm_visible": False,
            "contact_surface_visible": False,
            "contact_relationship_visible": False,
        },
    }
    (run_dir / "observer_manual_checks.json").write_text(
        json.dumps(manual_checks, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    node.blockers = list(dict.fromkeys(node.blockers))
    if node.counters["native_contact_row_count"] <= 0:
        node.blockers.append("native_contact_rows_zero")
    if node.counters["camera_view_count"] != len(CAMERA_VIEWS):
        node.blockers.append("camera_views_incomplete")
    for key in (
        "sequence_gap_count",
        "period_miss_count",
        "nonfinite_count",
        "command_publish_failure_count",
        "command_response_mismatch_count",
        "rejected_nonzero_command_count",
    ):
        if node.counters[key] != 0:
            node.blockers.append(f"runtime_counter_not_zero:{key}")
    if node.counters["command_response_count"] != node.counters["command_publish_count"]:
        node.blockers.append("controller_command_response_count_mismatch")
    if node.counters["command_publish_count"] <= 0:
        node.blockers.append("controller_command_publish_count_zero")
    manifest = {
        "schema": RUNTIME_SCHEMA,
        "run_id": args.run_id,
        "backend": BACKEND,
        "status": "complete" if node.finished else "partial",
        "profile": {
            "id": PROFILE_ID,
            "backend": "cupy",
            "inner_iterations": 512,
            "epsilon": 0.010,
            "sigr_exponent_r": 0.8,
            "qdot_cap_rad_s": 0.05,
            "dls_runtime_fallback_allowed": False,
        },
        "prewarm": {
            "complete": node.prewarm_complete and node.prewarm_execute_count == PREWARM_EXECUTE_COUNT,
            "branch": "unmeasured_execute_path_no_command_publish",
            "execute_count": node.prewarm_execute_count,
            "required_execute_count": PREWARM_EXECUTE_COUNT,
            "commands_published": False,
            "solver_state_reset_after": True,
        },
        "source_bindings": source_bindings,
        "topics": {
            "joint_state": JOINT_STATE_TOPIC,
            "native_contact": NATIVE_CONTACT_TOPIC,
            "native_ft": NATIVE_FT_TOPIC,
            "command": COMMAND_TOPIC,
            "cameras": CAMERA_TOPICS,
        },
        "counters": node.counters,
        "all_rejected_commands_exact_zero": node.counters["rejected_nonzero_command_count"] == 0,
        "controller_delivery_proven": node.counters["command_publish_failure_count"] == 0
        and node.counters["command_response_mismatch_count"] == 0
        and node.counters["command_response_count"] == node.counters["command_publish_count"]
        and node.counters["command_publish_count"] > 0,
        "simulated_tare_only_not_bench_zero_ft": True,
        "blockers": list(dict.fromkeys(node.blockers)),
        "claim_boundary": {
            "offline_gazebo_only": True,
            "real_robot_motion": False,
            "package_acceptance": False,
            "live_acceptance": False,
            "reproduction_complete": False,
            "effort_or_true_torque_backend": False,
        },
    }
    (run_dir / "runtime_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if manifest["status"] == "complete" else 2


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--backend", choices=("velocity", "effort_surrogate"), default="velocity")
    parser.add_argument("--duration-s", type=float, default=10.0)
    parser.add_argument("--startup-timeout-s", type=float, default=30.0)
    parser.add_argument("--tare-samples", type=int, default=20)
    args = parser.parse_args(argv)
    if args.duration_s <= 0.0 or args.startup_timeout_s <= 0.0 or args.tare_samples <= 0:
        parser.error("duration, startup timeout, and tare sample count must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    return run_ros_runtime(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
