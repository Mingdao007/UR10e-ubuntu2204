"""R009 bounded observability primitives and state-20/state-25 seams.

This module is deliberately independent of the historical r008 trace modules.
The producer side keeps a fixed-size rolling ring and uses only non-blocking
queue admission.  The worker owns the only disk handle and writes bounded
JSONL batches.  A shared byte budget accounts for both streams, so one stream
cannot silently consume the other stream's attempt or run allowance.

The classes are offline/integration seams only.  They do not open a controller
connection, start a bridge, issue robot commands, or change a control decision.
"""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol

from .identity import (
    DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG,
    ExecutableBehaviorConfig,
    R009ObservabilityConfig,
    canonical_bytes,
    sha256_bytes,
)


R009_STATE20_SCHEMA = "step5d.autotune-v4/r009-state20-observability-v1"
R009_STATE25_SCHEMA = "step5d.autotune-v4/r009-state25-observability-v1"
R009_AUDIT_SCHEMA = "step5d.autotune-v4/r009-observability-audit-v1"


class R009ObservabilityError(RuntimeError):
    """A malformed or unavailable R009 observability seam."""


class R009DropCause(str, Enum):
    """Typed reasons why a high-rate row was not persisted."""

    QUEUE_OVERFLOW = "queue_overflow"
    SAMPLING = "sampling"
    ATTEMPT_CAP = "attempt_cap"
    RUN_CAP = "run_cap"
    CLOSED = "closed"
    ERROR = "error"


@dataclass(frozen=True)
class R009DropCounter:
    rows: int = 0
    bytes: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"rows": int(self.rows), "bytes": int(self.bytes)}


@dataclass(frozen=True)
class R009BudgetReservation:
    token: int
    attempt_key: str
    size_bytes: int


@dataclass(frozen=True)
class R009BudgetSnapshot:
    attempt_cap_bytes: int
    run_cap_bytes: int
    attempt_used_bytes: Mapping[str, int]
    run_used_bytes: int
    queued_rows: int
    queued_bytes: int
    written_rows: int
    written_bytes: int
    pending_rows: int
    pending_bytes: int
    dropped: Mapping[R009DropCause, R009DropCounter]

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempt_cap_bytes": int(self.attempt_cap_bytes),
            "run_cap_bytes": int(self.run_cap_bytes),
            "attempt_used_bytes": dict(self.attempt_used_bytes),
            "run_used_bytes": int(self.run_used_bytes),
            "queued_rows": int(self.queued_rows),
            "queued_bytes": int(self.queued_bytes),
            "written_rows": int(self.written_rows),
            "written_bytes": int(self.written_bytes),
            "pending_rows": int(self.pending_rows),
            "pending_bytes": int(self.pending_bytes),
            "dropped": {
                cause.value: counter.as_dict()
                for cause, counter in self.dropped.items()
            },
        }


