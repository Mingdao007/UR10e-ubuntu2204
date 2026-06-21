#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[3]
SRC_PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
INSTALL_PACKAGE = WORKSPACE / "install" / "ur10e_example_controllers" / "share" / "ur10e_example_controllers"
if str(SRC_PACKAGE) not in sys.path:
    sys.path.insert(0, str(SRC_PACKAGE))

from ur10e_example_controllers import ur10e_gazebo_matrix_runner as gazebo  # noqa: E402


EOAT_REPO_MESH = SRC_PACKAGE / "meshes" / "eoat" / "ur5e_ksm8n_ball_transfer_tool_v13_assembly.stl"
EOAT_INSTALL_MESH = INSTALL_PACKAGE / "meshes" / "eoat" / "ur5e_ksm8n_ball_transfer_tool_v13_assembly.stl"
EOAT_XWECHAT_CANDIDATE = Path(
    "/home/andy/Documents/xwechat_files/wxid_yn52rrzphgdv21_b48e/msg/file/2026-05/"
    "ur5e_ksm8n_ball_transfer_tool_v11_receiver_fitcheck.stl"
)
SURFACE_REPO_MESH = (
    SRC_PACKAGE / "meshes" / "contact_surface" / "two_piece_surface_smooth_v11_3mm_thick.stl"
)
SURFACE_INSTALL_MESH = (
    INSTALL_PACKAGE / "meshes" / "contact_surface" / "two_piece_surface_smooth_v11_3mm_thick.stl"
)
SURFACE_COUPON_REPO_MESH = SRC_PACKAGE / "meshes" / "contact_surface" / "coupon_v11_smooth_seam_30x30_3mm.stl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stl_bbox_mm(path: Path) -> dict[str, Any]:
    vertices = _stl_vertices(path)
    if not vertices:
        return {"exists": path.is_file(), "format": None, "triangle_count": 0, "raw_bbox_mm": None}
    mins = [min(vertex[index] for vertex in vertices) for index in range(3)]
    maxs = [max(vertex[index] for vertex in vertices) for index in range(3)]
    sizes = [maxs[index] - mins[index] for index in range(3)]
    return {
        "exists": True,
        "format": "binary" if _looks_binary_stl(path) else "ascii",
        "triangle_count": len(vertices) // 3,
        "raw_bbox_mm": {"min": mins, "max": maxs, "size": sizes},
    }


def _stl_vertices(path: Path) -> list[tuple[float, float, float]]:
    if not path.is_file():
        return []
    if _looks_binary_stl(path):
        return _binary_stl_vertices(path)
    return _ascii_stl_vertices(path)


def _looks_binary_stl(path: Path) -> bool:
    data = path.read_bytes()
    if len(data) < 84:
        return False
    count = struct.unpack("<I", data[80:84])[0]
    return len(data) == 84 + count * 50


def _binary_stl_vertices(path: Path) -> list[tuple[float, float, float]]:
    data = path.read_bytes()
    count = struct.unpack("<I", data[80:84])[0]
    vertices: list[tuple[float, float, float]] = []
    offset = 84
    for _ in range(count):
        # normal vector is first 12 bytes; then three float32 vertices.
        for vertex_index in range(3):
            start = offset + 12 + vertex_index * 12
            vertices.append(struct.unpack("<fff", data[start : start + 12]))
        offset += 50
    return [(float(x), float(y), float(z)) for x, y, z in vertices]


def _ascii_stl_vertices(path: Path) -> list[tuple[float, float, float]]:
    vertices: list[tuple[float, float, float]] = []
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = raw_line.strip().split()
        if len(parts) == 4 and parts[0].lower() == "vertex":
            vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
    return vertices


def _bbox_close(actual: dict[str, Any] | None, expected: dict[str, Any], *, tolerance_mm: float = 0.02) -> bool:
    if not isinstance(actual, dict):
        return False
    actual_size = actual.get("size")
    expected_size = expected.get("size")
    if not isinstance(actual_size, list) or not isinstance(expected_size, list):
        return False
    return all(abs(float(a) - float(e)) <= tolerance_mm for a, e in zip(actual_size, expected_size))


