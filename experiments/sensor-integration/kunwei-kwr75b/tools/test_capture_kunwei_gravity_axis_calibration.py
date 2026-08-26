from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

import capture_kunwei_gravity_axis_calibration as capture
import capture_kunwei_payload_cog_calibration as payload


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.reads = [b"frame", b""]

    def __enter__(self) -> "FakeSocket":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def settimeout(self, _timeout: float) -> None:
        pass

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)

    def recv(self, _size: int) -> bytes:
        return self.reads.pop(0)


def test_sensor_worker_retains_synchronous_ur_actual_tcp_force() -> None:
    fake = FakeSocket()
    shared = capture.SharedState()
    shared.segment = "P0"
    shared.segment_index = 0
    shared.latest_rtde = {
        "actual_q": [0.0] * 6,
        "actual_TCP_pose": [0.0] * 6,
        "actual_TCP_speed": [0.0] * 6,
        "actual_TCP_force": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        "runtime_state": 1,
        "robot_mode": 7,
        "safety_mode": 1,
    }
    shared.latest_rtde_mono = 1.0
    shared.rtde_samples = 7
    args = argparse.Namespace(
        sensor_ip="fake",
        sensor_port=5152,
        connect_timeout_s=1.0,
        no_start_command=False,
        no_stop_command=False,
        flush_every=1,
    )

    with TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        with patch.object(capture.socket, "create_connection", return_value=fake), \
             patch.object(capture, "pop_frames", return_value=([b"frame"], 0)), \
             patch.object(capture, "parse_frame", return_value=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6)):
            capture.sensor_worker(args, shared, output_dir / "capture.csv", output_dir / "raw.bin")

        row = next(csv.DictReader((output_dir / "capture.csv").open(newline="")))
        assert [row[col] for col in capture.UR_FORCE_COLS] == ["1", "2", "3", "4", "5", "6"]
        stats = shared.segment_ur_force_stats["P0"]
        assert all(stats[col].n == 1 for col in capture.UR_FORCE_COLS)
        assert [stats[col].mean for col in capture.UR_FORCE_COLS] == [1, 2, 3, 4, 5, 6]

    assert fake.sent == [capture.START_STREAM, capture.STOP_STREAM]

    p0 = [26.00022327384637, -94.99769405105059, -124.99880022678312, -1.9992690094169994, -7.000387466216799, -140.53747057064948]
    plan = payload.build_pose_plan(p0)
    assert [pose["name"] for pose in plan] == [
        "P0",
        "P1",
        "P2",
        "P3",
        "P0_return_repeat_drift_validation",
    ]
    assert plan[0]["joint_deg"] == p0
    assert plan[1]["joint_deg"][3] == p0[3] + 45.0
    assert plan[2]["joint_deg"][3] == p0[3] - 45.0
    assert plan[3]["joint_deg"][3] == p0[3]
    assert plan[3]["joint_deg"][4] == p0[4] - 60.0
    for pose in plan:
        assert [pose["joint_deg"][idx] for idx in (0, 1, 2, 5)] == [p0[idx] for idx in (0, 1, 2, 5)]
        assert payload._format_joint_deg(pose["joint_deg"]) in pose["operator_action"]
    assert sum(pose["unique_pose"] for pose in plan) == 4
    assert plan[-1]["exclude_from_unique_pose_count"] is True
