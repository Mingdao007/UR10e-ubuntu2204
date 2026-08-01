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

from .contracts import CONTROL_RATE_HZ, RAW_WRENCH_RATE_HZ, DynamicsReceipt, DynamicsSample
from .episode_composition import ActionLabel, ActionLabelContext


EPISODE_ARTIFACT_SCHEMA_V2 = "ur10e_tacdiffusion_episode_artifact/v2"
EPISODE_FRAME_SCHEMA_V2 = "ur10e_tacdiffusion_episode_frame/v2"
EPISODE_ARTIFACT_SCHEMA = "ur10e_tacdiffusion_episode_artifact/v3"
EPISODE_FRAME_SCHEMA = "ur10e_tacdiffusion_episode_frame/v3"
EPISODE_ARTIFACT_SCHEMA_V3 = EPISODE_ARTIFACT_SCHEMA
EPISODE_FRAME_SCHEMA_V3 = EPISODE_FRAME_SCHEMA
EPISODE_TAIL_SCHEMA = "ur10e_tacdiffusion_episode_tail/v1"
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


def _is_sha256(value: object) -> bool:
    text = str(value)
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _semantic_identity_status(metadata: Mapping[str, Any]) -> tuple[bool, str | None]:
    context = metadata.get("semantic_context")
    if not isinstance(context, Mapping):
        return False, None
    hashes = context.get("hash_identities")
    if not isinstance(hashes, Mapping) or not hashes:
        return False, None
    enabled = all(_is_sha256(value) for value in hashes.values())
    fingerprint = context.get("semantic_context_fingerprint_sha256")
    return enabled and _is_sha256(fingerprint), str(fingerprint) if _is_sha256(fingerprint) else None


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
        payload = {
            field.name: getattr(self, field.name)
            for field in fields(EpisodeFrameV2)
        }
        payload["schema"] = EPISODE_FRAME_SCHEMA_V2
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