def mesh_entry(
    *,
    mesh_id: str,
    role: str,
    repo_path: Path,
    source_path: Path,
    install_path: Path | None,
    expected_bbox_mm: dict[str, Any],
    uri: str,
    scale_to_m: tuple[float, float, float],
    origin_xyz_rpy: tuple[float, float, float, float, float, float],
    axis_convention: str,
    candidate_path: Path | None = None,
) -> dict[str, Any]:
    bbox = stl_bbox_mm(repo_path)
    source_bbox = stl_bbox_mm(source_path)
    install_bbox = stl_bbox_mm(install_path) if install_path is not None else None
    candidate_bbox = stl_bbox_mm(candidate_path) if candidate_path is not None else None
    repo_sha = sha256(repo_path)
    source_sha = sha256(source_path)
    install_sha = sha256(install_path) if install_path is not None else None
    candidate_sha = sha256(candidate_path) if candidate_path is not None else None
    raw_bbox = bbox.get("raw_bbox_mm")
    source_raw_bbox = source_bbox.get("raw_bbox_mm")
    install_raw_bbox = install_bbox.get("raw_bbox_mm") if isinstance(install_bbox, dict) else None
    candidate_raw_bbox = candidate_bbox.get("raw_bbox_mm") if isinstance(candidate_bbox, dict) else None
    source_geometry_matches_repo = bool(
        bbox.get("triangle_count") == source_bbox.get("triangle_count")
        and _bbox_close(raw_bbox, source_raw_bbox or {})
    )
    install_geometry_matches_repo = (
        bool(bbox.get("triangle_count") == install_bbox.get("triangle_count") and _bbox_close(raw_bbox, install_raw_bbox or {}))
        if isinstance(install_bbox, dict)
        else None
    )
    candidate_geometry_matches_repo = (
        bool(
            bbox.get("triangle_count") == candidate_bbox.get("triangle_count")
            and _bbox_close(raw_bbox, candidate_raw_bbox or {})
        )
        if isinstance(candidate_bbox, dict)
        else None
    )
    return {
        "id": mesh_id,
        "role": role,
        "claim_tier": "visual_only",
        "uri": uri,
        "repo_path": str(repo_path),
        "source_asset": str(source_path),
        "install_path": str(install_path) if install_path is not None else None,
        "candidate_asset": str(candidate_path) if candidate_path is not None else None,
        "exists": repo_path.is_file(),
        "source_exists": source_path.is_file(),
        "install_exists": install_path.is_file() if install_path is not None else None,
        "repo_sha256": repo_sha,
        "source_sha256": source_sha,
        "install_sha256": install_sha,
        "candidate_sha256": candidate_sha,
        "source_matches_repo_copy": bool(repo_sha and source_sha and repo_sha == source_sha),
        "install_matches_repo_copy": bool(repo_sha and install_sha and repo_sha == install_sha)
        if install_path is not None
        else None,
        "candidate_matches_repo_copy": bool(repo_sha and candidate_sha and repo_sha == candidate_sha)
        if candidate_path is not None
        else None,
        "source_geometry_matches_repo_copy": source_geometry_matches_repo,
        "install_geometry_matches_repo_copy": install_geometry_matches_repo,
        "candidate_geometry_matches_repo_copy": candidate_geometry_matches_repo,
        "stl": bbox,
        "source_stl": source_bbox,
        "install_stl": install_bbox,
        "candidate_stl": candidate_bbox,
        "expected_raw_bbox_mm": expected_bbox_mm,
        "raw_bbox_size_matches_expected": _bbox_close(raw_bbox, expected_bbox_mm),
        "scale_to_m": list(scale_to_m),
        "scaled_bbox_m": {
            "size": [float(value) * float(scale_to_m[index]) for index, value in enumerate(raw_bbox["size"])]
        }
        if isinstance(raw_bbox, dict)
        else None,
        "unit_convention": "STL coordinates are millimeters; URDF/SDF scale 0.001 converts to meters.",
        "origin_xyz_rpy": list(origin_xyz_rpy),
        "axis_convention": axis_convention,
        "allowed_claim": "primary visual mesh evidence only",
        "forbidden_claim": "simulated_ft; physical Gazebo collision/contact physics; real bench/live contact",
    }


