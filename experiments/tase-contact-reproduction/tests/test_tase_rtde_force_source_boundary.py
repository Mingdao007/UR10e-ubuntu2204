from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r004.transport import OUTPUT_FIELDS, R004OutputSnapshot  # noqa: E402


def _rtde_row() -> dict[str, object]:
    from step5d_autotune_v4_r004.wire import (
        OUTPUT_DOUBLE_FIELDS,
        OUTPUT_INTEGER_FIELDS,
    )

    row: dict[str, object] = {
        "timestamp": 1.0,
        "payload": 0.413,
        "payload_cog": [0.0, 0.0, 0.0],
        "tcp_offset": [0.0] * 6,
        "actual_TCP_speed": [0.0] * 6,
        "actual_TCP_pose": [0.0] * 6,
        "actual_q": [0.0] * 6,
        "actual_qd": [0.0] * 6,
        "target_qd": [0.0] * 6,
        "speed_scaling": 1.0,
        "safety_mode": "NORMAL",
        "robot_mode": "RUNNING",
        "runtime_state": 2,
    }
    row.update({f"output_double_register_{index}": 0.0 for index in OUTPUT_DOUBLE_FIELDS})
    row.update({f"output_int_register_{index}": 0 for index in OUTPUT_INTEGER_FIELDS})
    return row


def test_r004_rtde_recipe_excludes_ur_builtin_force_estimate() -> None:
    assert "actual_TCP_force" not in OUTPUT_FIELDS


def test_r004_snapshot_does_not_parse_builtin_force_even_if_present() -> None:
    row = _rtde_row()
    # A malformed value makes accidental parsing fail visibly. The snapshot
    # should only consume kinematics, controller echoes, safety, and payload.
    row["actual_TCP_force"] = object()

    snapshot = R004OutputSnapshot.from_mapping(1.0, row)

    assert snapshot.q_rad == (0.0,) * 6
    assert snapshot.tcp_pose_m_rad == (0.0,) * 6