@dataclass(frozen=True)
class EpisodeFrameV3(EpisodeFrameV2):
    """Canonical row with typed dynamics/action receipts and identity binding."""

    dynamics_sample: DynamicsSample | None = None
    dynamics_receipt: DynamicsReceipt | None = None
    action_label_context: ActionLabelContext | None = None
    action_label: ActionLabel | None = None
    identity_enabled: bool = True
    semantic_context_fingerprint_sha256: str | None = None
    # These receipts are optional for compatibility rows, but the canonical
    # offline campaign fills both fields.  Keeping them on the v3 row makes
    # the current/previous observation and the sampled reference auditable
    # without inventing a parallel artifact format.
    observation_receipt: Mapping[str, Any] | None = None
    reference_receipt: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.identity_enabled, bool):
            raise ValueError("v3 identity_enabled must be boolean")
        if (self.dynamics_sample is None) != (self.dynamics_receipt is None):
            raise ValueError("v3 dynamics sample and receipt must be paired")
        if self.dynamics_sample is not None and self.dynamics_receipt is not None:
            if not isinstance(self.dynamics_sample, DynamicsSample) or not isinstance(self.dynamics_receipt, DynamicsReceipt):
                raise ValueError("v3 dynamics fields must use typed contracts")
            self.dynamics_receipt.validate_against(self.dynamics_sample)
        if (self.action_label_context is None) != (self.action_label is None):
            raise ValueError("v3 action context and label must be paired")
        if self.action_label_context is not None and self.action_label is not None:
            if not isinstance(self.action_label_context, ActionLabelContext) or not isinstance(self.action_label, ActionLabel):
                raise ValueError("v3 action fields must use typed contracts")
            if (
                self.action_label_context.sequence != self.control_sequence
                or not math.isclose(self.action_label_context.timestamp_s, self.control_time_s, rel_tol=0.0, abs_tol=1e-12)
                or self.action_label_context.frame_id != self.action_label.frame_id
                or self.action_label.sequence != self.action_label_context.sequence
                or not math.isclose(self.action_label.timestamp_s, self.action_label_context.timestamp_s, rel_tol=0.0, abs_tol=1e-12)
            ):
                raise ValueError("v3 action context/label identity mismatch")
            if self.action_label.available and tuple(self.expert_action_12d) != self.action_label.expert_action_12d:
                raise ValueError("v3 expert action does not match typed action label")
            if self.semantic_context_fingerprint_sha256 is not None and self.action_label_context.semantic_context_fingerprint_sha256 is not None:
                if self.action_label_context.semantic_context_fingerprint_sha256 != self.semantic_context_fingerprint_sha256:
                    raise ValueError("v3 semantic context fingerprint mismatch")
        if self.semantic_context_fingerprint_sha256 is not None:
            if len(self.semantic_context_fingerprint_sha256) != 64 or any(
                char not in "0123456789abcdef" for char in self.semantic_context_fingerprint_sha256
            ):
                raise ValueError("v3 semantic context fingerprint is invalid")
        for name in ("observation_receipt", "reference_receipt"):
            receipt = getattr(self, name)
            if receipt is not None:
                if not isinstance(receipt, Mapping):
                    raise ValueError(f"v3 {name} must be a mapping")
                if not str(receipt.get("schema", "")).strip():
                    raise ValueError(f"v3 {name} schema is required")

    @classmethod
    def from_v2(cls, frame: EpisodeFrameV2) -> "EpisodeFrameV3":
        if not isinstance(frame, EpisodeFrameV2):
            raise TypeError("v3 compatibility conversion requires EpisodeFrameV2")
        # V2 has already completed the expensive vector validation.  Copying
        # its frozen fields directly keeps the compatibility adapter out of
        # the producer's 500 Hz budget; v3 typed rows still validate normally
        # at construction time.
        accepted = object.__new__(cls)
        accepted.__dict__.update(frame.__dict__)
        accepted.__dict__.update(
            dynamics_sample=None,
            dynamics_receipt=None,
            action_label_context=None,
            action_label=None,
            identity_enabled=False,
            semantic_context_fingerprint_sha256=None,
        )
        return accepted

    @property
    def source_identity_enabled(self) -> bool:
        return self.identity_enabled

    @property
    def typed_receipts_valid(self) -> bool:
        return bool(
            self.identity_enabled
            and self.semantic_context_fingerprint_sha256 is not None
            and self.dynamics_sample is not None
            and self.dynamics_receipt is not None
            and self.dynamics_receipt.valid
            and self.action_label_context is not None
            and self.action_label is not None
            and self.action_label.available
            and not self.action_label.shadow_only
        )

    @property
    def row_valid(self) -> bool:
        return bool(super().row_valid and self.typed_receipts_valid)

    def as_json(self) -> dict[str, Any]:
        payload = super().as_json()
        payload["schema"] = EPISODE_FRAME_SCHEMA
        payload["dynamics_sample"] = None if self.dynamics_sample is None else self.dynamics_sample.as_json()
        payload["dynamics_receipt"] = None if self.dynamics_receipt is None else self.dynamics_receipt.as_json()
        payload["action_label_context"] = None if self.action_label_context is None else self.action_label_context.as_json()
        payload["action_label"] = None if self.action_label is None else self.action_label.as_json()
        payload["identity_enabled"] = self.identity_enabled
        payload["semantic_context_fingerprint_sha256"] = self.semantic_context_fingerprint_sha256
        payload["observation_receipt"] = (
            None if self.observation_receipt is None else dict(self.observation_receipt)
        )
        payload["reference_receipt"] = (
            None if self.reference_receipt is None else dict(self.reference_receipt)
        )
        payload["typed_receipts_valid"] = self.typed_receipts_valid
        payload["typed_receipts"] = {
            "dynamics": payload["dynamics_receipt"],
            "action_label": payload["action_label"],
        }
        # The row seal is calculated only after recorder-owned metadata has
        # been applied.  It therefore binds the exact serialized v3 row and
        # remains compatible with the bounded background sealer.
        payload["row_seal_sha256"] = hashlib.sha256(
            _line(payload)
        ).hexdigest()
        return payload


