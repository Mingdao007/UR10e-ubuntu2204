"""Non-blocking, observation-only ROS2 and rosbag2 mirror for Autotuner V5."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import math
from pathlib import Path
import queue
import threading
import time
from typing import Any, Mapping, Protocol


ROS2_OBSERVATION_SCHEMA = "step6.autotune/ros2-observation-envelope-v1"
ROS2_OBSERVATION_VERSION = 1
SAMPLE_TOPIC = "/autotuner_v5/observations"
EVENT_TOPIC = "/autotuner_v5/events"
ALLOWED_TOPICS = (SAMPLE_TOPIC, EVENT_TOPIC)
SAMPLE_RATE_HZ = 10.0
QUEUE_CAPACITY = 128
ALLOWED_SOURCES = ("FILTER_SHADOW", "CAMERA", "LIFECYCLE", "AUTOTUNER")


class V5ROS2ObservationError(RuntimeError):
    """The ROS2 mirror crossed its observation-only boundary."""


class ObservationKindV1(str, Enum):
    SAMPLE = "SAMPLE"
    EVENT = "EVENT"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise V5ROS2ObservationError(f"{name} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise V5ROS2ObservationError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise V5ROS2ObservationError(f"{name} must be finite")
    return result


def _json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise V5ROS2ObservationError("observation payload is not a mapping")
    try:
        return json.loads(_canonical(dict(value)))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise V5ROS2ObservationError("observation payload is not canonical JSON") from exc


@dataclass(frozen=True)
class TimeAlignmentV1:
    monotonic_ns: int
    source_timestamp_ns: int
    ros_timestamp_ns: int
    schema: str = "step6.autotune/time-alignment-v1"
    version: int = ROS2_OBSERVATION_VERSION

    def __post_init__(self) -> None:
        if self.schema != "step6.autotune/time-alignment-v1" or self.version != ROS2_OBSERVATION_VERSION:
            raise V5ROS2ObservationError("time alignment schema/version differs")
        if any(type(value) is not int or value < 0 for value in (self.monotonic_ns, self.source_timestamp_ns, self.ros_timestamp_ns)):
            raise V5ROS2ObservationError("time alignment clocks are invalid")

    @property
    def source_to_ros_offset_ns(self) -> int:
        return self.ros_timestamp_ns - self.source_timestamp_ns

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "monotonic_ns": self.monotonic_ns,
            "source_timestamp_ns": self.source_timestamp_ns,
            "ros_timestamp_ns": self.ros_timestamp_ns,
            "source_to_ros_offset_ns": self.source_to_ros_offset_ns,
        }

    @classmethod
    def capture(
        cls,
        *,
        source_timestamp_ns: int,
        monotonic_ns: int | None = None,
        ros_timestamp_ns: int | None = None,
    ) -> "TimeAlignmentV1":
        return cls(
            time.monotonic_ns() if monotonic_ns is None else monotonic_ns,
            source_timestamp_ns,
            time.time_ns() if ros_timestamp_ns is None else ros_timestamp_ns,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TimeAlignmentV1":
        expected = {"schema", "version", "monotonic_ns", "source_timestamp_ns", "ros_timestamp_ns", "source_to_ros_offset_ns"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise V5ROS2ObservationError("time alignment mapping differs")
        try:
            alignment = cls(value["monotonic_ns"], value["source_timestamp_ns"], value["ros_timestamp_ns"], schema=value["schema"], version=value["version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise V5ROS2ObservationError("time alignment mapping is invalid") from exc
        if value.get("source_to_ros_offset_ns") != alignment.source_to_ros_offset_ns:
            raise V5ROS2ObservationError("time alignment offset differs")
        return alignment


@dataclass(frozen=True)
class ObservationEnvelopeV1:
    kind: ObservationKindV1
    source: str
    sequence: int
    alignment: TimeAlignmentV1
    payload: Mapping[str, Any]
    topic: str | None = None
    schema: str = ROS2_OBSERVATION_SCHEMA
    version: int = ROS2_OBSERVATION_VERSION

    def __post_init__(self) -> None:
        if self.schema != ROS2_OBSERVATION_SCHEMA or self.version != ROS2_OBSERVATION_VERSION or not isinstance(self.kind, ObservationKindV1):
            raise V5ROS2ObservationError("observation envelope schema/kind differs")
        if self.source not in ALLOWED_SOURCES or type(self.sequence) is not int or self.sequence <= 0 or not isinstance(self.alignment, TimeAlignmentV1):
            raise V5ROS2ObservationError("observation envelope identity differs")
        expected_topic = SAMPLE_TOPIC if self.kind is ObservationKindV1.SAMPLE else EVENT_TOPIC
        topic = expected_topic if self.topic is None else self.topic
        if topic != expected_topic or topic not in ALLOWED_TOPICS:
            raise V5ROS2ObservationError("observation envelope topic is not allowed")
        object.__setattr__(self, "topic", topic)
        object.__setattr__(self, "payload", _json_mapping(self.payload))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "kind": self.kind.value,
            "source": self.source,
            "sequence": self.sequence,
            "topic": self.topic,
            "alignment": self.alignment.as_dict(),
            "payload": dict(self.payload),
            "authority": "observation_only",
            "control_authority": False,
            "command_topic": False,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ObservationEnvelopeV1":
        expected = {"schema", "version", "kind", "source", "sequence", "topic", "alignment", "payload", "authority", "control_authority", "command_topic"}
        if not isinstance(value, Mapping) or set(value) != expected or value.get("authority") != "observation_only" or value.get("control_authority") is not False or value.get("command_topic") is not False:
            raise V5ROS2ObservationError("observation envelope mapping differs")
        try:
            return cls(
                ObservationKindV1(value["kind"]),
                str(value["source"]),
                value["sequence"],
                TimeAlignmentV1.from_mapping(value["alignment"]),
                value["payload"],
                topic=value["topic"],
                schema=value["schema"],
                version=value["version"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise V5ROS2ObservationError("observation envelope mapping is invalid") from exc


class ObservationSinkV1(Protocol):
    def write(self, envelope: ObservationEnvelopeV1) -> None: ...

    def close(self) -> None: ...


class MemoryObservationSinkV1:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def write(self, envelope: ObservationEnvelopeV1) -> None:
        self.rows.append(envelope.as_dict())

    def close(self) -> None:
        return None


class CompositeObservationSinkV1:
    def __init__(self, *sinks: ObservationSinkV1) -> None:
        if not sinks:
            raise V5ROS2ObservationError("composite observation sink is empty")
        self.sinks = tuple(sinks)

    def write(self, envelope: ObservationEnvelopeV1) -> None:
        for sink in self.sinks:
            sink.write(envelope)

    def close(self) -> None:
        errors: list[Exception] = []
        for sink in reversed(self.sinks):
            try:
                sink.close()
            except Exception as exc:  # noqa: BLE001 -- close every isolated sink
                errors.append(exc)
        if errors:
            raise V5ROS2ObservationError("one or more observation sinks failed to close") from errors[0]


class Rosbag2SQLiteSinkV1:
    """Lazy rosbag2 SequentialWriter using only std_msgs/String observations."""

    def __init__(self, uri: Path | str) -> None:
        self.uri = Path(uri)
        if self.uri.is_symlink() or (self.uri.exists() and (not self.uri.is_dir() or any(self.uri.iterdir()))):
            raise V5ROS2ObservationError("rosbag2 output URI must be a fresh non-symlink directory")
        self._writer: Any = None
        self._topics: set[str] = set()

    def _open(self) -> None:
        if self._writer is not None:
            return
        try:
            import rosbag2_py
        except ImportError as exc:
            raise V5ROS2ObservationError("rosbag2_py is unavailable") from exc
        writer = rosbag2_py.SequentialWriter()
        writer.open(
            rosbag2_py.StorageOptions(uri=str(self.uri.resolve()), storage_id="sqlite3"),
            rosbag2_py.ConverterOptions(input_serialization_format="cdr", output_serialization_format="cdr"),
        )
        self._writer = writer

    def write(self, envelope: ObservationEnvelopeV1) -> None:
        if not isinstance(envelope, ObservationEnvelopeV1):
            raise V5ROS2ObservationError("rosbag2 sink requires a typed observation")
        self._open()
        try:
            import rosbag2_py
            from rclpy.serialization import serialize_message
            from std_msgs.msg import String
        except ImportError as exc:
            raise V5ROS2ObservationError("ROS2 std_msgs serialization is unavailable") from exc
        assert self._writer is not None
        if envelope.topic not in self._topics:
            self._writer.create_topic(rosbag2_py.TopicMetadata(envelope.topic, "std_msgs/msg/String", "cdr", ""))
            self._topics.add(envelope.topic)
        message = String()
        message.data = _canonical(envelope.as_dict()).decode("utf-8")
        self._writer.write(envelope.topic, serialize_message(message), envelope.alignment.ros_timestamp_ns)

    def close(self) -> None:
        writer = self._writer
        self._writer = None
        if writer is not None:
            del writer


class ROS2StringPublisherSinkV1:
    """Publisher-only node; it has no subscriptions and no command topics."""

    def __init__(self, *, node_name: str = "autotuner_v5_observation_mirror") -> None:
        if not node_name or any(char in node_name for char in "\r\n/"):
            raise V5ROS2ObservationError("ROS2 observation node name is invalid")
        self.node_name = node_name
        self._node: Any = None
        self._publishers: dict[str, Any] = {}
        self._owned_context = False

    def _open(self) -> None:
        if self._node is not None:
            return
        try:
            import rclpy
        except ImportError as exc:
            raise V5ROS2ObservationError("rclpy is unavailable") from exc
        if not rclpy.ok():
            rclpy.init(args=None)
            self._owned_context = True
        self._node = rclpy.create_node(self.node_name)

    def write(self, envelope: ObservationEnvelopeV1) -> None:
        self._open()
        try:
            import rclpy
            from std_msgs.msg import String
        except ImportError as exc:
            raise V5ROS2ObservationError("ROS2 std_msgs publisher is unavailable") from exc
        if envelope.topic not in ALLOWED_TOPICS:
            raise V5ROS2ObservationError("ROS2 publisher topic is not observation-only")
        assert self._node is not None
        publisher = self._publishers.get(envelope.topic)
        if publisher is None:
            publisher = self._node.create_publisher(String, envelope.topic, 10)
            self._publishers[envelope.topic] = publisher
        message = String()
        message.data = _canonical(envelope.as_dict()).decode("utf-8")
        publisher.publish(message)
        rclpy.spin_once(self._node, timeout_sec=0.0)

    def close(self) -> None:
        try:
            import rclpy
        except ImportError:
            rclpy = None
        if self._node is not None:
            self._node.destroy_node()
            self._node = None
            self._publishers.clear()
        if rclpy is not None and self._owned_context and rclpy.ok():
            rclpy.shutdown()
        self._owned_context = False


class ROS2ObservationMirrorV1:
    """Bounded non-blocking queue; sink failures never reach submitters."""

    def __init__(
        self,
        sink: ObservationSinkV1,
        *,
        sample_rate_hz: float = SAMPLE_RATE_HZ,
        capacity: int = QUEUE_CAPACITY,
    ) -> None:
        if not hasattr(sink, "write") or not hasattr(sink, "close"):
            raise V5ROS2ObservationError("observation mirror sink is invalid")
        rate = _finite(sample_rate_hz, "observation mirror sample rate")
        if rate != SAMPLE_RATE_HZ or type(capacity) is not int or capacity != QUEUE_CAPACITY:
            raise V5ROS2ObservationError("observation mirror rate/capacity differs")
        self.sink = sink
        self.period_ns = int(1_000_000_000 / rate)
        self.capacity = capacity
        self._queue: queue.Queue[ObservationEnvelopeV1] = queue.Queue(maxsize=capacity)
        self._last_sample_ns: dict[str, int] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.published = 0
        self.skipped_rate = 0
        self.dropped_newest = 0
        self.failed = 0
        self.last_error: str | None = None

    def submit(self, envelope: ObservationEnvelopeV1) -> dict[str, Any]:
        if not isinstance(envelope, ObservationEnvelopeV1):
            raise V5ROS2ObservationError("observation mirror submit requires a typed envelope")
        with self._lock:
            if envelope.kind is ObservationKindV1.SAMPLE:
                previous = self._last_sample_ns.get(envelope.source)
                if previous is not None and envelope.alignment.monotonic_ns - previous < self.period_ns:
                    self.skipped_rate += 1
                    return self._receipt(envelope, False, "SKIPPED_RATE")
            try:
                self._queue.put_nowait(envelope)
            except queue.Full:
                self.dropped_newest += 1
                return self._receipt(envelope, False, "DROP_NEWEST")
            if envelope.kind is ObservationKindV1.SAMPLE:
                self._last_sample_ns[envelope.source] = envelope.alignment.monotonic_ns
        return self._receipt(envelope, True, "ENQUEUED_EVENT" if envelope.kind is ObservationKindV1.EVENT else "ENQUEUED_SAMPLE")

    def _receipt(self, envelope: ObservationEnvelopeV1, accepted: bool, disposition: str) -> dict[str, Any]:
        return {
            "schema": "step6.autotune/ros2-observation-submit-receipt-v1",
            "version": 1,
            "source": envelope.source,
            "sequence": envelope.sequence,
            "kind": envelope.kind.value,
            "accepted": accepted,
            "disposition": disposition,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "events_bypass_rate_limit": True,
            "capacity": self.capacity,
            "overflow_policy": "drop_newest",
            "physical_campaign_dependency": False,
            "control_authority": False,
        }

    def drain_one(self) -> dict[str, Any] | None:
        try:
            envelope = self._queue.get_nowait()
        except queue.Empty:
            return None
        try:
            self.sink.write(envelope)
            self.published += 1
            return {"status": "PUBLISHED", "source": envelope.source, "sequence": envelope.sequence}
        except Exception as exc:  # noqa: BLE001 -- isolated sidecar failure boundary
            self.failed += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {"status": "FAILED", "source": envelope.source, "sequence": envelope.sequence, "error": self.last_error}
        finally:
            self._queue.task_done()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                envelope = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.sink.write(envelope)
                self.published += 1
            except Exception as exc:  # noqa: BLE001 -- isolated sidecar failure boundary
                self.failed += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                self._queue.task_done()

    def start(self) -> None:
        if self._worker is not None:
            raise V5ROS2ObservationError("observation mirror worker is already started")
        self._worker = threading.Thread(target=self._run, name="autotuner-v5-observation-mirror", daemon=True)
        self._worker.start()

    def close(self, *, drain: bool = False, timeout_s: float = 2.0) -> None:
        if drain:
            deadline = time.monotonic() + _finite(timeout_s, "observation mirror close timeout")
            while not self._queue.empty() and time.monotonic() < deadline:
                time.sleep(0.01)
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=max(0.0, _finite(timeout_s, "observation mirror close timeout")))
            self._worker = None
        try:
            self.sink.close()
        except Exception as exc:  # noqa: BLE001 -- close failure remains sidecar-only
            self.failed += 1
            self.last_error = f"{type(exc).__name__}: {exc}"

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": "step6.autotune/ros2-observation-mirror-status-v1",
            "version": 1,
            "queue_depth": self._queue.qsize(),
            "capacity": self.capacity,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "events_bypass_rate_limit": True,
            "published": self.published,
            "skipped_rate": self.skipped_rate,
            "dropped_newest": self.dropped_newest,
            "failed": self.failed,
            "last_error": self.last_error,
            "allowed_topics": list(ALLOWED_TOPICS),
            "storage_id": "sqlite3",
            "authority": "observation_only",
            "physical_campaign_dependency": False,
            "control_authority": False,
        }


__all__ = [
    "ALLOWED_TOPICS",
    "CompositeObservationSinkV1",
    "EVENT_TOPIC",
    "MemoryObservationSinkV1",
    "ObservationEnvelopeV1",
    "ObservationKindV1",
    "QUEUE_CAPACITY",
    "ROS2ObservationMirrorV1",
    "ROS2StringPublisherSinkV1",
    "Rosbag2SQLiteSinkV1",
    "SAMPLE_RATE_HZ",
    "SAMPLE_TOPIC",
    "TimeAlignmentV1",
    "V5ROS2ObservationError",
]