def build_audit(*, generated_at: str | None = None) -> dict[str, Any]:
    generated_at = generated_at or _now_iso()
    eoat = mesh_entry(
        mesh_id="eoat_v13_assembly_primary_visual",
        role="EOAT primary visual mesh",
        repo_path=EOAT_REPO_MESH,
        source_path=Path(gazebo.EOAT_REAL_MESH_SOURCE_ASSET),
        install_path=EOAT_INSTALL_MESH,
        expected_bbox_mm=gazebo.EOAT_REAL_MESH_RAW_BBOX_MM,
        uri=gazebo.EOAT_REAL_MESH_URI,
        scale_to_m=gazebo.EOAT_REAL_MESH_SCALE,
        origin_xyz_rpy=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        axis_convention="URDF link-local +Z extends from tool0 toward active contact point.",
        candidate_path=EOAT_XWECHAT_CANDIDATE,
    )
    surface = mesh_entry(
        mesh_id="contact_surface_two_piece_v11_3mm_primary_visual",
        role="contact surface primary visual mesh",
        repo_path=SURFACE_REPO_MESH,
        source_path=Path(gazebo.CONTACT_SURFACE_REAL_MESH_SOURCE_ASSET),
        install_path=SURFACE_INSTALL_MESH,
        expected_bbox_mm=gazebo.CONTACT_SURFACE_REAL_MESH_RAW_BBOX_MM,
        uri=gazebo.CONTACT_SURFACE_REAL_MESH_URI,
        scale_to_m=gazebo.CONTACT_SURFACE_REAL_MESH_SCALE,
        origin_xyz_rpy=gazebo.CONTACT_SURFACE_REAL_MESH_VISUAL_POSE,
        axis_convention="SDF pose rotates STL so modeled surface top aligns with Gazebo world +Z normal.",
        candidate_path=SURFACE_COUPON_REPO_MESH,
    )
    mesh_ready = all(
        bool(entry["exists"])
        and bool(entry["source_exists"])
        and bool(entry["source_geometry_matches_repo_copy"])
        and bool(entry["install_matches_repo_copy"])
        and bool(entry["install_geometry_matches_repo_copy"])
        and bool(entry["raw_bbox_size_matches_expected"])
        for entry in (eoat, surface)
    )
    return {
        "schema": "ur10e_visual_mesh_foundation_audit_v1",
        "generated_at": generated_at,
        "claim_tier": "visual_only",
        "mode": "offline_no_live_prelaunch_mesh_foundation_audit",
        "goal_lineage": "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md",
        "live_robot_command_authorized": False,
        "real_bench_live_contact_authorized": False,
        "marker_default_style": "minimal_tcp_dot",
        "marker_visual_role": "auxiliary_tcp_pose_reference_only",
        "primitive_proxy_policy": "primitive cylinders/boxes/spheres allowed only as simplified collision or optional debug markers; not primary visual cues",
        "observer_acceptance_prelaunch_ready": mesh_ready,
        "post_launch_observer_screenshot_required": True,
        "forbidden_claim": "visual demo success before a post-integration visible Gazebo GUI screenshot review; simulated_ft; physical Gazebo collision/contact physics; real bench/live contact",
        "meshes": {
            "eoat": eoat,
            "contact_surface": surface,
        },
    }


def write_audit(output_dir: Path, *, generated_at: str | None = None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "visual_mesh_foundation_audit.json"
    path.write_text(json.dumps(build_audit(generated_at=generated_at), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(write_audit(args.output_dir, generated_at=args.generated_at))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
