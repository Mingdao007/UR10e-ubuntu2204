from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step6_figure8_autotune_v1.v5_ros2_observation_mirror import (  # noqa: E402
    ALLOWED_TOPICS,
    EVENT_TOPIC,
    MemoryObservationSinkV1,
    ObservationEnvelopeV1,
    ObservationKindV1,
    ROS2ObservationMirrorV1,
    Rosbag2SQLiteSinkV1,
    SAMPLE_TOPIC,
    TimeAlignmentV1,
    V5ROS2ObservationError,
)


def _envelope(kind: ObservationKindV1, sequence: int, monotonic_ns: int, *, source: str = "LIFECYCLE") -> ObservationEnvelopeV1:
    return ObservationEnvelopeV1(
        kind,
        source,
        sequence,
        TimeAlignmentV1(monotonic_ns, monotonic_ns + 10, monotonic_ns + 20),
        {"state": "PATH", "value": sequence},
    )


def test_observation_mirror_is_10hz_nonblocking_and_events_bypass_rate_limit():
    sink = MemoryObservationSinkV1()
    mirror = ROS2ObservationMirrorV1(sink)
    first = mirror.submit(_envelope(ObservationKindV1.SAMPLE, 1, 1_000_000_000))
    skipped = mirror.submit(_envelope(ObservationKindV1.SAMPLE, 2, 1_050_000_000))
    event = mirror.submit(_envelope(ObservationKindV1.EVENT, 3, 1_050_000_001))
    assert first["accepted"] and first["disposition"] == "ENQUEUED_SAMPLE"
    assert not skipped["accepted"] and skipped["disposition"] == "SKIPPED_RATE"
    assert event["accepted"] and event["disposition"] == "ENQUEUED_EVENT"
    assert event["events_bypass_rate_limit"] is True
    assert mirror.drain_one()["status"] == "PUBLISHED"
    assert mirror.drain_one()["status"] == "PUBLISHED"
    assert [row["topic"] for row in sink.rows] == [SAMPLE_TOPIC, EVENT_TOPIC]
    assert all(row["control_authority"] is False and row["command_topic"] is False for row in sink.rows)


def test_observation_mirror_drops_newest_at_capacity_and_sink_failure_isolated():
    class FailingSink:
        def write(self, _envelope):
            raise RuntimeError("sidecar-only")

        def close(self):
            raise RuntimeError("close-sidecar-only")

    mirror = ROS2ObservationMirrorV1(FailingSink())
    for sequence in range(1, 129):
        receipt = mirror.submit(_envelope(ObservationKindV1.EVENT, sequence, sequence))
        assert receipt["accepted"]
    dropped = mirror.submit(_envelope(ObservationKindV1.EVENT, 129, 129))
    assert not dropped["accepted"] and dropped["disposition"] == "DROP_NEWEST"
    assert dropped["physical_campaign_dependency"] is False
    assert mirror.drain_one()["status"] == "FAILED"
    mirror.close()
    assert mirror.failed == 2
    assert mirror.snapshot()["control_authority"] is False


def test_observation_envelope_rejects_non_observation_topic_and_non_json_payload():
    alignment = TimeAlignmentV1(1, 2, 3)
    with pytest.raises(V5ROS2ObservationError):
        ObservationEnvelopeV1(ObservationKindV1.SAMPLE, "LIFECYCLE", 1, alignment, {}, topic="/joint_commands")
    with pytest.raises(V5ROS2ObservationError):
        ObservationEnvelopeV1(ObservationKindV1.SAMPLE, "LIFECYCLE", 1, alignment, {"bad": object()})


@pytest.mark.skipif(importlib.util.find_spec("rosbag2_py") is None, reason="ROS2 runtime unavailable")
def test_rosbag2_sink_writes_real_sqlite3_bag(tmp_path: Path):
    uri = tmp_path / "observation_bag"
    sink = Rosbag2SQLiteSinkV1(uri)
    sink.write(_envelope(ObservationKindV1.SAMPLE, 1, 1_000_000_000))
    sink.write(_envelope(ObservationKindV1.EVENT, 2, 1_100_000_000))
    sink.close()
    databases = tuple(uri.glob("*.db3"))
    assert (uri / "metadata.yaml").is_file()
    assert len(databases) == 1
    with sqlite3.connect(databases[0]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        topics = {row[0] for row in connection.execute("SELECT name FROM topics")}
    assert topics == set(ALLOWED_TOPICS)


def test_ros2_mirror_source_has_no_reader_driver_or_command_authority():
    source = (ROOT / "tools" / "step6_figure8_autotune_v1" / "v5_ros2_observation_mirror.py").read_text(encoding="utf-8").lower()
    for forbidden in ("ur_robot_driver", "external_control", "ros2_control", "import rtde", "import kunwei", "create_subscription", "joint_commands"):
        assert forbidden not in source