# The canonical short name follows the current writer version.  Explicit
# ``EpisodeFrameV2`` remains available for compatibility adapters/readers.
EpisodeFrame = EpisodeFrameV3


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
    frame_type = type(frame)
    accepted = object.__new__(frame_type)
    replacements = {
        "sample_index": sample_index,
        "control_time_strict": bool(control_time_strict),
        "external_lineage_valid": bool(external_lineage_valid),
        "external_hold": bool(external_hold),
        "external_held_ticks": external_held_ticks,
        "device_age_samples": device_age_samples,
    }
    accepted.__dict__.update(frame.__dict__)
    accepted.__dict__.update(replacements)
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
        self.identity_enabled, self.semantic_context_fingerprint_sha256 = _semantic_identity_status(self.metadata)
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
                "format_version": 3,
                "frame_schema": EPISODE_FRAME_SCHEMA,
                "tail_schema": EPISODE_TAIL_SCHEMA,
                "episode_id": self.episode_id,
                "durability_mode": DURABILITY_MODE,
                "spool_capacity": self.spool.capacity,
                "batch_size": self.batch_size,
                "max_unsealed_tail": MAX_UNSEALED_TAIL,
                "identity_enabled": self.identity_enabled,
                "semantic_context_fingerprint_sha256": self.semantic_context_fingerprint_sha256,
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
        canonical_frames: list[EpisodeFrameV3] = []
        for frame in batch:
            canonical = frame if isinstance(frame, EpisodeFrameV3) else EpisodeFrameV3.from_v2(frame)
            if not self.identity_enabled or self.semantic_context_fingerprint_sha256 is None:
                bound = object.__new__(EpisodeFrameV3)
                for field in fields(EpisodeFrameV3):
                    object.__setattr__(bound, field.name, getattr(canonical, field.name))
                object.__setattr__(bound, "identity_enabled", False)
                object.__setattr__(bound, "semantic_context_fingerprint_sha256", None)
                canonical = bound
            elif canonical.semantic_context_fingerprint_sha256 is None:
                bound = object.__new__(EpisodeFrameV3)
                for field in fields(EpisodeFrameV3):
                    object.__setattr__(bound, field.name, getattr(canonical, field.name))
                object.__setattr__(bound, "semantic_context_fingerprint_sha256", self.semantic_context_fingerprint_sha256)
                canonical = bound
            elif canonical.semantic_context_fingerprint_sha256 != self.semantic_context_fingerprint_sha256:
                bound = object.__new__(EpisodeFrameV3)
                for field in fields(EpisodeFrameV3):
                    object.__setattr__(bound, field.name, getattr(canonical, field.name))
                object.__setattr__(bound, "identity_enabled", False)
                canonical = bound
            canonical_frames.append(canonical)
        canonical_batch = tuple(canonical_frames)
        payload = b"".join(_line(frame.as_json()) for frame in canonical_batch)
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

    def _append_v3_tail(self) -> dict[str, Any]:
        data = self.artifact_path.read_bytes()
        if not data or not data.endswith(b"\n"):
            raise RecorderError("episode_artifact_has_incomplete_tail")
        lines = data.splitlines(keepends=True)
        if not lines:
            raise RecorderError("episode_artifact_header_missing")
        try:
            header = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise RecorderError("episode_artifact_header_invalid") from exc
        if not isinstance(header, dict) or header.get("schema") != EPISODE_ARTIFACT_SCHEMA:
            raise RecorderError("episode_artifact_schema_mismatch")
        row_lines = lines[1:]
        if row_lines:
            try:
                last_payload = json.loads(row_lines[-1])
            except json.JSONDecodeError as exc:
                raise RecorderError("episode_artifact_tail_is_torn") from exc
            if isinstance(last_payload, dict) and last_payload.get("schema") == EPISODE_TAIL_SCHEMA:
                raise RecorderError("episode_artifact_tail_already_present")
        parsed_rows: list[dict[str, Any]] = []
        for index, raw in enumerate(row_lines):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RecorderError(f"episode_artifact_row_{index}_invalid") from exc
            if not isinstance(payload, dict) or payload.get("schema") != EPISODE_FRAME_SCHEMA:
                raise RecorderError(f"episode_artifact_row_{index}_schema_mismatch")
            parsed_rows.append(payload)
        if len(parsed_rows) != self.durable_rows:
            raise RecorderError("episode_artifact_durable_row_count_mismatch")
        content_bytes = b"".join(row_lines)
        content_sha256 = hashlib.sha256(content_bytes).hexdigest()
        last = parsed_rows[-1] if parsed_rows else None
        tail_unsigned: dict[str, Any] = {
            "schema": EPISODE_TAIL_SCHEMA,
            "format_version": 3,
            "complete_tail": True,
            "row_count": len(parsed_rows),
            "last_sample_index": None if last is None else last.get("sample_index"),
            "last_control_sequence": None if last is None else last.get("control_sequence"),
            "content_sha256": content_sha256,
        }
        tail_sha256 = hashlib.sha256(_line(tail_unsigned)).hexdigest()
        tail = tail_unsigned | {"tail_sha256": tail_sha256}
        with self.artifact_path.open("ab") as handle:
            payload = _line(tail)
            if handle.write(payload) != len(payload):
                raise RecorderError("episode_artifact_tail_write_incomplete")
            handle.flush()
            os.fsync(handle.fileno())
        return {
            "content_sha256": content_sha256,
            "tail_sha256": tail_sha256,
            "row_count": len(parsed_rows),
            "last_sample_index": tail["last_sample_index"],
            "last_control_sequence": tail["last_control_sequence"],
            "header_sha256": hashlib.sha256(lines[0]).hexdigest(),
        }

    def seal(self) -> dict[str, Any]:
        self.close()
        fault = self.poll_fault()
        if fault is not None:
            raise RecorderError(fault)
        if self.spool.unfinished_tasks:
            raise RecorderError("writer_has_unsealed_rows")
        if not self.artifact_path.is_file():
            raise RecorderError("episode_artifact_missing")
        tail = self._append_v3_tail()
        artifact_hash = hashlib.sha256(self.artifact_path.read_bytes()).hexdigest()
        manifest: dict[str, Any] = {
            "schema": EPISODE_ARTIFACT_SCHEMA,
            "format_version": 3,
            "episode_id": self.episode_id,
            "artifact": self.artifact_path.name,
            "artifact_sha256": artifact_hash,
            "row_count": self.durable_rows,
            "durability_mode": DURABILITY_MODE,
            "spool_capacity": self.spool.capacity,
            "batch_size": self.batch_size,
            "max_unsealed_tail": MAX_UNSEALED_TAIL,
            "frame_schema": EPISODE_FRAME_SCHEMA,
            "tail_schema": EPISODE_TAIL_SCHEMA,
            "header_sha256": tail["header_sha256"],
            "content_sha256": tail["content_sha256"],
            "tail_sha256": tail["tail_sha256"],
            "last_sample_index": tail["last_sample_index"],
            "last_control_sequence": tail["last_control_sequence"],
            "complete_tail": True,
            "identity_enabled": self.identity_enabled,
            "semantic_context_fingerprint_sha256": self.semantic_context_fingerprint_sha256,
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
        semantic_context: Any | None = None,
        capacity: int = SPOOL_CAPACITY,
        stall_timeout_s: float = 0.250,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.episode_id = episode_id
        normalized_metadata = dict(metadata or {})
        if semantic_context is not None:
            if not hasattr(semantic_context, "as_metadata"):
                raise TypeError("semantic_context must expose as_metadata()")
            normalized_metadata["semantic_context"] = semantic_context.as_metadata()
        self.spool = BoundedEpisodeSpool(capacity=capacity)
        self.artifact_path = self.output_dir / "episode_v3.jsonl"
        self.manifest_path = self.output_dir / "episode_v3.manifest.json"
        self.health_path = self.output_dir / "recorder_health.json"
        self.sealer = BatchFsync10Sealer(
            self.spool,
            self.artifact_path,
            self.manifest_path,
            episode_id=episode_id,
            metadata=normalized_metadata,
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
        if (
            (self.output_dir / "episode_v2.jsonl").exists()
            or (self.output_dir / "episode_v2.manifest.json").exists()
        ):
            raise FileExistsError("legacy episode artifact or manifest already exists")
        self.sealer.start()
        self._started = True

    def enqueue(self, frame: EpisodeFrameV2) -> bool:
        """Producer seam: bounded enqueue only; no JSON or filesystem calls."""

        if not self._started:
            raise RuntimeError("episode recorder is not started")
        if isinstance(frame, EpisodeFrameV2) and not isinstance(frame, EpisodeFrameV3):
            frame = EpisodeFrameV3.from_v2(frame)
        if not isinstance(frame, EpisodeFrameV3):
            raise TypeError("canonical EpisodeRecorder requires EpisodeFrameV3 or an EpisodeFrameV2 adapter")
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
    lines = data.splitlines(keepends=True)
    try:
        parsed = [json.loads(row) for row in lines]
    except json.JSONDecodeError as exc:
        raise ValueError("episode artifact contains invalid JSON") from exc
    if not parsed or not isinstance(parsed[0], dict):
        raise ValueError("episode artifact header is invalid")
    header = parsed[0]
    if header.get("schema") == EPISODE_ARTIFACT_SCHEMA:
        if (
            header.get("format_version") != 3
            or header.get("frame_schema") != EPISODE_FRAME_SCHEMA
            or header.get("tail_schema") != EPISODE_TAIL_SCHEMA
        ):
            raise ValueError("episode v3 header version/schema mismatch")
        integrity = _validate_v3_tail_lines(lines, parsed)
        return header, tuple(integrity["rows"])
    return header, tuple(row for row in parsed[1:] if isinstance(row, dict))


def _validate_v3_tail_lines(
    lines: Sequence[bytes],
    parsed: Sequence[object],
) -> dict[str, Any]:
    if len(lines) < 2 or len(parsed) < 2:
        raise ValueError("episode v3 is missing its complete tail")
    tail = parsed[-1]
    if not isinstance(tail, dict) or tail.get("schema") != EPISODE_TAIL_SCHEMA:
        raise ValueError("episode v3 tail is missing or incomplete")
    rows = parsed[1:-1]
    if any(not isinstance(row, dict) or row.get("schema") != EPISODE_FRAME_SCHEMA for row in rows):
        raise ValueError("episode v3 row schema mismatch")
    for index, row in enumerate(rows):
        supplied_row_seal = row.get("row_seal_sha256")
        if supplied_row_seal is None:
            # Existing v3 artifacts predate the row-level seal.  They remain
            # readable, while all new canonical rows carry the seal below.
            continue
        if not _is_sha256(supplied_row_seal):
            raise ValueError(f"episode v3 row {index} seal is invalid")
        unsigned_row = dict(row)
        unsigned_row.pop("row_seal_sha256", None)
        expected_row_seal = hashlib.sha256(_line(unsigned_row)).hexdigest()
        if supplied_row_seal != expected_row_seal:
            raise ValueError(f"episode v3 row {index} seal mismatch")
    row_lines = lines[1:-1]
    content_sha256 = hashlib.sha256(b"".join(row_lines)).hexdigest()
    if tail.get("content_sha256") != content_sha256:
        raise ValueError("episode v3 content identity mismatch")
    if tail.get("row_count") != len(rows):
        raise ValueError("episode v3 tail row count mismatch")
    expected_tail = dict(tail)
    supplied_tail_hash = expected_tail.pop("tail_sha256", None)
    if not isinstance(supplied_tail_hash, str) or supplied_tail_hash != hashlib.sha256(_line(expected_tail)).hexdigest():
        raise ValueError("episode v3 tail identity mismatch")
    last = rows[-1] if rows else None
    if tail.get("last_sample_index") != (None if last is None else last.get("sample_index")):
        raise ValueError("episode v3 tail sample identity mismatch")
    if tail.get("last_control_sequence") != (None if last is None else last.get("control_sequence")):
        raise ValueError("episode v3 tail sequence identity mismatch")
    if tail.get("complete_tail") is not True:
        raise ValueError("episode v3 tail is not complete")
    return {
        "rows": tuple(rows),
        "tail": tail,
        "content_sha256": content_sha256,
        "tail_sha256": supplied_tail_hash,
        "header_sha256": hashlib.sha256(lines[0]).hexdigest(),
        "last_sample_index": tail.get("last_sample_index"),
        "last_control_sequence": tail.get("last_control_sequence"),
    }


def read_episode_artifact(path: str | Path) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """Read v3 writes and expose v1/v2 rows without rewriting legacy artifacts."""

    header, rows = _load_jsonl(Path(path))
    schema = header.get("schema")
    if schema not in {
        EPISODE_ARTIFACT_SCHEMA,
        EPISODE_ARTIFACT_SCHEMA_V2,
        "ur10e_tacdiffusion_expert_episode/v1",
    }:
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
    if header.get("schema") == EPISODE_ARTIFACT_SCHEMA:
        lines = artifact.read_bytes().splitlines(keepends=True)
        parsed = [json.loads(line) for line in lines]
        integrity = _validate_v3_tail_lines(lines, parsed)
        for name in (
            "header_sha256",
            "content_sha256",
            "tail_sha256",
            "last_sample_index",
            "last_control_sequence",
        ):
            if manifest.get(name) != integrity[name]:
                raise ValueError(f"episode v3 manifest {name} mismatch")
        if manifest.get("complete_tail") is not True or manifest.get("format_version") != 3:
            raise ValueError("episode v3 manifest tail is incomplete")
    elif header.get("schema") not in {
        EPISODE_ARTIFACT_SCHEMA_V2,
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
    "EPISODE_ARTIFACT_SCHEMA_V2",
    "EPISODE_ARTIFACT_SCHEMA_V3",
    "EPISODE_FRAME_SCHEMA",
    "EPISODE_FRAME_SCHEMA_V2",
    "EPISODE_FRAME_SCHEMA_V3",
    "EPISODE_TAIL_SCHEMA",
    "RECORDER_HEALTH_SCHEMA",
    "EpisodeFrame",
    "EpisodeFrameV2",
    "EpisodeFrameV3",
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
