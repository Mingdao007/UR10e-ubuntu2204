#!/usr/bin/env python3
"""Render fixed MuJoCo observer views with an explicit non-promoting review."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


CAMERAS = ("wide", "oblique", "close", "contact")


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_review(payload: Mapping[str, object], root: Path) -> list[str]:
    blockers: list[str] = []
    if payload.get("schema") != "ur10e_mujoco_observer_review_v1":
        blockers.append("schema_invalid")
    views = payload.get("views")
    if not isinstance(views, list) or {row.get("camera") for row in views if isinstance(row, dict)} != set(CAMERAS):
        blockers.append("camera_set_invalid")
        views = []
    for row in views:
        if not isinstance(row, dict):
            blockers.append("view_row_invalid")
            continue
        path = root / str(row.get("path") or "")
        if not path.is_file():
            blockers.append(f"view_missing:{path.name}")
        elif path.stat().st_size != row.get("size_bytes"):
            blockers.append(f"view_size_mismatch:{path.name}")
        elif sha256_path(path) != row.get("sha256"):
            blockers.append(f"view_sha256_mismatch:{path.name}")
    boundary = payload.get("claim_boundary") or {}
    if any(
        boundary.get(field) is not False
        for field in ("visual_acceptance_pass", "contact_sim_pass", "live_accepted", "reproduction_complete")
    ):
        blockers.append("claim_boundary_invalid")
    if payload.get("overall_pass") is not False:
        blockers.append("provisional_scene_must_not_pass_visual_acceptance")
    declared = payload.get("blockers")
    if not isinstance(declared, list) or not declared:
        blockers.append("visual_blockers_missing")
    alignment = payload.get("measured_tcp_surface_xy_error_m")
    if (
        not isinstance(alignment, (int, float))
        or isinstance(alignment, bool)
        or not math.isfinite(float(alignment))
        or float(alignment) > 5e-6
    ):
        blockers.append("tcp_surface_model_xy_alignment_invalid")
    return sorted(set(blockers))


def render(manifest_path: Path, output_dir: Path) -> dict[str, object]:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite observer output: {output_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output_binding = (manifest.get("outputs") or {}).get("contact_velocity")
    if not isinstance(output_binding, dict):
        raise ValueError("model manifest lacks outputs.contact_velocity")
    model_path = manifest_path.parent / str(output_binding.get("path") or "")
    if not model_path.is_file() or sha256_path(model_path) != output_binding.get("sha256"):
        raise ValueError("contact velocity model binding is invalid")

    os.environ.setdefault("MUJOCO_GL", "egl")
    import mujoco
    import numpy as np
    from PIL import Image

    model = mujoco.MjModel.from_xml_path(str(model_path))
    data = mujoco.MjData(model)
    mujoco.mj_resetDataKeyframe(model, data, 0)
    mujoco.mj_forward(model, data)
    model.vis.headlight.ambient[:] = (0.12, 0.12, 0.12)
    model.vis.headlight.diffuse[:] = (0.42, 0.42, 0.42)
    model.vis.headlight.specular[:] = (0.05, 0.05, 0.05)
    if model.nlight:
        model.light_ambient[:] = np.minimum(model.light_ambient, 0.08)
        model.light_diffuse[:] *= 0.35
        model.light_specular[:] *= 0.20
    tcp = np.asarray(data.site_xpos[model.site("active_tcp_site").id], dtype=float)
    surface = np.asarray(data.xpos[model.body("step5_surface").id], dtype=float)
    probe = np.asarray(
        data.geom_xpos[model.geom("eoat_contact_probe_collision").id], dtype=float
    )
    contact_focus = 0.55 * probe + 0.45 * surface
    view_specs = {
        "wide": {
            "lookat": np.asarray((-0.22, -0.05, 0.28)),
            "distance": 1.55,
            "azimuth": 135.0,
            "elevation": -24.0,
        },
        "oblique": {
            "lookat": np.asarray((-0.26, -0.06, 0.24)),
            "distance": 1.15,
            "azimuth": -55.0,
            "elevation": -22.0,
        },
        "close": {
            "lookat": 0.65 * tcp + 0.35 * surface + np.asarray((0.0, 0.0, 0.05)),
            "distance": 0.60,
            "azimuth": 135.0,
            "elevation": -20.0,
        },
        "contact": {
            "lookat": contact_focus,
            "distance": 0.36,
            "azimuth": 105.0,
            "elevation": -16.0,
        },
    }
    output_dir.mkdir(parents=True)
    renderer = mujoco.Renderer(model, height=720, width=1280)
    scene_option = mujoco.MjvOption()
    mujoco.mjv_defaultOption(scene_option)
    scene_option.flags[mujoco.mjtVisFlag.mjVIS_CONTACTPOINT] = True
    scene_option.flags[mujoco.mjtVisFlag.mjVIS_CONTACTFORCE] = True
    views: list[dict[str, object]] = []
    try:
        for camera in CAMERAS:
            camera_name = f"observer_{camera}_dynamic"
            spec = view_specs[camera]
            observer = mujoco.MjvCamera()
            mujoco.mjv_defaultCamera(observer)
            observer.type = mujoco.mjtCamera.mjCAMERA_FREE
            observer.lookat[:] = spec["lookat"]
            observer.distance = float(spec["distance"])
            observer.azimuth = float(spec["azimuth"])
            observer.elevation = float(spec["elevation"])
            renderer.update_scene(data, camera=observer, scene_option=scene_option)
            path = output_dir / f"{camera}.png"
            Image.fromarray(renderer.render()).save(path)
            views.append(
                {
                    "camera": camera,
                    "model_camera": camera_name,
                    "path": path.name,
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_path(path),
                    "resolution": [1280, 720],
                }
            )
    finally:
        renderer.close()

    contacts = [
        {
            "geom1": model.geom(data.contact[index].geom1).name,
            "geom2": model.geom(data.contact[index].geom2).name,
            "distance_m": float(data.contact[index].dist),
        }
        for index in range(data.ncon)
    ]
    blockers = [
        "current_kunwei_stack_full_cad_missing",
        "eoat_mass_cog_inertia_unvalidated",
        "bench_fixture_geometry_missing",
        "native_contact_uses_provisional_probe_collision_not_current_cad",
        "provisional_collision_primitives_are_not_visual_acceptance_geometry",
    ]
    review: dict[str, object] = {
        "schema": "ur10e_mujoco_observer_review_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_manifest": str(manifest_path.resolve()),
        "model_manifest_sha256": sha256_path(manifest_path),
        "model_output_key": "contact_velocity",
        "model_sha256": output_binding["sha256"],
        "native_contact_count": int(data.ncon),
        "native_contacts": contacts,
        "measured_active_tcp_xyz_m": tcp.tolist(),
        "measured_surface_body_xyz_m": surface.tolist(),
        "measured_tcp_surface_xy_error_m": float(np.linalg.norm(tcp[:2] - surface[:2])),
        "views": views,
        "checks": {
            "robot_identifiable": True,
            "surface_identifiable": True,
            "current_eoat_identifiable": False,
            "kunwei_sensor_stack_identifiable": False,
            "bench_fixture_identifiable": False,
            "contact_relationship_identifiable": False,
            "visual_and_physics_evidence_separated": True,
        },
        "overall_pass": False,
        "blockers": blockers,
        "claim_boundary": {
            "visual_acceptance_pass": False,
            "contact_sim_pass": False,
            "live_accepted": False,
            "reproduction_complete": False,
            "render_cannot_promote_control_or_contact_claim": True,
        },
        "safety_boundary": [
            "offline EGL rendering only",
            "no controller upload",
            "no bridge start",
            "no robot motion",
        ],
    }
    path = output_dir / "observer_review.json"
    path.write_text(json.dumps(review, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return review


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        review_path = args.output_dir.resolve() / "observer_review.json"
        payload = json.loads(review_path.read_text(encoding="utf-8"))
        blockers = validate_review(payload, review_path.parent)
        print(json.dumps({"valid": not blockers, "blockers": blockers}, indent=2))
        return 0 if not blockers else 3
    if args.model_manifest is None:
        parser.error("--model-manifest is required unless --verify is used")
    print(
        json.dumps(
            render(args.model_manifest.resolve(), args.output_dir.resolve()),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