class R009AggregateBudget:
    """Thread-safe aggregate byte budget shared by state20 and state25.

    Reservation happens before queue admission so a row can never make the
    aggregate cap exceed its bound.  Queue overflow or writer failure releases
    the reservation and records its typed drop cause; successfully written
    rows remain charged to the attempt and run totals.
    """

    def __init__(self, *, attempt_cap_bytes: int, run_cap_bytes: int) -> None:
        if (
            isinstance(attempt_cap_bytes, bool)
            or not isinstance(attempt_cap_bytes, int)
            or attempt_cap_bytes <= 0
        ):
            raise ValueError("attempt_cap_bytes must be a positive int")
        if (
            isinstance(run_cap_bytes, bool)
            or not isinstance(run_cap_bytes, int)
            or run_cap_bytes <= 0
        ):
            raise ValueError("run_cap_bytes must be a positive int")
        if attempt_cap_bytes > run_cap_bytes:
            raise ValueError("attempt cap cannot exceed run cap")
        self.attempt_cap_bytes = int(attempt_cap_bytes)
        self.run_cap_bytes = int(run_cap_bytes)
        self._lock = threading.Lock()
        self._attempt_used: dict[str, int] = {}
        self._run_used = 0
        self._next_token = 1
        self._active: dict[int, tuple[str, int, str]] = {}
        self._queued_rows = 0
        self._queued_bytes = 0
        self._written_rows = 0
        self._written_bytes = 0
        self._pending_rows = 0
        self._pending_bytes = 0
        self._dropped: dict[R009DropCause, list[int]] = {
            cause: [0, 0] for cause in R009DropCause
        }

    @staticmethod
    def _key(value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("attempt_key must be a non-empty string")
        return value

    @staticmethod
    def _size(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("row size must be a positive int")
        return int(value)

    def _drop_locked(self, cause: R009DropCause, size_bytes: int) -> None:
        counter = self._dropped[cause]
        counter[0] += 1
        counter[1] += int(size_bytes)

    def drop(self, cause: R009DropCause, *, size_bytes: int = 0) -> None:
        if not isinstance(cause, R009DropCause):
            raise TypeError("drop cause must be R009DropCause")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise ValueError("dropped size must be a non-negative int")
        with self._lock:
            self._drop_locked(cause, int(size_bytes))

    def reserve(
        self, attempt_key: str, size_bytes: int
    ) -> R009BudgetReservation | R009DropCause:
        key = self._key(attempt_key)
        size = self._size(size_bytes)
        with self._lock:
            attempt_used = self._attempt_used.get(key, 0)
            if attempt_used + size > self.attempt_cap_bytes:
                self._drop_locked(R009DropCause.ATTEMPT_CAP, size)
                return R009DropCause.ATTEMPT_CAP
            if self._run_used + size > self.run_cap_bytes:
                self._drop_locked(R009DropCause.RUN_CAP, size)
                return R009DropCause.RUN_CAP
            token = self._next_token
            self._next_token += 1
            self._attempt_used[key] = attempt_used + size
            self._run_used += size
            self._active[token] = (key, size, "reserved")
            return R009BudgetReservation(token, key, size)

    def mark_queued(self, reservation: R009BudgetReservation) -> None:
        with self._lock:
            active = self._active.get(reservation.token)
            if active is None or active[:2] != (reservation.attempt_key, reservation.size_bytes):
                raise R009ObservabilityError("unknown R009 budget reservation")
            if active[2] != "reserved":
                raise R009ObservabilityError("R009 reservation was already queued")
            self._active[reservation.token] = (active[0], active[1], "queued")
            self._queued_rows += 1
            self._queued_bytes += reservation.size_bytes
            self._pending_rows += 1
            self._pending_bytes += reservation.size_bytes

    def cancel(
        self, reservation: R009BudgetReservation, *, cause: R009DropCause
    ) -> None:
        if not isinstance(cause, R009DropCause):
            raise TypeError("cancel cause must be R009DropCause")
        with self._lock:
            active = self._active.pop(reservation.token, None)
            if active is None or active[:2] != (reservation.attempt_key, reservation.size_bytes):
                raise R009ObservabilityError("unknown R009 budget reservation")
            if active[2] == "queued":
                self._pending_rows -= 1
                self._pending_bytes -= reservation.size_bytes
                self._queued_rows -= 1
                self._queued_bytes -= reservation.size_bytes
            self._attempt_used[reservation.attempt_key] -= reservation.size_bytes
            if self._attempt_used[reservation.attempt_key] == 0:
                del self._attempt_used[reservation.attempt_key]
            self._run_used -= reservation.size_bytes
            self._drop_locked(cause, reservation.size_bytes)

    def mark_written(self, reservation: R009BudgetReservation) -> None:
        with self._lock:
            active = self._active.pop(reservation.token, None)
            if active is None or active[:2] != (reservation.attempt_key, reservation.size_bytes):
                raise R009ObservabilityError("unknown R009 budget reservation")
            if active[2] != "queued":
                raise R009ObservabilityError("R009 reservation was not queued")
            self._pending_rows -= 1
            self._pending_bytes -= reservation.size_bytes
            self._written_rows += 1
            self._written_bytes += reservation.size_bytes

    def snapshot(self) -> R009BudgetSnapshot:
        with self._lock:
            return R009BudgetSnapshot(
                attempt_cap_bytes=self.attempt_cap_bytes,
                run_cap_bytes=self.run_cap_bytes,
                attempt_used_bytes=dict(sorted(self._attempt_used.items())),
                run_used_bytes=self._run_used,
                queued_rows=self._queued_rows,
                queued_bytes=self._queued_bytes,
                written_rows=self._written_rows,
                written_bytes=self._written_bytes,
                pending_rows=self._pending_rows,
                pending_bytes=self._pending_bytes,
                dropped={
                    cause: R009DropCounter(rows=values[0], bytes=values[1])
                    for cause, values in self._dropped.items()
                },
            )


class R009RollingRing:
    """Single-producer bounded rolling retention with no filesystem access."""

    def __init__(self, capacity_rows: int) -> None:
        if isinstance(capacity_rows, bool) or not isinstance(capacity_rows, int) or capacity_rows < 1:
            raise ValueError("capacity_rows must be a positive int")
        self.capacity_rows = int(capacity_rows)
        self._rows: deque[dict[str, Any]] = deque(maxlen=self.capacity_rows)
        self._evicted_rows = 0

    def append(self, row: Mapping[str, Any]) -> None:
        if len(self._rows) == self.capacity_rows:
            self._evicted_rows += 1
        self._rows.append(dict(row))

    def snapshot(self, n: int | None = None) -> list[dict[str, Any]]:
        rows = list(self._rows)
        if n is not None:
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                raise ValueError("ring snapshot length must be a non-negative int")
            rows = rows[-n:] if n else []
        return [dict(row) for row in rows]

    @property
    def evicted_rows(self) -> int:
        return int(self._evicted_rows)

    def __len__(self) -> int:
        return len(self._rows)


@dataclass(frozen=True)
class _QueuedRow:
    reservation: R009BudgetReservation
    encoded: bytes
    ready: threading.Event


@dataclass
class _FlushRequest:
    completed: threading.Event


_WRITER_SENTINEL = object()


class R009AsyncBatchWriter:
    """One worker-owned exclusive JSONL handle with bounded nonblocking input."""

    def __init__(
        self,
        path: Path,
        *,
        budget: R009AggregateBudget,
        queue_max_rows: int,
        batch_max_rows: int,
        batch_max_wait_s: float,
        name: str,
    ) -> None:
        if isinstance(queue_max_rows, bool) or not isinstance(queue_max_rows, int) or queue_max_rows < 1:
            raise ValueError("queue_max_rows must be a positive int")
        if isinstance(batch_max_rows, bool) or not isinstance(batch_max_rows, int) or batch_max_rows < 1:
            raise ValueError("batch_max_rows must be a positive int")
        if not math.isfinite(float(batch_max_wait_s)) or batch_max_wait_s <= 0.0:
            raise ValueError("batch_max_wait_s must be positive and finite")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.budget = budget
        self.queue_max_rows = int(queue_max_rows)
        self.batch_max_rows = int(batch_max_rows)
        self.batch_max_wait_s = float(batch_max_wait_s)
        self.name = str(name)
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=self.queue_max_rows)
        # This lock linearizes producer admission with close.  It is held only
        # around state checks and put_nowait; no blocking queue operation is
        # ever performed while observe admission owns it.
        self._admission_lock = threading.Lock()
        self._closed = False
        self._error: str | None = None
        self._sentinel_enqueued = False
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    @property
    def closed(self) -> bool:
        with self._admission_lock:
            return self._closed

    @property
    def error(self) -> str | None:
        with self._admission_lock:
            return self._error

    def _set_error(self, exc: BaseException) -> None:
        with self._admission_lock:
            if self._error is None:
                self._error = f"{type(exc).__name__}: {exc}"

    def enqueue(self, item: _QueuedRow) -> R009DropCause | None:
        if not isinstance(item, _QueuedRow):
            raise TypeError("R009 writer accepts only queued rows")
        # The state check and nonblocking queue insertion are one admission
        # point.  close() cannot put its sentinel between these operations.
        with self._admission_lock:
            if self._closed:
                return R009DropCause.CLOSED
            if self._error is not None:
                return R009DropCause.ERROR
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                return R009DropCause.QUEUE_OVERFLOW
            return None

    def _wait_for_queue_slot(self, deadline: float) -> bool:
        """Wait for a queue notification without holding admission state."""

        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return False
        # Queue.get() notifies not_full after removing an item.  This wait is
        # used only by flush/close; observe uses put_nowait and never waits.
        with self._queue.not_full:
            # The condition already owns Queue.mutex; Queue.full() would try
            # to acquire the same non-reentrant lock a second time.
            if self._queue._qsize() < self._queue.maxsize:
                return True
            self._queue.not_full.wait(timeout=min(remaining, 0.01))
        return time.monotonic() < deadline

    def flush(self, timeout_s: float = 30.0) -> None:
        """Wait for rows accepted before this marker; never called by observe()."""

        timeout = float(timeout_s)
        if not math.isfinite(timeout) or timeout <= 0.0:
            raise ValueError("flush timeout must be positive and finite")
        request = _FlushRequest(threading.Event())
        deadline = time.monotonic() + timeout
        while True:
            with self._admission_lock:
                if self._error is not None or self._closed:
                    return
                try:
                    self._queue.put_nowait(request)
                    break
                except queue.Full:
                    pass
            if not self._wait_for_queue_slot(deadline):
                raise TimeoutError("R009 observability writer flush queue timed out")
        if not request.completed.wait(timeout=max(0.0, deadline - time.monotonic())):
            raise TimeoutError("R009 observability writer flush timed out")

    def close(self, timeout_s: float = 30.0) -> None:
        timeout = float(timeout_s)
        if not math.isfinite(timeout) or timeout <= 0.0:
            raise ValueError("close timeout must be positive and finite")
        with self._admission_lock:
            if self._closed:
                return
            self._closed = True
            should_enqueue_sentinel = (
                self._error is None and self._thread.is_alive()
            )

        deadline = time.monotonic() + timeout
        if should_enqueue_sentinel:
            while True:
                with self._admission_lock:
                    if self._error is not None or not self._thread.is_alive():
                        break
                    try:
                        self._queue.put_nowait(_WRITER_SENTINEL)
                        self._sentinel_enqueued = True
                        break
                    except queue.Full:
                        pass
                if not self._wait_for_queue_slot(deadline):
                    self._set_error(
                        TimeoutError("R009 observability writer close queue timed out")
                    )
                    break

        self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._thread.is_alive():
            self._set_error(TimeoutError("R009 observability writer did not stop"))

    def _complete_control(self, control: Any, handle: Any) -> bool:
        if isinstance(control, _FlushRequest):
            try:
                handle.flush()
            finally:
                control.completed.set()
            return False
        if control is _WRITER_SENTINEL:
            handle.flush()
            return True
        return False

    def _fail_row(self, item: _QueuedRow) -> None:
        try:
            self.budget.cancel(item.reservation, cause=R009DropCause.ERROR)
        except R009ObservabilityError:
            # A worker fault must stay observable but must not escape into the
            # worker thread and become a control/motion failure.
            self._set_error(RuntimeError("R009 budget reservation fault"))

    def _finish_removed_item(self, item: Any) -> None:
        """Finish one item already removed by queue.get exactly once."""

        try:
            if isinstance(item, _QueuedRow):
                self._fail_row(item)
            elif isinstance(item, _FlushRequest):
                item.completed.set()
            # The sentinel and unknown controls have no external waiter.
        finally:
            self._queue.task_done()

    def _drain_after_error(self, pending_control: Any | tuple[Any, ...] | None = None) -> None:
        removed: tuple[Any, ...]
        if pending_control is None:
            removed = ()
        elif isinstance(pending_control, tuple):
            removed = pending_control
        else:
            removed = (pending_control,)
        for item in removed:
            self._finish_removed_item(item)
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            self._finish_removed_item(item)

    def _process_control(self, control: Any, handle: Any) -> bool:
        """Complete one removed control and account for its queue task."""

        try:
            return self._complete_control(control, handle)
        finally:
            self._queue.task_done()

    def _write_batch(self, handle: Any, batch: list[_QueuedRow]) -> None:
        for item in batch:
            # The producer commits the reservation immediately after the
            # nonblocking put.  This tiny handoff prevents the worker from
            # observing a provisional reservation and keeps queued counters
            # exact without making the producer wait.
            item.ready.wait()
        handle.write(b"".join(item.encoded for item in batch))
        handle.flush()
        for item in batch:
            self.budget.mark_written(item.reservation)

    def _run(self) -> None:
        handle: Any | None = None
        pending_control: Any | None = None
        try:
            # Exclusive creation keeps an existing trace immutable.  A caller
            # that wants a new run must supply a new R009 path.
            handle = self.path.open("xb")
            while True:
                item = pending_control
                pending_control = None
                if item is None:
                    try:
                        item = self._queue.get(timeout=self.batch_max_wait_s)
                    except queue.Empty:
                        continue
                if isinstance(item, _QueuedRow):
                    batch = [item]
                    while len(batch) < self.batch_max_rows:
                        try:
                            extra = self._queue.get_nowait()
                        except queue.Empty:
                            break
                        if isinstance(extra, _QueuedRow):
                            batch.append(extra)
                        else:
                            pending_control = extra
                            break
                    try:
                        self._write_batch(handle, batch)
                    except BaseException as exc:
                        self._set_error(exc)
                        for queued in batch:
                            self._fail_row(queued)
                        for _ in batch:
                            self._queue.task_done()
                        self._drain_after_error(pending_control)
                        return
                    for _ in batch:
                        self._queue.task_done()
                    if pending_control is not None:
                        control = pending_control
                        pending_control = None
                        try:
                            if self._process_control(control, handle):
                                return
                        except BaseException as exc:
                            self._set_error(exc)
                            self._drain_after_error()
                            return
                    continue
                try:
                    if self._process_control(item, handle):
                        return
                except BaseException as exc:
                    self._set_error(exc)
                    self._drain_after_error()
                    return
        except BaseException as exc:
            self._set_error(exc)
            self._drain_after_error(pending_control)
        finally:
            if handle is not None:
                try:
                    handle.close()
                except BaseException as exc:
                    self._set_error(exc)


class R009WriterLike(Protocol):
    @property
    def closed(self) -> bool: ...

    @property
    def error(self) -> str | None: ...

    def enqueue(self, item: _QueuedRow) -> R009DropCause | None: ...


def _finite_time(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise R009ObservabilityError("observation time must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise R009ObservabilityError("observation time must be finite")
    return result


def _attempt_key(row: Mapping[str, Any], explicit: str | None) -> str:
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit:
            raise R009ObservabilityError("attempt_key must be a non-empty string")
        return explicit
    attempt_id = row.get("attempt_id")
    if attempt_id is not None:
        if not isinstance(attempt_id, str) or not attempt_id:
            raise R009ObservabilityError("attempt_id must be a non-empty string")
        return f"id:{attempt_id}"
    ordinal = row.get("attempt_ordinal")
    if ordinal is not None:
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise R009ObservabilityError("attempt_ordinal must be a non-negative int")
        return f"ordinal:{ordinal}"
    return "unbound"


def _state(row: Mapping[str, Any]) -> int | None:
    raw = row.get("tp_state", row.get("state"))
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise R009ObservabilityError("trace state must be a non-negative int")
    return int(raw)


def _encode(row: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                dict(row),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise R009ObservabilityError(f"R009 trace row is not finite JSON: {exc}") from exc


@dataclass(frozen=True)
class R009ObserveResult:
    stream: str
    trace_sequence: int
    sampled: bool
    state_transition: bool
    stop_transition: bool
    queued_rows: int
    unavailable_cause: R009DropCause | None = None


class R009TraceStream:
    """Shared producer logic used by the state-20 and state-25 adapters."""

    def __init__(
        self,
        *,
        stream: str,
        schema: str,
        config: R009ObservabilityConfig,
        budget: R009AggregateBudget,
        writer: R009WriterLike | None = None,
    ) -> None:
        if not isinstance(config, R009ObservabilityConfig):
            raise TypeError("R009 trace stream requires typed observability config")
        if not isinstance(budget, R009AggregateBudget):
            raise TypeError("R009 trace stream requires shared R009 budget")
        self.stream = str(stream)
        self.schema = str(schema)
        self.config = config
        self.budget = budget
        self.writer = writer
        self.ring = R009RollingRing(config.ring_capacity_rows)
        self._trace_sequence = 0
        self._last_state: int | None = None
        self._last_attempt_key: str | None = None
        self._last_observed_at_s: float | None = None
        self._next_sample_at_s: float | None = None
        self._sample_period_s = 1.0 / config.disk_sample_hz
        self._persisted_sequences: set[int] = set()
        self._persisted_order: deque[int] = deque(
            maxlen=max(config.ring_capacity_rows * 2, config.stop_tail_rows * 2)
        )
        self._sampled_rows = 0
        self._transition_rows = 0
        self._stop_transition_rows = 0
        self._unavailable_rows = 0

    def _new_attempt(self, attempt_key: str) -> None:
        if attempt_key == self._last_attempt_key:
            return
        self._last_attempt_key = attempt_key
        self._last_state = None
        self._last_observed_at_s = None
        self._next_sample_at_s = None

    def _sample_due(self, timestamp_s: float) -> bool:
        if self._last_observed_at_s is not None and timestamp_s < self._last_observed_at_s:
            raise R009ObservabilityError("R009 observation time regressed")
        self._last_observed_at_s = timestamp_s
        if self._next_sample_at_s is None:
            self._next_sample_at_s = timestamp_s
            sampled = True
        else:
            epsilon = 1e-12
            sampled = timestamp_s + epsilon >= self._next_sample_at_s
        if sampled:
            while self._next_sample_at_s <= timestamp_s + 1e-12:
                self._next_sample_at_s += self._sample_period_s
        return sampled

    def _remember_persisted(self, sequence: int) -> None:
        if sequence in self._persisted_sequences:
            return
        if len(self._persisted_order) == self._persisted_order.maxlen:
            old = self._persisted_order.popleft()
            self._persisted_sequences.discard(old)
        self._persisted_order.append(sequence)
        self._persisted_sequences.add(sequence)

    def _writer_unavailable(self) -> R009DropCause | None:
        if self.writer is None:
            return None
        if self.writer.closed:
            return R009DropCause.CLOSED
        if self.writer.error is not None:
            return R009DropCause.ERROR
        return None

    def _enqueue_candidate(
        self, payload: Mapping[str, Any], *, attempt_key: str, sequence: int
    ) -> R009DropCause | None:
        if self.writer is None:
            return None
        try:
            encoded = _encode(payload)
        except R009ObservabilityError:
            self.budget.drop(R009DropCause.ERROR)
            return R009DropCause.ERROR
        unavailable = self._writer_unavailable()
        if unavailable is not None:
            self.budget.drop(unavailable, size_bytes=len(encoded))
            self._unavailable_rows += 1
            return unavailable
        reservation = self.budget.reserve(attempt_key, len(encoded))
        if isinstance(reservation, R009DropCause):
            return reservation
        handoff = threading.Event()
        outcome = self.writer.enqueue(_QueuedRow(reservation, encoded, handoff))
        if outcome is not None:
            self.budget.cancel(reservation, cause=outcome)
            self._unavailable_rows += int(outcome in {R009DropCause.CLOSED, R009DropCause.ERROR})
            return outcome
        try:
            self.budget.mark_queued(reservation)
        except R009ObservabilityError:
            # An async open/write failure can drain the just-admitted item
            # before this producer gets the GIL again.  Surface that failure
            # as an observation drop instead of leaking an exception into the
            # motion/control caller.
            if self.writer.error is not None:
                self._unavailable_rows += 1
                return R009DropCause.ERROR
            if self.writer.closed:
                self._unavailable_rows += 1
                return R009DropCause.CLOSED
            raise
        handoff.set()
        self._remember_persisted(sequence)
        return None

    def _candidate_payload(
        self, row: Mapping[str, Any], *, retention: str
    ) -> dict[str, Any]:
        payload = dict(row)
        payload["r009_retention"] = retention
        return payload

    def observe(
        self,
        row: Mapping[str, Any],
        *,
        observed_at_s: float | None = None,
        attempt_key: str | None = None,
    ) -> R009ObserveResult:
        """Append one high-rate row without any blocking filesystem operation."""

        if not isinstance(row, Mapping):
            raise TypeError("R009 trace row must be a mapping")
        timestamp = _finite_time(
            row.get("monotonic_s") if observed_at_s is None else observed_at_s
        )
        key = _attempt_key(row, attempt_key)
        self._new_attempt(key)
        self._trace_sequence += 1
        sequence = self._trace_sequence
        state = _state(row)
        transition = state is not None and (
            self._last_state is None or state != self._last_state
        )
        stop_transition = transition and state in self.config.stop_states
        base = dict(row)
        base.update(
            {
                "schema": self.schema,
                "r009_observability_version": self.config.version,
                "r009_stream": self.stream,
                "r009_trace_sequence": sequence,
                "r009_observed_at_s": timestamp,
                "r009_attempt_key": key,
            }
        )
        if state is not None:
            base["r009_state"] = state
        self.ring.append(base)
        sampled = self._sample_due(timestamp)
        if sampled:
            self._sampled_rows += 1
        if transition:
            self._transition_rows += 1
        if stop_transition:
            self._stop_transition_rows += 1
        self._last_state = state

        unavailable = self._writer_unavailable()
        if unavailable is not None:
            try:
                size = len(_encode(self._candidate_payload(base, retention="unavailable")))
            except R009ObservabilityError:
                size = 0
            self.budget.drop(unavailable, size_bytes=size)
            self._unavailable_rows += 1
            return R009ObserveResult(
                self.stream, sequence, sampled, transition, stop_transition, 0, unavailable
            )

        candidate_rows: list[tuple[dict[str, Any], str]] = []
        if stop_transition:
            tail = [
                item
                for item in self.ring.snapshot(self.config.stop_tail_rows)
                if item.get("r009_attempt_key") == key
            ]
            for item in tail:
                retention = "state_transition" if item.get("r009_trace_sequence") == sequence else "stop_tail"
                candidate_rows.append((item, retention))
        elif transition:
            candidate_rows.append((base, "state_transition"))
        elif sampled:
            candidate_rows.append((base, "sample"))
        else:
            try:
                size = len(_encode(self._candidate_payload(base, retention="sampling")))
            except R009ObservabilityError:
                size = 0
            self.budget.drop(R009DropCause.SAMPLING, size_bytes=size)

        queued = 0
        unavailable_cause: R009DropCause | None = None
        for item, retention in candidate_rows:
            raw_sequence = item.get("r009_trace_sequence")
            if isinstance(raw_sequence, int) and raw_sequence in self._persisted_sequences:
                continue
            candidate = self._candidate_payload(item, retention=retention)
            cause = self._enqueue_candidate(candidate, attempt_key=key, sequence=sequence if raw_sequence == sequence else int(raw_sequence))
            if cause is None:
                if self.writer is not None:
                    queued += 1
            elif unavailable_cause is None:
                unavailable_cause = cause

        return R009ObserveResult(
            self.stream,
            sequence,
            sampled,
            transition,
            stop_transition,
            queued,
            unavailable_cause,
        )

    def recent_rows(self, n: int | None = None) -> list[dict[str, Any]]:
        return self.ring.snapshot(n)

    def flush(self, timeout_s: float = 30.0) -> None:
        if self.writer is not None and hasattr(self.writer, "flush"):
            self.writer.flush(timeout_s=timeout_s)  # type: ignore[attr-defined]

    def close(self, timeout_s: float = 30.0) -> None:
        if self.writer is not None and hasattr(self.writer, "close"):
            self.writer.close(timeout_s=timeout_s)  # type: ignore[attr-defined]

    def snapshot(self) -> dict[str, Any]:
        return {
            "stream": self.stream,
            "schema": self.schema,
            "ring_capacity_rows": self.ring.capacity_rows,
            "ring_rows": len(self.ring),
            "ring_evicted_rows": self.ring.evicted_rows,
            "last_trace_sequence": self._trace_sequence,
            "sampled_rows": self._sampled_rows,
            "transition_rows": self._transition_rows,
            "stop_transition_rows": self._stop_transition_rows,
            "unavailable_rows": self._unavailable_rows,
            "disk_enabled": self.writer is not None,
            "writer_closed": bool(self.writer.closed) if self.writer is not None else False,
            "writer_error": self.writer.error if self.writer is not None else None,
        }


class R009State20Trace(R009TraceStream):
    """Thin state-20 adapter over the shared R009 trace stream."""

    def __init__(
        self,
        *,
        config: R009ObservabilityConfig,
        budget: R009AggregateBudget,
        writer: R009WriterLike | None = None,
    ) -> None:
        super().__init__(
            stream="state20",
            schema=R009_STATE20_SCHEMA,
            config=config,
            budget=budget,
            writer=writer,
        )


class R009State25Trace(R009TraceStream):
    """Thin state-25 adapter over the shared R009 trace stream."""

    def __init__(
        self,
        *,
        config: R009ObservabilityConfig,
        budget: R009AggregateBudget,
        writer: R009WriterLike | None = None,
    ) -> None:
        super().__init__(
            stream="state25",
            schema=R009_STATE25_SCHEMA,
            config=config,
            budget=budget,
            writer=writer,
        )


State20R009Trace = R009State20Trace
State25R009Trace = R009State25Trace


def _resolve_config(
    config: R009ObservabilityConfig | ExecutableBehaviorConfig | Mapping[str, Any] | None,
) -> R009ObservabilityConfig:
    if config is None:
        executable = ExecutableBehaviorConfig.from_mapping(DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG)
        resolved = executable.observability
    elif isinstance(config, R009ObservabilityConfig):
        resolved = config
    elif isinstance(config, ExecutableBehaviorConfig):
        resolved = config.observability
    elif isinstance(config, Mapping):
        resolved = R009ObservabilityConfig.from_mapping(config)
    else:
        raise TypeError("R009 observability config must be typed")
    if resolved is None:
        raise R009ObservabilityError(
            "R009 executable behavior config has no identity-bound observability values"
        )
    return resolved


class R009ObservabilitySession:
    """Shared R009 budget plus state-20/state-25 integration seams."""

    def __init__(
        self,
        *,
        config: R009ObservabilityConfig | ExecutableBehaviorConfig | Mapping[str, Any] | None = None,
        run_dir: Path | None = None,
        writers: Mapping[str, R009WriterLike | None] | None = None,
        budget: R009AggregateBudget | None = None,
    ) -> None:
        self.config = _resolve_config(config)
        self.budget = budget or R009AggregateBudget(
            attempt_cap_bytes=self.config.attempt_cap_bytes,
            run_cap_bytes=self.config.run_cap_bytes,
        )
        supplied = {} if writers is None else dict(writers)
        if run_dir is not None and writers is not None:
            raise ValueError("R009 session accepts run_dir or writers, not both")
        if run_dir is not None:
            root = Path(run_dir)
            supplied = {
                "state20": R009AsyncBatchWriter(
                    root / self.config.state20_filename,
                    budget=self.budget,
                    queue_max_rows=self.config.queue_max_rows,
                    batch_max_rows=self.config.batch_max_rows,
                    batch_max_wait_s=self.config.batch_max_wait_s,
                    name="r009-state20-observability-writer",
                ),
                "state25": R009AsyncBatchWriter(
                    root / self.config.state25_filename,
                    budget=self.budget,
                    queue_max_rows=self.config.queue_max_rows,
                    batch_max_rows=self.config.batch_max_rows,
                    batch_max_wait_s=self.config.batch_max_wait_s,
                    name="r009-state25-observability-writer",
                ),
            }
        self.state20 = R009State20Trace(
            config=self.config,
            budget=self.budget,
            writer=supplied.get("state20"),
        )
        self.state25 = R009State25Trace(
            config=self.config,
            budget=self.budget,
            writer=supplied.get("state25"),
        )

    @classmethod
    def from_executable_behavior_config(
        cls,
        config: ExecutableBehaviorConfig,
        *,
        run_dir: Path | None = None,
        writers: Mapping[str, R009WriterLike | None] | None = None,
    ) -> "R009ObservabilitySession":
        return cls(config=config, run_dir=run_dir, writers=writers)

    def flush(self, timeout_s: float = 30.0) -> None:
        self.state20.flush(timeout_s)
        self.state25.flush(timeout_s)

    def close(self, timeout_s: float = 30.0) -> None:
        # Closing state20 then state25 preserves a deterministic shutdown
        # order while the two workers remain independent during normal writes.
        self.state20.close(timeout_s)
        self.state25.close(timeout_s)

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": self.config.audit_schema or R009_AUDIT_SCHEMA,
            "version": self.config.version,
            "config_sha256": sha256_bytes(canonical_bytes(self.config.as_dict())),
            "config": self.config.as_dict(),
            "budget": self.budget.snapshot().as_dict(),
            "streams": {
                "state20": self.state20.snapshot(),
                "state25": self.state25.snapshot(),
            },
        }

    audit_metadata = snapshot


def attach_state20_r009_trace(owner: Any, trace: R009State20Trace) -> R009State20Trace:
    """Attach only an R009 seam; no historical r008 owner is modified."""

    if not isinstance(trace, R009State20Trace):
        raise TypeError("attach_state20_r009_trace requires an R009 state20 trace")
    setattr(owner, "_r009_state20_trace", trace)
    return trace


def attach_state25_r009_trace(owner: Any, trace: R009State25Trace) -> R009State25Trace:
    """Attach only an R009 seam; no historical r008 owner is modified."""

    if not isinstance(trace, R009State25Trace):
        raise TypeError("attach_state25_r009_trace requires an R009 state25 trace")
    setattr(owner, "_r009_state25_trace", trace)
    return trace


__all__ = [
    "R009_AUDIT_SCHEMA",
    "R009_STATE20_SCHEMA",
    "R009_STATE25_SCHEMA",
    "R009AggregateBudget",
    "R009AsyncBatchWriter",
    "R009BudgetReservation",
    "R009BudgetSnapshot",
    "R009DropCause",
    "R009DropCounter",
    "R009ObserveResult",
    "R009ObservabilityError",
    "R009ObservabilitySession",
    "R009RollingRing",
    "R009State20Trace",
    "R009State25Trace",
    "R009TraceStream",
    "State20R009Trace",
    "State25R009Trace",
    "attach_state20_r009_trace",
    "attach_state25_r009_trace",
]
