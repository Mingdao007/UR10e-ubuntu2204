#!/usr/bin/env python3
"""Offline URDF operational-inertia estimate at a saved UR10e read-back.

This script reads only a local RTDE/read-back JSON file. It never opens a
controller or sensor endpoint. It reports two distinct task assumptions:
other Cartesian axes free, and tangential/orientation accelerations constrained.
The payload is added from the controller's mass, CoG, and inertia read-back.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from contact_yield_kinematics import TCP_OFFSET_TOOL0
from step5c_calibrated_kinematics_audit import (
    DEFAULT_CALIBRATION_YAML,
    DEFAULT_XACRO_PATH,
    build_calibrated_model,
)


SCHEMA = "tase.normal-axis-operational-inertia-model-candidate-v2"
NORMAL_TASK_DIRECTION = (0.0, 0.0, 1.0, 0.0, 0.0, 0.0)


class EffectiveMassError(ValueError):
    """The saved read-back or model input cannot support this calculation."""


def _finite_vector(value: Any, size: int, name: str) -> np.ndarray:
    try:
        result = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EffectiveMassError(f"{name} must be a finite length-{size} vector") from exc
    if result.shape != (size,) or not np.isfinite(result).all():
        raise EffectiveMassError(f"{name} must be a finite length-{size} vector")
    return result


def _finite_matrix(value: Any, rows: int, cols: int, name: str) -> np.ndarray:
    try:
        result = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EffectiveMassError(f"{name} must be a finite {rows}x{cols} matrix") from exc
    if result.shape != (rows, cols) or not np.isfinite(result).all():
        raise EffectiveMassError(f"{name} must be a finite {rows}x{cols} matrix")
    return result


def _skew(vector: np.ndarray) -> np.ndarray:
    x, y, z = vector
    return np.array(((0.0, -z, y), (z, 0.0, -x), (-y, x, 0.0)))


def normal_effective_mass_metrics(
    mass_matrix: Any,
    tcp_jacobian: Any,
    direction: Any = NORMAL_TASK_DIRECTION,
) -> dict[str, Any]:
    """Compute free-task and locked-task operational mass for one axis.

    The first assumes a pure normal wrench while other task directions may
    accelerate. The second imposes a pure normal task acceleration by solving
    ``J qdd = direction``. They are different physical constraints, not an
    uncertainty interval.
    """
    mass = _finite_matrix(mass_matrix, 6, 6, "mass_matrix")
    jacobian = _finite_matrix(tcp_jacobian, 6, 6, "tcp_jacobian")
    axis = _finite_vector(direction, 6, "direction")
    if not np.allclose(mass, mass.T, rtol=0.0, atol=1e-9):
        raise EffectiveMassError("mass_matrix must be symmetric")
    if float(np.linalg.eigvalsh(mass).min()) <= 0.0:
        raise EffectiveMassError("mass_matrix must be positive definite")
    singular_values = np.linalg.svd(jacobian, compute_uv=False)
    if singular_values[-1] <= 1e-10:
        raise EffectiveMassError("tcp_jacobian is rank deficient")

    inverse_operational_inertia = jacobian @ np.linalg.solve(mass, jacobian.T)
    normal_compliance = float(axis @ inverse_operational_inertia @ axis)
    if not math.isfinite(normal_compliance) or normal_compliance <= 0.0:
        raise EffectiveMassError("normal operational compliance is not positive")

    joint_acceleration = np.linalg.solve(jacobian, axis)
    constrained_mass = float(joint_acceleration @ mass @ joint_acceleration)
    free_mass = 1.0 / normal_compliance
    if not math.isfinite(constrained_mass) or constrained_mass <= 0.0:
        raise EffectiveMassError("constrained operational mass is not positive")
    return {
        "effective_mass_normal_free_other_task_axes_kg": free_mass,
        "effective_mass_normal_with_tangent_and_orientation_locked_kg": constrained_mass,
        "normal_compliance_free_other_task_axes_kg_inv": normal_compliance,
        "unit_normal_task_acceleration_joint_accelerations": joint_acceleration.tolist(),
        "tcp_jacobian_singular_values": singular_values.tolist(),
        "tcp_jacobian_condition_number": float(singular_values[0] / singular_values[-1]),
        "mass_matrix_min_eigenvalue_kg": float(np.linalg.eigvalsh(mass).min()),
        "operational_inertia_condition_number": float(np.linalg.cond(inverse_operational_inertia)),
    }


def _payload_inertia_matrix(values: Any) -> np.ndarray:
    inertia = _finite_vector(values, 6, "payload_inertia")
    matrix = np.array(
        ((inertia[0], inertia[3], inertia[4]),
         (inertia[3], inertia[1], inertia[5]),
         (inertia[4], inertia[5], inertia[2])),
        dtype=float,
    )
    if float(np.linalg.eigvalsh(matrix).min()) <= 0.0:
        raise EffectiveMassError("payload inertia must be positive definite")
    return matrix


def analyze_readback(readback_path: Path | str, home_path: Path | str) -> dict[str, Any]:
    read_path, home_config_path = Path(readback_path), Path(home_path)
    try:
        readback = json.loads(read_path.read_text(encoding="utf-8"))
        home = json.loads(home_config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EffectiveMassError(f"cannot read local read-back/Home JSON: {exc}") from exc
    rtde = readback.get("rtde")
    if not isinstance(rtde, dict):
        raise EffectiveMassError("read-back has no RTDE object")
    q = _finite_vector(rtde.get("actual_q"), 6, "actual_q")
    payload = float(rtde.get("payload"))
    if not math.isfinite(payload) or payload <= 0.0:
        raise EffectiveMassError("payload mass must be finite and positive")
    cog = _finite_vector(rtde.get("payload_cog"), 3, "payload_cog")
    payload_inertia_tool = _payload_inertia_matrix(rtde.get("payload_inertia"))
    normal = np.array(NORMAL_TASK_DIRECTION, dtype=float)

    bundle = build_calibrated_model()
    model, data = bundle.model, bundle.data
    import pinocchio as pin

    pin.crba(model, data, q)
    robot_mass = np.asarray(data.M, dtype=float).copy()
    robot_mass = 0.5 * (robot_mass + robot_mass.T)

    pin.forwardKinematics(model, data, q)
    pin.computeJointJacobians(model, data, q)
    pin.updateFramePlacements(model, data)
    base_rotation = data.oMf[bundle.base_frame_id].rotation.T
    tool0_world = pin.getFrameJacobian(
        model, data, bundle.tool0_frame_id, pin.ReferenceFrame.LOCAL_WORLD_ALIGNED
    )
    tool0_base = np.vstack((base_rotation @ tool0_world[:3], base_rotation @ tool0_world[3:]))
    tool_transform = data.oMf[bundle.base_frame_id].inverse() * data.oMf[bundle.tool0_frame_id]
    tcp_lever_base = tool_transform.rotation @ TCP_OFFSET_TOOL0
    tcp_jacobian = np.vstack(
        (tool0_base[:3] - _skew(tcp_lever_base) @ tool0_base[3:], tool0_base[3:])
    )

    flange_world = pin.getFrameJacobian(
        model, data, bundle.flange_frame_id, pin.ReferenceFrame.LOCAL_WORLD_ALIGNED
    )
    flange_to_cog_world = data.oMf[bundle.flange_frame_id].rotation @ cog
    cog_linear_jacobian = base_rotation @ (
        flange_world[:3] - _skew(flange_to_cog_world) @ flange_world[3:]
    )
    flange_rotation_base = base_rotation @ data.oMf[bundle.flange_frame_id].rotation
    payload_inertia_base = (
        flange_rotation_base @ payload_inertia_tool @ flange_rotation_base.T
    )
    flange_angular_jacobian = base_rotation @ flange_world[3:]
    payload_mass_matrix = (
        payload * (cog_linear_jacobian.T @ cog_linear_jacobian)
        + flange_angular_jacobian.T @ payload_inertia_base @ flange_angular_jacobian
    )
    total_mass = robot_mass + payload_mass_matrix
    approved_q = _finite_vector(home.get("joint_positions_rad"), 6, "joint_positions_rad")
    urdf_text_hash = hashlib.sha256(bundle.urdf_text.encode("utf-8")).hexdigest()
    calibration_file_hash = hashlib.sha256(Path(DEFAULT_CALIBRATION_YAML).read_bytes()).hexdigest()
    xacro_file_hash = hashlib.sha256(Path(DEFAULT_XACRO_PATH).read_bytes()).hexdigest()

    return {
        "schema": SCHEMA,
        "claim_scope": "offline URDF operational inertia at the saved Home read-back; not empirical contact identification, not live qualification",
        "hardware_or_endpoint_action": False,
        "observed_at": readback.get("observed_at_local"),
        "readback_path": str(read_path),
        "home_profile_id": home.get("home_profile_id"),
        "approved_home_q_rad": approved_q.tolist(),
        "actual_q_rad": q.tolist(),
        "actual_q_max_error_from_approved_home_rad": float(np.max(np.abs(q - approved_q))),
        "normal_direction_base": [0.0, 0.0, 1.0],
        "payload_readback": {
            "mass_kg": payload,
            "cog_m_from_tool_flange": cog.tolist(),
            "inertia_tensor_at_cog_tool_flange_kg_m2": payload_inertia_tool.tolist(),
        },
        "model_identity": {
            "calibration_hash": bundle.calibration_hash,
            "expanded_urdf_sha256": urdf_text_hash,
            "calibration_yaml_sha256": calibration_file_hash,
            "xacro_sha256": xacro_file_hash,
        },
        "task_jacobian_singular_values": np.linalg.svd(tcp_jacobian, compute_uv=False).tolist(),
        "tcp_jacobian_base": tcp_jacobian.tolist(),
        "urdf_arm_mass_matrix_kg": robot_mass.tolist(),
        "payload_mass_matrix_contribution_kg": payload_mass_matrix.tolist(),
        "loaded_mass_matrix_kg": total_mass.tolist(),
        "robot_urdf_only": normal_effective_mass_metrics(robot_mass, tcp_jacobian, normal),
        "robot_plus_readback_payload": normal_effective_mass_metrics(total_mass, tcp_jacobian, normal),
        "formula_notes": {
            "free_other_task_axes": "m=1/(u^T J M_q^-1 J^T u)",
            "tangent_and_orientation_locked": "qdd=solve(J,u); m=qdd^T M_q qdd",
            "controller_outer_Md_is_not_estimated_here": 9.565272137974492,
        },
        "limitations": [
            "URDF arm inertias are model values, not empirically identified robot dynamics",
            "no contact stiffness, contact damping, force-loop state, servo compliance, or friction is included",
            "free-axis and locked-task values represent different constraint assumptions and are not a confidence interval",
            "fixed Home configuration only; no motion or contact excitation was executed",
            "KWR75 remains feedback, not independent task-force truth",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readback", type=Path, required=True, help="saved local RTDE read-back JSON; no endpoint access")
    parser.add_argument("--home-config", type=Path, default=Path(__file__).resolve().parents[1] / "config" / "figure8_home_v1.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = analyze_readback(args.readback, args.home_config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as target:
        json.dump(result, target, indent=2, sort_keys=True, allow_nan=False)
        target.write("\n")
    print(json.dumps({
        "output": str(args.output),
        "robot_urdf_only": result["robot_urdf_only"],
        "robot_plus_readback_payload": result["robot_plus_readback_payload"],
    }, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
