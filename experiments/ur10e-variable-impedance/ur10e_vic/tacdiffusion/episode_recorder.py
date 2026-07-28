"""Bounded, durable TacDiffusion episode recording primitives.

The producer-facing object in this module is deliberately small: it accepts a
validated-in-memory frame and performs one non-blocking bounded enqueue.  JSON
encoding and every filesystem operation belong to the background
``batch_fsync_10`` sealer.  Recorder health is a latched receipt consumed by a
task-level executor; it is never consulted by a 500 Hz controller loop.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, fields
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from .contracts import CONTROL_RATE_HZ, RAW_WRENCH_RATE_HZ


EPISODE_ARTIFACT_SCHEMA = "ur10e_tacdiffusion_episode_artifact/v2"
EPISODE_FRAME_SCHEMA = "ur10e_tacdiffusion_episode_frame/v2"
DURABILITY_MODE = "batch_fsync_10"
RECORDER_HEALTH_SCHEMA = "ur10e_tacdiffusion_recorder_health/v1"
SPOOL_CAPACITY = 8192
BATCH_SIZE = 10
MAX_UNSEALED_TAIL = BATCH_SIZE - 1
OBSERVATION_DIMENSION = 84
ACTION_DIMENSION = 12
DEVICE_SAMPLES_PER_CONTROL_TICK = RAW_WRENCH_RATE_HZ // CONTROL_RATE_HZ


def _finite_vector(values: Sequence[float], length: int, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != length or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain {length} finite values")
    return result


def _optional_vector(
    values: Sequence[float] | None,
    length: int,
    name: str,
) -> tuple[float, ...] | None:
    return None if values is None else _finite_vector(values, length, name)


def _line(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


@dataclass(frozen=True)
class EpisodeFrameV2:
    """One retained 500 Hz row, including invalid/torn-source receipts."""

    episode_id: str
    sample_index: int
    control_sequence: int
    control_time_s: float
    observation_84d: Sequence[float]
    expert_action_12d: Sequence[float]
    applied_action_12d: Sequence[float]
    echoed_action_12d: Sequence[float]
    action_generation: int
    action_age_ticks: int
    action_echo_coherent: bool
    external_device_time_s: float | None
    external_host_visible_time_s: float | None
    external_batch_id: int | None
    external_sample_index: int | None
    external_hold: bool
    external_held_ticks: int
    device_age_samples: int
    host_age_s: float | None
    internal_wrench_valid: bool
    external_lineage_valid: bool = True
    control_time_strict: bool = True
    source_row_torn: bool = False
    source_row_invalid: bool = False
    echoed_action_valid: bool = True
    recorder_valid: bool = True
    controller_time_s: float | None = None
    control_clock: str = "host_monotonic_elapsed"
    # Semantic fields are explicit so a diagnostic command cannot be inferred
    # as an expert label from the numeric action vector alone.  Defaults keep
    # the v2 reader/writer compatible with existing offline fixtures.
    expert_label_available: bool = True
    expert_action_source: str = "deterministic_expert"
    action_label_semantics: str = "deterministic_expert_guarded_action_12d_v1"
    diagnostic_command_12d: Sequence[float] | None = None
    controller_echo_12d: Sequence[float] | None = None
    observation_history_valid: bool = True
    reference_derivatives_valid: bool = True
    desired_pose_6d: Sequence[float] | None = None
    desired_twist_6d: Sequence[float] | None = None
    desired_acceleration_6d: Sequence[float] | None = None
    reference_sample_id: str | None = None
    candidate_window: bool = False
    capture_phase: str = "unknown"

    def __post_init__(self) -> None:
        if not self.episode_id.strip() or self.sample_index < 0 or self.control_sequence < 0:
            raise ValueError("episode frame identity is invalid")
        if not math.isfinite(self.control_time_s) or self.control_time_s < 0.0:
            raise ValueError("episode frame control time is invalid")
        if self.controller_time_s is not None and (
            not math.isfinite(self.controller_time_s) or self.controller_time_s < 0.0
        ):
            raise ValueError("episode frame controller time is invalid")
        object.__setattr__(
            self,
            "observation_84d",
            _finite_vector(self.observation_84d, OBSERVATION_DIMENSION, "observation_84d"),
        )
        for name in ("expert_action_12d", "applied_action_12d", "echoed_action_12d"):
            object.__setattr__(
                self,
                name,
                _finite_vector(getattr(self, name), ACTION_DIMENSION, name),
            )
        if self.action_generation < 0 or self.action_age_ticks < 0:
            raise ValueError("episode action generation/age is invalid")
        if self.external_device_time_s is not None and (
            not math.isfinite(self.external_device_time_s)
            or self.external_device_time_s < 0.0
        ):
            raise ValueError("external device time is invalid")
        if self.external_host_visible_time_s is not None and (
            not math.isfinite(self.external_host_visible_time_s)
            or self.external_host_visible_time_s < 0.0
        ):
            raise ValueError("external host-visible time is invalid")
        for name in ("external_batch_id", "external_sample_index"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} is invalid")
        if self.external_held_ticks < 0 or self.device_age_samples < 0:
            raise ValueError("external hold/device age is invalid")
        if self.external_hold and self.external_held_ticks <= 0:
            raise ValueError("held episode frame must report held ticks")
        if not self.external_hold and self.external_held_ticks != 0:
            raise ValueError("fresh episode frame cannot report held ticks")
        if self.host_age_s is not None and (
            not math.isfinite(self.host_age_s) or self.host_age_s < 0.0
        ):
            raise ValueError("external host age is invalid")
        if not self.control_clock.strip():
            raise ValueError("control clock identity is required")
        if not isinstance(self.expert_label_available, bool):
            raise ValueError("expert_label_available must be boolean")
        if not self.expert_action_source.strip() or not self.action_label_semantics.strip():
            raise ValueError("expert action source and label semantics are required")
        object.__setattr__(
            self,
            "diagnostic_command_12d",
            _optional_vector(self.diagnostic_command_12d, ACTION_DIMENSION, "diagnostic_command_12d"),
        )
        object.__setattr__(
            self,
            "controller_echo_12d",
            _optional_vector(self.controller_echo_12d, ACTION_DIMENSION, "controller_echo_12d"),
        )
        for name in ("desired_pose_6d", "desired_twist_6d", "desired_acceleration_6d"):
            object.__setattr__(
                self,
                name,
                _optional_vector(getattr(self, name), 6, name),
            )
        if self.reference_sample_id is not None and not str(self.reference_sample_id).strip():
            raise ValueError("reference_sample_id must be non-empty when present")
        if not isinstance(self.observation_history_valid, bool) or not isinstance(self.reference_derivatives_valid, bool):
            raise ValueError("observation/reference validity flags must be boolean")
        if not isinstance(self.candidate_window, bool) or not self.capture_phase.strip():
            raise ValueError("capture window/phase metadata is invalid")

    @property
    def row_valid(self) -> bool:
        return bool(
            self.recorder_valid
            and self.control_time_strict
            and self.external_lineage_valid
            and self.internal_wrench_valid
            and self.action_echo_coherent
            and self.echoed_action_valid
            and not self.source_row_torn
            and not self.source_row_invalid
        )

    @property
    def recorder_validity_flags(self) -> dict[str, bool]:
        return {
            "row_valid": self.row_valid,
            "recorder_valid": bool(self.recorder_valid),
            "control_time_strict": bool(self.control_time_strict),
            "external_lineage_valid": bool(self.external_lineage_valid),
            "internal_wrench_valid": bool(self.internal_wrench_valid),
            "action_echo_coherent": bool(self.action_echo_coherent),
            "echoed_action_valid": bool(self.echoed_action_valid),
            "source_row_torn": bool(self.source_row_torn),
            "source_row_invalid": bool(self.source_row_invalid),
        }

    def as_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema"] = EPISODE_FRAME_SCHEMA
        payload["recorder_validity_flags"] = self.recorder_validity_flags
        payload["semantic_flags"] = {
            "expert_label_available": self.expert_label_available,
            "expert_action_source": self.expert_action_source,
            "action_label_semantics": self.action_label_semantics,
            "observation_history_valid": self.observation_history_valid,
            "reference_derivatives_valid": self.reference_derivatives_valid,
            "candidate_window": self.candidate_window,
            "capture_phase": self.capture_phase,
        }
        return payload


# The short alias makes the new-write contract discoverable without removing
# the v1 ``ExpertEpisodeFrame`` reader/writer.
EpisodeFrame = EpisodeFrameV2
_EPISODE_FRAME_FIELDS = tuple(field.name for field in fields(EpisodeFrameV2))


def _with_recorder_metadata(
    frame: EpisodeFrameV2,
    *,
    sample_index: int,
    control_time_strict: bool,
    external_lineage_valid: bool,
    external_hold: bool,
    external_held_ticks: int,
    device_age_samples: int,
) -> EpisodeFrameV2:
    """Reuse already-validated vectors while replacing recorder-owned scalars.

    ``dataclasses.replace`` reruns the full 84D + 36D vector validation at
    500 Hz. The input frame is already frozen and validated; these six bounded
    scalar fields are the only recorder-owned values.
    """

    if sample_index < 0 or external_held_ticks < 0 or device_age_samples < 0:
        raise ValueError("recorder metadata is invalid")
    accepted = object.__new__(EpisodeFrameV2)
    replacements = {
        "sample_index": sample_index,
        "control_time_strict": bool(control_time_strict),
        "external_lineage_valid": bool(external_lineage_valid),
        "external_hold": bool(external_hold),
        "external_held_ticks": external_held_ticks,
        "device_age_samples": device_age_samples,
    }
    for name in _EPISODE_FRAME_FIELDS:
        object.__setattr__(
            accepted,
            name,
            replacements.get(name, getattr(frame, name)),
        )
    return accepted


@dataclass(frozen=True)
class RecorderHealth:
    capacity: int
    queue_depth: int
    enqueued_rows: int
    durable_rows: int
    rejected_rows: int
    dropped_rows: int
    overflowed: bool
    stalled: bool
    writer_error: str | None
    fault: str | None
    durability_mode: str
    unsealed_tail: int
    max_unsealed_tail: int
    sealed: bool
    manifest_written: bool
    tamper_free: bool

    @property
    def ok(self) -> bool:
        return not any(
            (
                self.overflowed,
                self.stalled,
                self.writer_error,
                self.fault,
                self.dropped_rows,
                self.rejected_rows,
            )
        )

    def as_json(self) -> dict[str, Any]:
        return asdict(self) | {"ok": self.ok}


class _FaultLatch:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reason: str | None = None

    def latch(self, reason: str) -> str:
        with self._lock:
            if self._reason is None:
                self._reason = str(reason)
            return self._reason

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason


class BoundedEpisodeSpool:
    """A non-overwriting, non-blocking producer queue."""

    def __init__(
        self,
        capacity: int = SPOOL_CAPACITY,
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if capacity <= 0:
            raise ValueError("spool capacity must be positive")
        self.capacity = int(capacity)
        self._queue: queue.Queue[EpisodeFrameV2] = queue.Queue(maxsize=self.capacity)
        self._fault = _FaultLatch()
        self._clock = clock
        self._enqueued = 0
        self._rejected = 0
        # Diagnostic latency history is bounded too; recorder correctness must
        # not quietly reintroduce an unbounded producer-side list.
        self._enqueue_latencies_us: deque[float] = deque(maxlen=self.capacity)

    def enqueue(self, frame: EpisodeFrameV2) -> bool:
        """Try once and return immediately; this method performs no FS/JSON IO."""

        started = self._clock()
        if self._fault.reason is not None:
            self._rejected += 1
            self._enqueue_latencies_us.append((self._clock() - started) * 1e6)
            return False
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            self._rejected += 1
            self._fault.latch("spool_overflow")
            self._enqueue_latencies_us.append((self._clock() - started) * 1e6)
            return False
        self._enqueued += 1
        self._enqueue_latencies_us.append((self._clock() - started) * 1e6)
        return True

    def try_enqueue(self, frame: EpisodeFrameV2) -> bool:
        return self.enqueue(frame)

    def put_nowait(self, frame: EpisodeFrameV2) -> bool:
        return self.enqueue(frame)

    def get(self, timeout_s: float) -> EpisodeFrameV2 | None:
        try:
            return self._queue.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def task_done(self) -> None:
        self._queue.task_done()

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    @property
    def unfinished_tasks(self) -> int:
        return self._queue.unfinished_tasks

    @property
    def fault(self) -> str | None:
        return self._fault.reason

    @property
    def enqueued_rows(self) -> int:
        return self._enqueued

    @property
    def rejected_rows(self) -> int:
        return self._rejected

    @property
    def enqueue_latency_us(self) -> tuple[float, ...]:
        return tuple(self._enqueue_latencies_us)


class RecorderError(RuntimeError):
    """Latched recorder fault surfaced at task cadence."""


class BatchFsync10Sealer:
    """Background JSONL sealer with one fsync per ten rows."""

    def __init__(
        self,
        spool: BoundedEpisodeSpool,
        artifact_path: str | Path,
        manifest_path: str | Path,
        *,
        episode_id: str,
        metadata: Mapping[str, Any] | None = None,
        batch_size: int = BATCH_SIZE,
        stall_timeout_s: float = 0.250,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if batch_size != BATCH_SIZE:
            raise ValueError("the frozen sealer batch size is exactly ten")
        if stall_timeout_s <= 0.0 or not math.isfinite(stall_timeout_s):
            raise ValueError("stall timeout must be finite and positive")
        self.spool = spool
        self.artifact_path = Path(artifact_path)
        self.manifest_path = Path(manifest_path)
        self.episode_id = episode_id
        self.metadata = dict(metadata or {})
        self.batch_size = batch_size
        self.stall_timeout_s = float(stall_timeout_s)
        self._clock = clock
        self._fault = _FaultLatch()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle: Any = None
        self._state_lock = threading.Lock()
        self._last_progress = self._clock()
        self._work_pending_since: float | None = None
        self._durable_rows = 0
        self._max_unsealed_tail = 0
        self._inflight_rows = 0
        self._sealed = False
        self._manifest_written = False
        self._tamper_free = False

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("sealer already started")
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        if self.artifact_path.exists() or self.manifest_path.exists():
            raise FileExistsError("episode recorder artifact or manifest already exists")
        try:
            with self._state_lock:
                self._last_progress = self._clock()
                self._work_pending_since = None
            self._handle = self.artifact_path.open("xb")
            header = {
                "schema": EPISODE_ARTIFACT_SCHEMA,
                "format_version": 2,
                "episode_id": self.episode_id,
                "durability_mode": DURABILITY_MODE,
                "spool_capacity": self.spool.capacity,
                "batch_size": self.batch_size,
                "max_unsealed_tail": MAX_UNSEALED_TAIL,
                "metadata": self.metadata,
            }
            self._handle.write(_line(header))
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except Exception as exc:
            self._fault.latch(f"writer_start:{type(exc).__name__}:{exc}")
            if self._handle is not None:
                self._handle.close()
            raise
        self._thread = threading.Thread(
            target=self._run,
            name="tacdiffusion-episode-batch-fsync-10",
            daemon=True,
        )
        self._thread.start()

    def _update_stall(self) -> None:
        now = self._clock()
        with self._state_lock:
            work_pending = self._inflight_rows > 0 or self.spool.depth > 0
            if not work_pending:
                self._work_pending_since = None
                return
            if self._work_pending_since is None:
                self._work_pending_since = now
            reference = max(self._last_progress, self._work_pending_since)
        if now - reference > self.stall_timeout_s:
            self._fault.latch("writer_stall")

    def _write_batch(self, batch: Sequence[EpisodeFrameV2]) -> None:
        if not batch:
            return
        payload = b"".join(_line(frame.as_json()) for frame in batch)
        written = self._handle.write(payload)
        if written != len(payload):
            raise OSError("episode batch write was incomplete")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        with self._state_lock:
            self._durable_rows += len(batch)
            self._last_progress = self._clock()
            self._work_pending_since = None

    def _run(self) -> None:
        pending: list[EpisodeFrameV2] = []
        try:
            while True:
                frame = self.spool.get(0.010)
                if frame is None:
                    if self._stop_requested.is_set() and self.spool.depth == 0:
                        if pending:
                            self._write_batch(pending)
                            for _ in pending:
                                self.spool.task_done()
                            pending.clear()
                            with self._state_lock:
                                self._inflight_rows = 0
                        break
                    self._update_stall()
                    continue
                pending.append(frame)
                with self._state_lock:
                    self._inflight_rows = len(pending)
                    self._max_unsealed_tail = max(
                        self._max_unsealed_tail,
                        len(pending) % self.batch_size,
                    )
                self._update_stall()
                if len(pending) == self.batch_size:
                    self._write_batch(pending)
                    for _ in pending:
                        self.spool.task_done()
                    pending.clear()
                    with self._state_lock:
                        self._inflight_rows = 0
        except Exception as exc:
            self._fault.latch(f"writer_error:{type(exc).__name__}:{exc}")
            self._stop_requested.set()
        finally:
            if self._handle is not None and not self._handle.closed:
                try:
                    self._handle.close()
                except Exception as exc:
                    self._fault.latch(f"writer_close:{type(exc).__name__}:{exc}")

    def poll_fault(self) -> str | None:
        self._update_stall()
        return self.spool.fault or self._fault.reason

    @property
    def durable_rows(self) -> int:
        with self._state_lock:
            return self._durable_rows

    @property
    def unsealed_tail(self) -> int:
        with self._state_lock:
            # "Tail" is the incomplete durability quantum, not the full
            # undurable backlog. Complete seal separately requires
            # durable_rows == enqueued_rows and an empty spool.
            return (self.spool.depth + self._inflight_rows) % self.batch_size

    def close(self, timeout_s: float = 5.0) -> None:
        self._stop_requested.set()
        if self._thread is not None:
            deadline = self._clock() + timeout_s
            while self.spool.unfinished_tasks and self._clock() < deadline:
                if self._fault.reason is not None:
                    break
                time.sleep(0.001)
            self._thread.join(max(0.0, deadline - self._clock()))
            if self._thread.is_alive():
                self._fault.latch("writer_close_timeout")
        if (
            self._handle is not None
            and not self._handle.closed
            and (self._thread is None or not self._thread.is_alive())
        ):
            self._handle.close()

    def seal(self) -> dict[str, Any]:
        self.close()
        fault = self.poll_fault()
        if fault is not None:
            raise RecorderError(fault)
        if self.spool.unfinished_tasks:
            raise RecorderError("writer_has_unsealed_rows")
        if not self.artifact_path.is_file():
            raise RecorderError("episode_artifact_missing")
        artifact_hash = hashlib.sha256(self.artifact_path.read_bytes()).hexdigest()
        manifest: dict[str, Any] = {
            "schema": EPISODE_ARTIFACT_SCHEMA,
            "format_version": 2,
            "episode_id": self.episode_id,
            "artifact": self.artifact_path.name,
            "artifact_sha256": artifact_hash,
            "row_count": self.durable_rows,
            "durability_mode": DURABILITY_MODE,
            "spool_capacity": self.spool.capacity,
            "batch_size": self.batch_size,
            "max_unsealed_tail": MAX_UNSEALED_TAIL,
            "metadata": self.metadata,
            "complete_seal": True,
            "tamper_free": True,
            "overflowed": False,
            "stalled": False,
            "dropped_rows": 0,
        }
        _atomic_create_json(self.manifest_path, manifest)
        self._sealed = True
        self._manifest_written = True
        self._tamper_free = True
        return manifest

    def health(self, *, validate_tamper: bool = False) -> RecorderHealth:
        self._update_stall()
        fault = self.spool.fault or self._fault.reason
        tamper_free = self._tamper_free
        if self._sealed and validate_tamper:
            try:
                validate_sealed_episode_manifest(self.artifact_path, self.manifest_path)
            except (OSError, ValueError, json.JSONDecodeError):
                tamper_free = False
        return RecorderHealth(
            capacity=self.spool.capacity,
            queue_depth=self.spool.depth,
            enqueued_rows=self.spool.enqueued_rows,
            durable_rows=self.durable_rows,
            rejected_rows=self.spool.rejected_rows,
            dropped_rows=0,
            overflowed=self.spool.fault == "spool_overflow",
            stalled=self._fault.reason == "writer_stall",
            writer_error=(
                self._fault.reason
                if self._fault.reason is not None and self._fault.reason != "writer_stall"
                else None
            ),
            fault=fault,
            durability_mode=DURABILITY_MODE,
            unsealed_tail=self.unsealed_tail,
            max_unsealed_tail=max(self._max_unsealed_tail, self.unsealed_tail),
            sealed=self._sealed,
            manifest_written=self._manifest_written,
            tamper_free=tamper_free,
        )


def _atomic_create_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"receipt already exists: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(_line(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


class EpisodeRecorder:
    """Composition of the bounded producer spool and background sealer."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        episode_id: str,
        metadata: Mapping[str, Any] | None = None,
        capacity: int = SPOOL_CAPACITY,
        stall_timeout_s: float = 0.250,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.episode_id = episode_id
        self.spool = BoundedEpisodeSpool(capacity=capacity)
        self.artifact_path = self.output_dir / "episode_v2.jsonl"
        self.manifest_path = self.output_dir / "episode_v2.manifest.json"
        self.health_path = self.output_dir / "recorder_health.json"
        self.sealer = BatchFsync10Sealer(
            self.spool,
            self.artifact_path,
            self.manifest_path,
            episode_id=episode_id,
            metadata=metadata,
            stall_timeout_s=stall_timeout_s,
        )
        self._producer_lock = threading.Lock()
        self._started = False
        self._accepting = True
        self._next_sample_index = 0
        self._last_control_time = -math.inf
        self._last_device_time = -math.inf
        self._last_host_time = -math.inf
        self._last_external_batch_id: int | None = None
        self._last_external_batch_host: float | None = None
        self._last_external_sample_index: int | None = None
        self._external_held_ticks = 0

    def start(self) -> None:
        if self.health_path.exists():
            raise FileExistsError("episode recorder health receipt already exists")
        self.sealer.start()
        self._started = True

    def enqueue(self, frame: EpisodeFrameV2) -> bool:
        """Producer seam: bounded enqueue only; no JSON or filesystem calls."""

        if not self._started:
            raise RuntimeError("episode recorder is not started")
        with self._producer_lock:
            if not self._accepting:
                return False
            control_strict = frame.control_time_strict and frame.control_time_s > self._last_control_time
            device_monotonic = (
                frame.external_device_time_s is None
                or frame.external_device_time_s >= self._last_device_time - 1e-12
            )
            host_monotonic = (
                frame.external_host_visible_time_s is None
                or frame.external_host_visible_time_s >= self._last_host_time - 1e-12
            )
            same_batch_arrival = True
            if (
                frame.external_batch_id is not None
                and frame.external_batch_id == self._last_external_batch_id
            ):
                same_batch_arrival = (
                    frame.external_host_visible_time_s is not None
                    and self._last_external_batch_host is not None
                    and math.isclose(
                        frame.external_host_visible_time_s,
                        self._last_external_batch_host,
                        rel_tol=0.0,
                        abs_tol=1e-12,
                    )
                )
            if (
                frame.external_sample_index is not None
                and frame.external_sample_index == self._last_external_sample_index
            ):
                self._external_held_ticks += 1
                external_hold = True
            elif frame.external_sample_index is not None:
                self._external_held_ticks = 0
                external_hold = False
            else:
                external_hold = False
            accepted = _with_recorder_metadata(
                frame,
                sample_index=self._next_sample_index,
                control_time_strict=control_strict,
                external_lineage_valid=(
                    frame.external_lineage_valid
                    and device_monotonic
                    and host_monotonic
                    and same_batch_arrival
                ),
                external_hold=external_hold,
                external_held_ticks=self._external_held_ticks,
                device_age_samples=(
                    self._external_held_ticks * DEVICE_SAMPLES_PER_CONTROL_TICK
                    if frame.external_sample_index is not None
                    else frame.device_age_samples
                ),
            )
            if not self.spool.enqueue(accepted):
                return False
            self._next_sample_index += 1
            self._last_control_time = frame.control_time_s
            if accepted.external_lineage_valid:
                if frame.external_device_time_s is not None:
                    self._last_device_time = frame.external_device_time_s
                if frame.external_host_visible_time_s is not None:
                    self._last_host_time = frame.external_host_visible_time_s
                if frame.external_batch_id is not None:
                    self._last_external_batch_id = frame.external_batch_id
                    self._last_external_batch_host = (
                        frame.external_host_visible_time_s
                    )
                if frame.external_sample_index is not None:
                    self._last_external_sample_index = frame.external_sample_index
            return True

    def poll_fault(self) -> str | None:
        return self.sealer.poll_fault()

    def require_healthy(self) -> None:
        fault = self.poll_fault()
        if fault is not None:
            raise RecorderError(fault)

    def health(self, *, validate_tamper: bool = False) -> RecorderHealth:
        return self.sealer.health(validate_tamper=validate_tamper)

    def close(self, *, seal: bool = True) -> dict[str, Any] | None:
        self._accepting = False
        if not self._started:
            return None
        if seal:
            try:
                return self.sealer.seal()
            finally:
                self._write_health_receipt()
        self.sealer.close()
        self._write_health_receipt()
        return None

    def _write_health_receipt(self) -> dict[str, Any]:
        health = self.health()
        payload: dict[str, Any] = {
            "schema": RECORDER_HEALTH_SCHEMA,
            "episode_id": self.episode_id,
            "health": health.as_json(),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        payload["health_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        _atomic_create_json(self.health_path, payload)
        return payload

    def __enter__(self) -> "EpisodeRecorder":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close(seal=True)


def _load_jsonl(path: Path) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    data = path.read_bytes()
    if not data or not data.endswith(b"\n"):
        raise ValueError("episode artifact has an incomplete final line")
    rows = data.splitlines()
    parsed = [json.loads(row) for row in rows]
    if not parsed or not isinstance(parsed[0], dict):
        raise ValueError("episode artifact header is invalid")
    return parsed[0], tuple(row for row in parsed[1:] if isinstance(row, dict))


def read_episode_artifact(path: str | Path) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """Read v2 writes and expose v1 rows without rewriting the legacy artifact."""

    header, rows = _load_jsonl(Path(path))
    schema = header.get("schema")
    if schema not in {EPISODE_ARTIFACT_SCHEMA, "ur10e_tacdiffusion_expert_episode/v1"}:
        raise ValueError("unsupported episode artifact schema")
    return header, rows


def read_recorder_health(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != RECORDER_HEALTH_SCHEMA:
        raise ValueError("recorder health receipt schema mismatch")
    if not isinstance(payload.get("health"), dict):
        raise ValueError("recorder health receipt payload is invalid")
    return payload


def validate_sealed_episode_manifest(
    artifact_path: str | Path,
    manifest_path: str | Path,
) -> dict[str, Any]:
    artifact = Path(artifact_path)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if manifest.get("artifact") != artifact.name:
        raise ValueError("episode manifest artifact identity mismatch")
    if manifest.get("artifact_sha256") != hashlib.sha256(artifact.read_bytes()).hexdigest():
        raise ValueError("episode manifest artifact hash mismatch")
    header, rows = read_episode_artifact(artifact)
    if manifest.get("row_count") != len(rows):
        raise ValueError("episode manifest row count mismatch")
    if not manifest.get("complete_seal") or not manifest.get("tamper_free"):
        raise ValueError("episode manifest is not a complete seal")
    if header.get("schema") not in {
        EPISODE_ARTIFACT_SCHEMA,
        "ur10e_tacdiffusion_expert_episode/v1",
    }:
        raise ValueError("episode header schema mismatch")
    return manifest


__all__ = [
    "ACTION_DIMENSION",
    "BATCH_SIZE",
    "BoundedEpisodeSpool",
    "DURABILITY_MODE",
    "EPISODE_ARTIFACT_SCHEMA",
    "EPISODE_FRAME_SCHEMA",
    "RECORDER_HEALTH_SCHEMA",
    "EpisodeFrame",
    "EpisodeFrameV2",
    "EpisodeRecorder",
    "MAX_UNSEALED_TAIL",
    "OBSERVATION_DIMENSION",
    "RecorderError",
    "RecorderHealth",
    "SPOOL_CAPACITY",
    "BatchFsync10Sealer",
    "read_episode_artifact",
    "read_recorder_health",
    "validate_sealed_episode_manifest",
]
