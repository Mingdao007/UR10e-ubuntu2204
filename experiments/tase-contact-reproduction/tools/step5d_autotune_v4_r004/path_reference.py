"""Immutable r004/r005 PATH reference binding.

The old ``step5_table`` helper is intentionally mutable-file backed.  A live
V4 writer must consume the exact relevant row and frame that were reviewed,
not whatever a later edit to the global table happens to return.  This module
is the small, hash-bound adapter used by the r004 production owners and by
r005's parent closure.
"""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Any, Mapping


PATH_STAGE_ID = "step5d_strict_rnn_autotune_v1"
PATH_SHAPE = "cycloid"
PATH_DURATION_S = 60.0
PATH_AMPLITUDE_M = 0.015
PATH_OMEGA_RAD_S = 0.1

# These are semantic bindings, not a claim that the whole mutable stage table
# is frozen.  The reviewed relevant row is equal in the current and canonical
# V3 tables; all unrelated rows remain outside this adapter.
BOUND_STAGE_ROW_SEMANTIC_SHA256 = (
    "3ba6468db434b6c5e5eca7ca387d01ad608d195f507caec27f56f081af40a925"
)
BOUND_FRAME_SEMANTIC_SHA256 = (
    "0a9b800b718c4058340961a4164eb2e5e48b1225b5c5b89f83f6da3a96dbfd82"
)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


# Only values consumed by the reference formula are carried.  The semantic
# SHA above binds this relevant-row selection to the reviewed full row.
R004_PATH_ROW_SNAPSHOT = _freeze(
    {
        "id": PATH_STAGE_ID,
        "shape": PATH_SHAPE,
        "duration_s": PATH_DURATION_S,
        "amplitude_m": PATH_AMPLITUDE_M,
        "phase_law": {
            "omega_rad_s": PATH_OMEGA_RAD_S,
            "type": "canonical_cycloid_linear_time",
        },
        "frame": "config/step5_safe_frame.json",
    }
)

R004_PATH_FRAME_SNAPSHOT = _freeze(
    {
        "status": "ok",
        "active_stage": "Step5",
        "active_variant": "Step5a no-contact cycloid",
        "policy": {"no_scale": True, "shape": "step5a_cycloid_no_contact"},
        "guard": {"passed": True},
        "basis": {
            "origin_xy_m": (0.487795411149049, 0.12932679270060748),
            "u_along_xy": (-0.010785642631908187, 0.9999418332648238),
            "p_lateral_xy": (-0.9999418332648239, -0.010785642631908406),
        },
    }
)


def path_reference_binding() -> Mapping[str, str]:
    """Return the immutable semantic binding advertised to r005."""

    return MappingProxyType(
        {
            "stage_id": PATH_STAGE_ID,
            "stage_row_semantic_sha256": BOUND_STAGE_ROW_SEMANTIC_SHA256,
            "frame_semantic_sha256": BOUND_FRAME_SEMANTIC_SHA256,
        }
    )


def _cycloid_reference(elapsed_s: float) -> dict[str, float]:
    if isinstance(elapsed_s, bool):
        raise ValueError("PATH elapsed time must be finite")
    time_s = float(elapsed_s)
    if not math.isfinite(time_s):
        raise ValueError("PATH elapsed time must be finite")
    time_s = min(max(time_s, 0.0), PATH_DURATION_S)
    phase = PATH_OMEGA_RAD_S * time_s
    return {
        "path_time_s": time_s,
        "progress": time_s,
        "phase_rad": phase,
        "local_x_m": PATH_AMPLITUDE_M * (phase - math.sin(phase)),
        "local_y_m": PATH_AMPLITUDE_M * (1.0 - math.cos(phase)),
        "local_vx_m_s": PATH_AMPLITUDE_M * PATH_OMEGA_RAD_S * (1.0 - math.cos(phase)),
        "local_vy_m_s": PATH_AMPLITUDE_M * PATH_OMEGA_RAD_S * math.sin(phase),
    }


def step5_path_reference(
    stage_id: str,
    pose_xy: tuple[float, float],
    elapsed_s: float,
) -> dict[str, Any]:
    """Evaluate the bound V1 cycloid without reading the global stage table."""

    if stage_id != PATH_STAGE_ID:
        raise KeyError(f"unknown bound Step5 stage id: {stage_id}")
    if len(tuple(pose_xy)) != 2:
        raise ValueError("pose_xy must contain two values")
    pose = tuple(float(value) for value in pose_xy)
    if not all(math.isfinite(value) for value in pose):
        raise ValueError("pose_xy must be finite")
    local = _cycloid_reference(elapsed_s)
    origin = R004_PATH_FRAME_SNAPSHOT["basis"]["origin_xy_m"]
    u_along = R004_PATH_FRAME_SNAPSHOT["basis"]["u_along_xy"]
    p_lateral = R004_PATH_FRAME_SNAPSHOT["basis"]["p_lateral_xy"]
    desired_xy = (
        origin[0] + local["local_x_m"] * u_along[0] + local["local_y_m"] * p_lateral[0],
        origin[1] + local["local_x_m"] * u_along[1] + local["local_y_m"] * p_lateral[1],
    )
    desired_velocity_xy = (
        local["local_vx_m_s"] * u_along[0] + local["local_vy_m_s"] * p_lateral[0],
        local["local_vx_m_s"] * u_along[1] + local["local_vy_m_s"] * p_lateral[1],
    )
    return {
        "stage_id": PATH_STAGE_ID,
        "progress": local["progress"],
        "path_time_s": local["path_time_s"],
        "phase_rad": local["phase_rad"],
        "desired_xy": desired_xy,
        "desired_velocity_xy": desired_velocity_xy,
        "path_error_xy": (desired_xy[0] - pose[0], desired_xy[1] - pose[1]),
        "local": local,
    }


__all__ = [
    "BOUND_FRAME_SEMANTIC_SHA256",
    "BOUND_STAGE_ROW_SEMANTIC_SHA256",
    "PATH_AMPLITUDE_M",
    "PATH_DURATION_S",
    "PATH_OMEGA_RAD_S",
    "PATH_STAGE_ID",
    "R004_PATH_FRAME_SNAPSHOT",
    "R004_PATH_ROW_SNAPSHOT",
    "path_reference_binding",
    "step5_path_reference",
]
