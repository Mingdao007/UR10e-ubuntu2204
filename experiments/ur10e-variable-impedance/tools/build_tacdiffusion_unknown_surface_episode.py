#!/usr/bin/env python3
"""Build a hash-bound constant-Z unknown-surface nominal episode reference."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ur10e_vic.tacdiffusion.trajectory import TRAJECTORY_FAMILIES, TrajectoryProfile
from ur10e_vic.tacdiffusion.unknown_surface_episode import (
    generate_unknown_surface_references,
    load_unknown_surface_tube,
    rotation_vector_distance_rad,
)


DEFAULT_SURFACE = ROOT / "config" / "tacdiffusion_surface_input_20260726.json"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"unknown-surface reference already exists: {path}")
    data = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def build(args: argparse.Namespace) -> dict[str, object]:
    profile = TrajectoryProfile(
        speed_scale=args.speed_scale,
        normal_force_target_n=args.target_load_n,
        preload_n=args.preload_n,
    )
    tube = load_unknown_surface_tube(
        args.surface_manifest,
        anchor_pose_base=args.anchor_pose,
        normal_half_width_m=args.tube_normal_half_width_m,
    )
    trajectory, references = generate_unknown_surface_references(
        tube,
        family=args.family,
        seed=args.seed,
        duration_s=args.path_duration_s,
        capture_duration_s=args.capture_duration_s,
        canary_radius_m=args.canary_radius_m,
        rate_hz=500,
        profile=profile,
    )
    first_pose = tuple(float(value) for value in references[0]["desired_pose_base"])
    initial_translation_error_m = math.dist(first_pose[:3], tuple(args.anchor_pose[:3]))
    initial_orientation_error_rad = rotation_vector_distance_rad(
        first_pose[3:],
        tuple(args.anchor_pose[3:]),
    )
    if initial_translation_error_m > 0.002 + 1e-12:
        raise ValueError("first reference exceeds the 2 mm release pose gate")
    if initial_orientation_error_rad > 1e-6:
        raise ValueError("first reference orientation differs from the episode anchor")
    unsigned: dict[str, object] = {
        "schema": "ur10e_tacdiffusion_unknown_surface_episode_artifact/v1",
        "claim_class": "offline_reference_only_no_live_authorization",
        "surface_manifest": str(Path(args.surface_manifest).resolve()),
        "surface_manifest_sha256": tube.surface_manifest_sha256,
        "height_source": "fresh_episode_anchor_constant_z",
        "normal_source": "nominal_task_loading_axis_base_positive_z",
        "cad_height_or_normal_feedforward": False,
        "anchor_pose_base": list(args.anchor_pose),
        "tube": {
            "center_base_m": list(tube.center_base_m),
            "u_axis_base": list(tube.u_axis_base),
            "v_axis_base": list(tube.v_axis_base),
            "safe_u_half_width_m": tube.safe_u_half_width_m,
            "safe_v_half_width_m": tube.safe_v_half_width_m,
            "normal_half_width_m": tube.normal_half_width_m,
            "orientation_tolerance_rad": tube.orientation_tolerance_rad,
        },
        "trajectory": {
            "family": trajectory.family,
            "seed": trajectory.seed,
            "path_duration_s": trajectory.duration_s,
            "capture_duration_s": args.capture_duration_s,
            "rate_hz": trajectory.rate_hz,
            "row_count": len(references),
            "max_speed_m_s": trajectory.max_speed_m_s,
            "max_acceleration_m_s2": trajectory.max_acceleration_m_s2,
            "max_curvature_m_inv": trajectory.max_curvature_m_inv,
            "speed_scale": profile.speed_scale,
            "target_load_n": profile.normal_force_target_n,
            "preload_n": profile.preload_n,
            "initial_translation_error_m": initial_translation_error_m,
            "initial_orientation_error_rad": initial_orientation_error_rad,
            "initial_release_pose_ready": True,
        },
        "references": list(references),
    }
    canonical = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    payload = {**unsigned, "content_sha256": _sha256_bytes(canonical)}
    _write_atomic(args.output, payload)
    return {
        "ok": True,
        "output": str(args.output.resolve()),
        "artifact_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
        "content_sha256": payload["content_sha256"],
        "row_count": len(references),
        "tube": payload["tube"],
        "trajectory": payload["trajectory"],
        "live_actions": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface-manifest", type=Path, default=DEFAULT_SURFACE)
    parser.add_argument("--anchor-pose", nargs=6, type=float, required=True)
    parser.add_argument("--tube-normal-half-width-m", type=float, required=True)
    parser.add_argument("--family", choices=(*TRAJECTORY_FAMILIES, "anchor_circle"), required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--path-duration-s", type=float, required=True)
    parser.add_argument("--capture-duration-s", type=float, required=True)
    parser.add_argument("--canary-radius-m", type=float)
    parser.add_argument("--speed-scale", type=float, required=True)
    parser.add_argument("--target-load-n", type=float, required=True)
    parser.add_argument("--preload-n", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = build(args)
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
