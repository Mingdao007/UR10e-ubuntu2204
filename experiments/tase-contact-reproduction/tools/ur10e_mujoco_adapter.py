#!/usr/bin/env python3
"""MuJoCo state/source plug-in for the single Step5d simulator adapter.

MuJoCo supplies plant state, native contacts, and an independent FK/J oracle.
The command Jacobian is always computed by the calibrated Pinocchio production
path.  This module never calls the strict-RNN solver or production safety gate.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from build_ur10e_digital_twin_model import fixed_transform, sha256_path
from step5d_p0_v8_control_core import build_p0_v8_target
from step5d_simulator_adapter import (
    P0_V8_QDOT_CAP_RAD_S,
    FrameLineage,
    SimulationCommand,
    SimulatorState,
)


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
TCP_CAGE_PATH = EXPERIMENT_ROOT / "config" / "step5d_v15a_tcp_cage_cache.json"


def external_wrench_at_tcp(
    raw_force_sensor: Sequence[float],
    raw_torque_sensor: Sequence[float],
    *,
    rotation_tcp_from_sensor: Sequence[Sequence[float]],
    sensor_to_tcp_sensor_m: Sequence[float],
) -> tuple[np.ndarray, np.ndarray]:
    """Convert MuJoCo parent->child constraint wrench to external TCP wrench.

    MuJoCo force/torque site sensors report parent-on-child constraint wrench in
    the sensor frame.  Environment-on-EOAT external wrench has the opposite
    sign.  Torque is shifted from the physical sensor origin to the TCP before
    both components are rotated into the TCP frame.
    """

    raw_force = np.asarray(raw_force_sensor, dtype=float)
    raw_torque = np.asarray(raw_torque_sensor, dtype=float)
    rotation = np.asarray(rotation_tcp_from_sensor, dtype=float)
    lever = np.asarray(sensor_to_tcp_sensor_m, dtype=float)
    if (
        raw_force.shape != (3,)
        or raw_torque.shape != (3,)
        or rotation.shape != (3, 3)
        or lever.shape != (3,)
        or not all(
            np.all(np.isfinite(value))
            for value in (raw_force, raw_torque, rotation, lever)
        )
    ):
        raise ValueError("wrench transform inputs must be finite 3D values")
    if not np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-9):
        raise ValueError("rotation_tcp_from_sensor must be orthonormal")
    external_force_sensor = -raw_force
    external_torque_sensor = -raw_torque
    external_force_tcp = rotation @ external_force_sensor
    external_torque_tcp = rotation @ (
        external_torque_sensor - np.cross(lever, external_force_sensor)
    )
    return external_force_tcp, external_torque_tcp


def wrench_tcp_to_base(
    force_tcp: Sequence[float],
    torque_tcp: Sequence[float],
    rotation_base_from_tcp: Sequence[Sequence[float]],
) -> tuple[float, float, float, float, float, float]:
    force = np.asarray(force_tcp, dtype=float)
    torque = np.asarray(torque_tcp, dtype=float)
    rotation = np.asarray(rotation_base_from_tcp, dtype=float)
    if (
        force.shape != (3,)
        or torque.shape != (3,)
        or rotation.shape != (3, 3)
        or not np.all(np.isfinite(force))
        or not np.all(np.isfinite(torque))
        or not np.all(np.isfinite(rotation))
    ):
        raise ValueError("TCP-to-base wrench transform inputs are invalid")
    values = np.r_[rotation @ force, rotation @ torque]
    return tuple(float(value) for value in values)  # type: ignore[return-value]


def subtract_simulated_tare(
    raw_sensor: Sequence[float], initial_free_space_raw: Sequence[float]
) -> np.ndarray:
    """Apply the simulator-only initial free-space tare in raw sensor space."""

    raw = np.asarray(raw_sensor, dtype=float)
    tare = np.asarray(initial_free_space_raw, dtype=float)
    if raw.shape != (3,) or tare.shape != (3,) or not (
        np.all(np.isfinite(raw)) and np.all(np.isfinite(tare))
    ):
        raise ValueError("simulated tare inputs must be finite 3-vectors")
    return raw - tare


@dataclass(frozen=True)
class NativeContactRow:
    geom1: str
    geom2: str
    distance_m: float
    position_world_m: tuple[float, float, float]
    normal_world: tuple[float, float, float]
    contact_force_frame: tuple[float, float, float, float, float, float]


class MuJoCoVelocityPlant:
    """2 kHz velocity-servo plant with 500 Hz state/command interface."""

    def __init__(
        self,
        manifest_path: Path,
        *,
        output_key: str = "no_contact_velocity",
    ) -> None:
        import mujoco
        import pinocchio as pin

        import step5c_calibrated_kinematics_audit as kinematics

        self.mujoco = mujoco
        self.pin = pin
        self.kinematics = kinematics
        self.manifest_path = manifest_path.resolve()
        self.bundle_dir = self.manifest_path.parent
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if self.manifest.get("schema") != "ur10e_mujoco_model_bundle_v1":
            raise ValueError("MuJoCo model manifest schema is invalid")
        self.output_key = str(output_key)
        velocity = (self.manifest.get("outputs") or {}).get(self.output_key)
        if not isinstance(velocity, Mapping):
            raise ValueError(f"MuJoCo model output is missing: {self.output_key}")
        if self.output_key == "no_contact_velocity":
            no_contact_scene = self.manifest.get("no_contact_scene") or {}
            if (
                no_contact_scene.get("output_key") != self.output_key
                or no_contact_scene.get("native_contact_enabled") is not True
                or float(no_contact_scene.get("minimum_remaining_clearance_m", -1.0))
                <= 0.0
            ):
                raise ValueError("MuJoCo P0 no-contact scene binding is invalid")
        self.model_path = self.bundle_dir / velocity["path"]
        if sha256_path(self.model_path) != velocity["sha256"]:
            raise ValueError("MuJoCo velocity model hash mismatch")
        self.model = mujoco.MjModel.from_xml_path(str(self.model_path))
        self.data = mujoco.MjData(self.model)
        self.urdf_path = self.bundle_dir / self.manifest["generated_urdf"]["path"]
        if sha256_path(self.urdf_path) != self.manifest["generated_urdf"]["sha256"]:
            raise ValueError("portable calibrated URDF hash mismatch")
        urdf_text = self.urdf_path.read_text(encoding="utf-8")
        pin_model = pin.buildModelFromUrdf(str(self.urdf_path))
        self.calibrated = kinematics.CalibratedModel(
            model=pin_model,
            data=pin_model.createData(),
            urdf_text=urdf_text,
            calibration_hash=self.manifest["calibration_hash"],
            base_frame_id=pin_model.getFrameId("base"),
            tool0_frame_id=pin_model.getFrameId("tool0"),
            flange_frame_id=pin_model.getFrameId("flange"),
        )
        self.base_from_world = fixed_transform(urdf_text, "base_link", "base")[:3, :3].T
        if not np.allclose(
            self.base_from_world,
            np.asarray(self.manifest["base_from_mujoco_world_rotation"], dtype=float),
            atol=1e-12,
        ):
            raise ValueError("MuJoCo world/base frame binding mismatch")
        self.tcp_offset = np.asarray(
            self.manifest["active_tcp_offset_tool0_m"], dtype=float
        )
        self.site_id = mujoco.mj_name2id(
            self.model, mujoco.mjtObj.mjOBJ_SITE, "active_tcp_site"
        )
        self.production_wrench_site_id = mujoco.mj_name2id(
            self.model,
            mujoco.mjtObj.mjOBJ_SITE,
            "production_tcp_wrench_site",
        )
        self.force_sensor_id = mujoco.mj_name2id(
            self.model,
            mujoco.mjtObj.mjOBJ_SENSOR,
            "production_tcp_force_raw",
        )
        self.torque_sensor_id = mujoco.mj_name2id(
            self.model,
            mujoco.mjtObj.mjOBJ_SENSOR,
            "production_tcp_torque_raw",
        )
        if min(
            self.site_id,
            self.production_wrench_site_id,
            self.force_sensor_id,
            self.torque_sensor_id,
        ) < 0:
            raise ValueError("MuJoCo TCP/wrench sites or sensors are missing")
        self.cage = json.loads(TCP_CAGE_PATH.read_text(encoding="utf-8"))
        self.frame_lineage = FrameLineage(
            command_frame="base",
            pose_frame="base",
            twist_frame="base",
            wrench_frame="base",
            jacobian_frame="base",
            normal_frame="base",
            transform_chain=(
                "mujoco_world(base_link)->base",
                "base->tool0(calibrated_pinocchio)",
                "tool0->active_tcp(manifest)",
                "production_tcp_wrench_raw->simulated_initial_tare",
                "tare_compensated_raw->external_tcp(sign_flip)",
                "external_tcp->base(rotation)",
            ),
            sha256=self._frame_lineage_sha256(),
        )
        self.reset()

    def _frame_lineage_sha256(self) -> str:
        import hashlib

        payload = {
            "base_from_world": self.base_from_world.tolist(),
            "tcp_offset": self.tcp_offset.tolist(),
            "calibration_hash": self.manifest["calibration_hash"],
            "model_output_key": self.output_key,
            "model_sha256": self.manifest["outputs"][self.output_key]["sha256"],
            "wrench_mapping": self.manifest["wrench_contract"],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def reset(self) -> None:
        self.mujoco.mj_resetDataKeyframe(self.model, self.data, 0)
        self.data.ctrl[:] = 0.0
        self.mujoco.mj_forward(self.model, self.data)
        self._tare_force_raw = self._sensor(self.force_sensor_id)
        self._tare_torque_raw = self._sensor(self.torque_sensor_id)

    def _sensor(self, sensor_id: int) -> np.ndarray:
        address = int(self.model.sensor_adr[sensor_id])
        dimension = int(self.model.sensor_dim[sensor_id])
        return np.asarray(
            self.data.sensordata[address : address + dimension], dtype=float
        ).copy()

    def _tcp_pose_and_rotation(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        position_world = np.asarray(self.data.site_xpos[self.site_id], dtype=float)
        rotation_world = np.asarray(
            self.data.site_xmat[self.site_id], dtype=float
        ).reshape(3, 3)
        position_base = self.base_from_world @ position_world
        rotation_base = self.base_from_world @ rotation_world
        rotvec_base = np.asarray(self.pin.log3(rotation_base), dtype=float)
        return position_base, rotation_base, rotvec_base

    def _command_jacobian(self, q: np.ndarray) -> np.ndarray:
        # Imported lazily to preserve the exact production implementation.
        from kunwei_rtde_bridge import step5d_tcp_jacobian_base

        return step5d_tcp_jacobian_base(self.calibrated, q, self.tcp_offset)

    def _omega_bounds(self, q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        from kunwei_rtde_bridge import step5d_omega_bounds

        return step5d_omega_bounds(
            q,
            self.calibrated.model.lowerPositionLimit,
            self.calibrated.model.upperPositionLimit,
            alpha_s_inv=1.0,
            qdot_limit_rad_s=P0_V8_QDOT_CAP_RAD_S,
        )

    def _native_contacts(self) -> list[NativeContactRow]:
        rows: list[NativeContactRow] = []
        for index in range(self.data.ncon):
            contact = self.data.contact[index]
            force = np.zeros(6, dtype=float)
            self.mujoco.mj_contactForce(self.model, self.data, index, force)
            rows.append(
                NativeContactRow(
                    geom1=self.model.geom(contact.geom1).name,
                    geom2=self.model.geom(contact.geom2).name,
                    distance_m=float(contact.dist),
                    position_world_m=tuple(float(value) for value in contact.pos),  # type: ignore[arg-type]
                    normal_world=tuple(float(value) for value in contact.frame[:3]),  # type: ignore[arg-type]
                    contact_force_frame=tuple(float(value) for value in force),  # type: ignore[arg-type]
                )
            )
        return rows

    def _inside_cage(self, position_base: np.ndarray) -> bool:
        lower = np.asarray(self.cage["min_xyz"], dtype=float)
        upper = np.asarray(self.cage["max_xyz"], dtype=float)
        return bool(np.all(position_base >= lower) and np.all(position_base <= upper))

    def read_state(self, *, sequence: int, wall_time_s: float) -> SimulatorState:
        self.mujoco.mj_forward(self.model, self.data)
        q = np.asarray(self.data.qpos, dtype=float).copy()
        qd = np.asarray(self.data.qvel, dtype=float).copy()
        position_base, rotation_base, rotvec_base = self._tcp_pose_and_rotation()
        jacobian = self._command_jacobian(q)
        twist_base = jacobian @ qd
        raw_force = self._sensor(self.force_sensor_id)
        raw_torque = self._sensor(self.torque_sensor_id)
        compensated_force = subtract_simulated_tare(raw_force, self._tare_force_raw)
        compensated_torque = subtract_simulated_tare(raw_torque, self._tare_torque_raw)
        external_force_tcp, external_torque_tcp = external_wrench_at_tcp(
            compensated_force,
            compensated_torque,
            rotation_tcp_from_sensor=np.eye(3),
            sensor_to_tcp_sensor_m=np.zeros(3),
        )
        wrench_base = wrench_tcp_to_base(
            external_force_tcp,
            external_torque_tcp,
            rotation_base,
        )
        reaction = np.array((0.0, 0.0, 1.0), dtype=float)
        normal_load = max(0.0, float(np.dot(np.asarray(wrench_base[:3]), reaction)))
        target = build_p0_v8_target(
            tcp_pose_base=tuple(float(value) for value in np.r_[position_base, rotvec_base]),
            tcp_speed_base=tuple(float(value) for value in twist_base),
            force_tcp_n=tuple(float(value) for value in external_force_tcp),
            reaction_normal_b=tuple(float(value) for value in reaction),
            normal_load_n=normal_load,
            force_error_n=1.0 - normal_load,
            jacobian=jacobian,
            dt_s=0.002,
            qdot_cap_rad_s=P0_V8_QDOT_CAP_RAD_S,
        )
        lower, upper = self._omega_bounds(q)
        contacts = self._native_contacts()
        expected_pair = {"eoat_contact_pad_collision", "step5_surface_collision"}
        cage_collisions = sum(
            1
            for row in contacts
            if {row.geom1, row.geom2} != expected_pair
        )
        tcp_pose = tuple(float(value) for value in np.r_[position_base, rotvec_base])
        return SimulatorState(
            engine="mujoco",
            engine_version=str(self.mujoco.__version__),
            sequence=int(sequence),
            sim_time_s=float(self.data.time),
            wall_time_s=float(wall_time_s),
            observation_age_s=0.0,
            q=tuple(float(value) for value in q),  # type: ignore[arg-type]
            qd=tuple(float(value) for value in qd),  # type: ignore[arg-type]
            tcp_pose=tcp_pose,  # type: ignore[arg-type]
            tcp_twist=tuple(float(value) for value in twist_base),  # type: ignore[arg-type]
            wrench=wrench_base,
            command_jacobian=tuple(  # type: ignore[arg-type]
                tuple(float(value) for value in row) for row in jacobian
            ),
            desired_twist=target.desired_twist,
            reaction_normal=(0.0, 0.0, 1.0),
            approach_normal=(0.0, 0.0, -1.0),
            frame_lineage=self.frame_lineage,
            calibration_hash=str(self.manifest["calibration_hash"]),
            model_hash=str(self.manifest["outputs"][self.output_key]["sha256"]),
            omega_minus=tuple(float(value) for value in lower),  # type: ignore[arg-type]
            omega_plus=tuple(float(value) for value in upper),  # type: ignore[arg-type]
            path_time_s=float(self.data.time),
            force_error_n=1.0 - normal_load,
            orientation_error_rad=float(
                target.outer_diagnostics["outer_orientation_angle_rad"]
            ),
            native_contact_count=len(contacts),
            cage_collision_count=cage_collisions,
            tcp_inside_cage=self._inside_cage(position_base),
            metadata={
                "normal_load_n": normal_load,
                "raw_parent_to_child_force_sensor": raw_force.tolist(),
                "raw_parent_to_child_torque_sensor": raw_torque.tolist(),
                "simulated_tare_force_raw": self._tare_force_raw.tolist(),
                "simulated_tare_torque_raw": self._tare_torque_raw.tolist(),
                "tare_compensated_force_raw": compensated_force.tolist(),
                "tare_compensated_torque_raw": compensated_torque.tolist(),
                "simulated_tare_is_not_bench_zero_ft": True,
                "external_force_tcp": external_force_tcp.tolist(),
                "external_torque_tcp": external_torque_tcp.tolist(),
                "target": {
                    "effective_ko": target.posture_policy["effective_ko"],
                    "limiter_active": target.limiter_active,
                    "feasibility_scale": target.feasibility_diagnostics[
                        "xdot_feasibility_scale"
                    ],
                },
                "contacts": [row.__dict__ for row in contacts],
                "wrench_contract": self.manifest["wrench_contract"],
            },
        )

    def write_command(self, command: SimulationCommand) -> None:
        if command.mode != "joint_velocity" or len(command.qdot) != 6:
            raise ValueError("MuJoCo velocity plant requires a six-joint velocity command")
        values = np.asarray(command.qdot, dtype=float)
        if not np.all(np.isfinite(values)) or np.max(np.abs(values)) > P0_V8_QDOT_CAP_RAD_S + 1e-12:
            raise ValueError("MuJoCo velocity command is nonfinite or over qdot cap")
        self.data.ctrl[:] = values
        # MuJoCo's native nstep overload executes the same four 0.5 ms physics
        # substeps while avoiding four separate Python/C crossings in every
        # 500 Hz control tick.
        self.mujoco.mj_step(self.model, self.data, nstep=4)

    def contact_rows(self) -> list[NativeContactRow]:
        return self._native_contacts()
