#!/usr/bin/env python3
"""Verify calibrated Pinocchio vs MuJoCo FK/Jacobian and model provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Mapping

import numpy as np


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
TOOLS = EXPERIMENT_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from build_ur10e_digital_twin_model import (  # noqa: E402
    JOINT_NAMES,
    _load_json,
    sha256_path,
)


BASE_FROM_MUJOCO_WORLD = np.diag((-1.0, -1.0, 1.0))
FK_POSITION_TOLERANCE_M = 2e-6
FK_ROTATION_TOLERANCE_RAD = 2e-6
JACOBIAN_MAX_ABS_TOLERANCE = 2e-6
DIRECTION_COSINE_MIN = 0.99999


def rotation_angle(rotation: np.ndarray) -> float:
    cosine = float((np.trace(rotation) - 1.0) * 0.5)
    return math.acos(max(-1.0, min(1.0, cosine)))


def mujoco_site_pose_in_command_frame(data: Any, site_id: int) -> tuple[np.ndarray, np.ndarray]:
    position_world = np.asarray(data.site_xpos[site_id], dtype=float)
    rotation_world = np.asarray(data.site_xmat[site_id], dtype=float).reshape(3, 3)
    return (
        BASE_FROM_MUJOCO_WORLD @ position_world,
        BASE_FROM_MUJOCO_WORLD @ rotation_world,
    )


def mujoco_site_jacobian_in_command_frame(model: Any, data: Any, site_id: int) -> np.ndarray:
    import mujoco

    linear = np.empty((3, model.nv), dtype=float)
    angular = np.empty((3, model.nv), dtype=float)
    mujoco.mj_jacSite(model, data, linear, angular, site_id)
    return np.vstack(
        (BASE_FROM_MUJOCO_WORLD @ linear, BASE_FROM_MUJOCO_WORLD @ angular)
    )


def _verify_binding(item: Mapping[str, object], *, bundle_dir: Path) -> str | None:
    raw_path = str(item.get("path") or "")
    if not raw_path:
        return "binding_path_missing"
    path = Path(raw_path)
    if not path.is_absolute():
        path = bundle_dir / path
    if not path.is_file():
        return f"binding_missing:{raw_path}"
    if int(item.get("size_bytes", -1)) != path.stat().st_size:
        return f"binding_size_mismatch:{raw_path}"
    if str(item.get("sha256") or "") != sha256_path(path):
        return f"binding_sha256_mismatch:{raw_path}"
    return None


def verify(
    *,
    manifest_path: Path,
    samples: int,
    seed: int,
) -> dict[str, Any]:
    import mujoco

    import step5c_calibrated_kinematics_audit as kinematics
    from kunwei_rtde_bridge import step5d_tcp_jacobian_base

    manifest = _load_json(manifest_path)
    bundle_dir = manifest_path.parent
    blockers: list[str] = []
    if manifest.get("schema") != "ur10e_mujoco_model_bundle_v1":
        blockers.append("manifest_schema_invalid")
    for collection in ("source_bindings", "vendored_assets"):
        for item in manifest.get(collection) or []:
            issue = _verify_binding(item, bundle_dir=bundle_dir)
            if issue:
                blockers.append(issue)
    generated_urdf = manifest.get("generated_urdf") or {}
    issue = _verify_binding(generated_urdf, bundle_dir=bundle_dir)
    if issue:
        blockers.append(issue)

    velocity_binding = (manifest.get("outputs") or {}).get("velocity") or {}
    issue = _verify_binding(velocity_binding, bundle_dir=bundle_dir)
    if issue:
        blockers.append(issue)
    model_path = Path(str(velocity_binding.get("path") or ""))
    if not model_path.is_absolute():
        model_path = bundle_dir / model_path
    model_text = model_path.read_text(encoding="utf-8") if model_path.is_file() else ""
    for token in ("tcp_site_unverified_85mm", "eoat_v13_tcp_guess", "ur10e_nominal.xml"):
        if token in model_text:
            blockers.append(f"legacy_token_present:{token}")
    legacy_hash = "f186a335c39d74979b62ea32eacbe15a8a893baab110ae3fa2bdd5db07f065d5"
    if model_path.is_file() and sha256_path(model_path) == legacy_hash:
        blockers.append("legacy_mjcf_hash_forbidden")

    model = mujoco.MjModel.from_xml_path(str(model_path))
    data = mujoco.MjData(model)
    if tuple(model.joint(index).name for index in range(model.njnt)) != JOINT_NAMES:
        blockers.append("joint_order_mismatch")
    if not math.isclose(float(model.opt.timestep), 0.0005, abs_tol=1e-12):
        blockers.append("physics_timestep_not_2khz")
    if model.nu != 6 or any(
        not model.actuator(index).name.startswith("velocity_") for index in range(model.nu)
    ):
        blockers.append("velocity_actuator_lane_invalid")

    site_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, "active_tcp_site")
    if site_id < 0:
        raise ValueError("active_tcp_site is missing")
    calibrated = kinematics.build_calibrated_model()
    tcp_offset = np.asarray(manifest["active_tcp_offset_tool0_m"], dtype=float)
    initial_q = np.asarray(model.key_qpos[0], dtype=float)
    rng = np.random.default_rng(seed)
    position_errors: list[float] = []
    rotation_errors: list[float] = []
    jacobian_errors: list[float] = []
    direction_cosines: list[float] = []
    initial_contact_rows: list[dict[str, object]] = []

    data.qpos[:] = initial_q
    mujoco.mj_forward(model, data)
    for index in range(data.ncon):
        contact = data.contact[index]
        initial_contact_rows.append(
            {
                "geom1": model.geom(contact.geom1).name,
                "geom2": model.geom(contact.geom2).name,
                "distance_m": float(contact.dist),
            }
        )
    if initial_contact_rows:
        blockers.append("initial_no_contact_state_has_contacts")

    for _ in range(int(samples)):
        q = initial_q + rng.uniform(-0.35, 0.35, size=6)
        data.qpos[:] = q
        mujoco.mj_forward(model, data)
        mj_position, mj_rotation = mujoco_site_pose_in_command_frame(data, site_id)
        tool0 = kinematics.base_to_tool0(calibrated, q)
        pin_position = tool0.translation + tool0.rotation @ tcp_offset
        pin_rotation = tool0.rotation
        position_errors.append(float(np.linalg.norm(mj_position - pin_position)))
        rotation_errors.append(rotation_angle(pin_rotation.T @ mj_rotation))
        mj_jacobian = mujoco_site_jacobian_in_command_frame(model, data, site_id)
        pin_jacobian = step5d_tcp_jacobian_base(calibrated, q, tcp_offset)
        jacobian_errors.append(float(np.max(np.abs(mj_jacobian - pin_jacobian))))

    q = initial_q.copy()
    data.qpos[:] = q
    mujoco.mj_forward(model, data)
    base_position, _ = mujoco_site_pose_in_command_frame(data, site_id)
    command_jacobian = step5d_tcp_jacobian_base(calibrated, q, tcp_offset)
    delta = 1e-6
    for joint_index in range(6):
        for sign in (-1.0, 1.0):
            perturbed = q.copy()
            perturbed[joint_index] += sign * delta
            data.qpos[:] = perturbed
            mujoco.mj_forward(model, data)
            position, _ = mujoco_site_pose_in_command_frame(data, site_id)
            observed = (position - base_position) / (sign * delta)
            expected = command_jacobian[:3, joint_index]
            observed_norm = float(np.linalg.norm(observed))
            expected_norm = float(np.linalg.norm(expected))
            if observed_norm < 1e-12 and expected_norm < 1e-12:
                cosine = 1.0
            else:
                cosine = float(np.dot(observed, expected) / (observed_norm * expected_norm))
            direction_cosines.append(cosine)

    max_position = max(position_errors, default=math.inf)
    max_rotation = max(rotation_errors, default=math.inf)
    max_jacobian = max(jacobian_errors, default=math.inf)
    min_direction_cosine = min(direction_cosines, default=-1.0)
    if max_position > FK_POSITION_TOLERANCE_M:
        blockers.append("fk_position_tolerance_exceeded")
    if max_rotation > FK_ROTATION_TOLERANCE_RAD:
        blockers.append("fk_rotation_tolerance_exceeded")
    if max_jacobian > JACOBIAN_MAX_ABS_TOLERANCE:
        blockers.append("jacobian_tolerance_exceeded")
    if min_direction_cosine < DIRECTION_COSINE_MIN:
        blockers.append("joint_command_direction_mismatch")

    return {
        "schema": "ur10e_mujoco_model_verification_v1",
        "manifest": str(manifest_path),
        "manifest_sha256": sha256_path(manifest_path),
        "samples": int(samples),
        "seed": int(seed),
        "command_frame": "base",
        "mujoco_world_frame": "base_link",
        "base_from_mujoco_world_rotation": BASE_FROM_MUJOCO_WORLD.tolist(),
        "command_jacobian_source": "calibrated_pinocchio",
        "mujoco_jacobian_role": "independent_oracle_only",
        "metrics": {
            "fk_position_max_m": max_position,
            "fk_rotation_max_rad": max_rotation,
            "jacobian_max_abs": max_jacobian,
            "joint_direction_min_cosine": min_direction_cosine,
            "initial_contact_count": len(initial_contact_rows),
        },
        "thresholds": {
            "fk_position_max_m": FK_POSITION_TOLERANCE_M,
            "fk_rotation_max_rad": FK_ROTATION_TOLERANCE_RAD,
            "jacobian_max_abs": JACOBIAN_MAX_ABS_TOLERANCE,
            "joint_direction_min_cosine": DIRECTION_COSINE_MIN,
        },
        "initial_contacts": initial_contact_rows,
        "pass": not blockers,
        "blockers": sorted(set(blockers)),
        "claim_boundary": {
            "physics_provenance": "geometry_provisional",
            "p0_sim_physics_pass": False,
            "package_accepted": False,
            "live_accepted": False,
            "reproduction_complete": False,
        },
        "safety_boundary": [
            "offline model oracle only",
            "no controller upload",
            "no bridge start",
            "no robot motion",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(
        manifest_path=args.manifest.resolve(),
        samples=args.samples,
        seed=args.seed,
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if result["pass"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
