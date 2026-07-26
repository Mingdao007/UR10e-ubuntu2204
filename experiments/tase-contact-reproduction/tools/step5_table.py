#!/usr/bin/env python3
"""Step5 trajectory/stage table helpers."""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
TABLE_PATH = EXPERIMENT_ROOT / "config" / "step5_stage_table.json"


@lru_cache(maxsize=16)
def _load_json_snapshot(
    resolved_path: str,
    device: int,
    inode: int,
    mtime_ns: int,
    size: int,
) -> dict[str, Any]:
    """Decode one immutable file version; identity fields form the cache key."""

    del device, inode, mtime_ns, size
    return json.loads(Path(resolved_path).read_text(encoding="utf-8"))


def load_json(path: Path) -> dict[str, Any]:
    """Load read-only configuration, reusing it until the file version changes."""

    resolved = path.resolve()
    stat = resolved.stat()
    return _load_json_snapshot(
        str(resolved),
        stat.st_dev,
        stat.st_ino,
        stat.st_mtime_ns,
        stat.st_size,
    )


def load_step5_table(path: Path = TABLE_PATH) -> dict[str, Any]:
    table = load_json(path)
    if table.get("status") != "active":
        raise RuntimeError(f"Step5 table is not active: {path}")
    if "stages" not in table or not isinstance(table["stages"], list):
        raise RuntimeError(f"Step5 table has no stage list: {path}")
    return table


def step5_stage(stage_id: str, table: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = table or load_step5_table()
    for stage in payload["stages"]:
        if stage.get("id") == stage_id:
            return stage
    raise KeyError(f"unknown Step5 stage id: {stage_id}")


def active_no_contact_stage(table: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = table or load_step5_table()
    matches = [
        stage
        for stage in payload["stages"]
        if stage.get("active") and stage.get("contact") is False and stage.get("bridge") is False
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one active no-contact Step5 stage, got {len(matches)}")
    return matches[0]


def load_stage_frame(stage: dict[str, Any]) -> dict[str, Any]:
    frame_path = EXPERIMENT_ROOT / str(stage["frame"])
    frame = load_json(frame_path)
    if frame.get("status") != "ok":
        raise RuntimeError(f"Step5 frame is not ok: {frame_path}")
    if not frame.get("policy", {}).get("no_scale"):
        raise RuntimeError(f"Step5 frame is not no-scale: {frame_path}")
    if not frame.get("guard", {}).get("passed"):
        raise RuntimeError(f"Step5 frame guard did not pass: {frame_path}")
    return frame


def basis_xy(frame: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
    basis = frame["basis"]
    origin = tuple(float(v) for v in basis["origin_xy_m"])
    u_along = tuple(float(v) for v in basis["u_along_xy"])
    p_lateral = tuple(float(v) for v in basis["p_lateral_xy"])
    return origin, u_along, p_lateral


def xy_from_basis(
    origin: tuple[float, float],
    u_along: tuple[float, float],
    p_lateral: tuple[float, float],
    along_m: float,
    lateral_m: float,
) -> tuple[float, float]:
    return (
        origin[0] + along_m * u_along[0] + lateral_m * p_lateral[0],
        origin[1] + along_m * u_along[1] + lateral_m * p_lateral[1],
    )


def cycloid_reference_local(stage: dict[str, Any], elapsed_s: float) -> dict[str, float]:
    duration_s = float(stage["duration_s"])
    amplitude_m = float(stage["amplitude_m"])
    omega_rad_s = float(stage["phase_law"]["omega_rad_s"])
    t_s = min(max(float(elapsed_s), 0.0), duration_s)
    phase = omega_rad_s * t_s
    return {
        "path_time_s": t_s,
        "progress": t_s,
        "phase_rad": phase,
        "local_x_m": amplitude_m * (phase - math.sin(phase)),
        "local_y_m": amplitude_m * (1.0 - math.cos(phase)),
        "local_vx_m_s": amplitude_m * omega_rad_s * (1.0 - math.cos(phase)),
        "local_vy_m_s": amplitude_m * omega_rad_s * math.sin(phase),
    }


def step5_path_reference(
    stage_id: str,
    pose_xy: tuple[float, float],
    elapsed_s: float,
    *,
    table: dict[str, Any] | None = None,
    frame: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stage = step5_stage(stage_id, table)
    if stage.get("shape") != "cycloid":
        raise ValueError(f"unsupported Step5 shape: {stage.get('shape')}")
    frame_payload = frame or load_stage_frame(stage)
    origin, u_along, p_lateral = basis_xy(frame_payload)
    local = cycloid_reference_local(stage, elapsed_s)
    desired_xy = xy_from_basis(origin, u_along, p_lateral, local["local_x_m"], local["local_y_m"])
    desired_vxy = (
        local["local_vx_m_s"] * u_along[0] + local["local_vy_m_s"] * p_lateral[0],
        local["local_vx_m_s"] * u_along[1] + local["local_vy_m_s"] * p_lateral[1],
    )
    return {
        "stage_id": stage_id,
        "progress": local["progress"],
        "path_time_s": local["path_time_s"],
        "phase_rad": local["phase_rad"],
        "desired_xy": desired_xy,
        "desired_velocity_xy": desired_vxy,
        "path_error_xy": (desired_xy[0] - pose_xy[0], desired_xy[1] - pose_xy[1]),
        "local": local,
    }
