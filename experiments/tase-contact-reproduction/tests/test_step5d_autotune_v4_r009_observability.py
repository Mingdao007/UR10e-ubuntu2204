"""Hermetic R009 observability tests; no controller, bridge, or robot I/O."""

from __future__ import annotations

import copy
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r009.identity import (  # noqa: E402
    DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG,
    R009ObservabilityConfig,
    build_behavior_manifest,
)
from step5d_autotune_v4_r009.observability import (  # noqa: E402
    R009AggregateBudget,
    R009AsyncBatchWriter,
    R009DropCause,
    R009ObservabilitySession,
    _QueuedRow,
    _FlushRequest,
    _WRITER_SENTINEL,
)
from step5d_autotune_v4_r009.observer import (  # noqa: E402
    R009DashboardObserver,
    R009IncrementalJsonlTail,
    freshness_ttl_s,
)


class _FakeWriter:
    def __init__(self, *, capacity: int | None = None, error: str | None = None) -> None:
        self.capacity = capacity
        self._error = error
        self._closed = False
        self.items: list[_QueuedRow] = []

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def error(self) -> str | None:
        return self._error

    def enqueue(self, item: _QueuedRow) -> R009DropCause | None:
        if self._closed:
            return R009DropCause.CLOSED
        if self._error is not None:
            return R009DropCause.ERROR
        if self.capacity is not None and len(self.items) >= self.capacity:
            return R009DropCause.QUEUE_OVERFLOW
        self.items.append(item)
        return None

    def close(self, timeout_s: float = 30.0) -> None:
        del timeout_s
        self._closed = True


def _config(**overrides: Any) -> R009ObservabilityConfig:
    raw = copy.deepcopy(DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG["values"]["observability"])
    raw.update(overrides)
    return R009ObservabilityConfig.from_mapping(raw)


def _row(t: float, *, state: int, attempt: str = "attempt-a", **extra: Any) -> dict[str, Any]:
    payload = {
        "monotonic_s": t,
        "tp_state": state,
        "attempt_id": attempt,
        "force_norm_n": 1.25,
    }
    payload.update(extra)
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_default_ring_eviction_and_behavior_identity_binding() -> None:
    config = _config()
    fake = _FakeWriter()
    session = R009ObservabilitySession(config=config, writers={"state20": fake})
    for index in range(config.ring_capacity_rows + 1):
        session.state20.observe(_row(index / config.hot_path_hz, state=20))

    rows = session.state20.recent_rows()
    assert len(rows) == 2500
    assert rows[0]["r009_trace_sequence"] == 2
    assert rows[-1]["r009_trace_sequence"] == 2501
    assert session.state20.snapshot()["ring_evicted_rows"] == 1

    baseline = build_behavior_manifest(
        parent_r006_contract_sha256="a" * 64,
        parent_r006_source_closure_sha256="b" * 64,
        source_set={"tools/behavior.py": "c" * 64},
    )
    changed = copy.deepcopy(DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG["values"]["observability"])
    changed["ring_capacity_rows"] = 2501
    drifted = build_behavior_manifest(
        parent_r006_contract_sha256="a" * 64,
        parent_r006_source_closure_sha256="b" * 64,
        source_set={"tools/behavior.py": "c" * 64},
        executable_behavior_config={
            **copy.deepcopy(DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG),
            "values": {
                **copy.deepcopy(DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG["values"]),
                "observability": changed,
            },
        },
    )
    assert baseline.campaign_fingerprint != drifted.campaign_fingerprint


def test_observe_hot_path_performs_no_filesystem_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"filesystem operation on observe path: {args!r} {kwargs!r}")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(os, "fsync", forbidden)
    fake = _FakeWriter(capacity=8)
    session = R009ObservabilitySession(config=_config(), writers={"state20": fake})
    for index in range(40):
        session.state20.observe(_row(index / 500.0, state=20))
    assert len(session.state20.recent_rows()) == 40


def test_exact_25_hz_sampling_and_transition_stop_tail(tmp_path: Path) -> None:
    config = _config(queue_max_rows=256, batch_max_rows=32, stop_tail_rows=5)
    sample_session = R009ObservabilitySession(config=config, run_dir=tmp_path / "sample")
    for index in range(500):
        sample_session.state20.observe(_row(index / 500.0, state=20))
    sample_session.flush()
    sample_session.close()
    sample_rows = _read_jsonl(tmp_path / "sample" / config.state20_filename)
    assert len(sample_rows) == 25
    assert [row["r009_trace_sequence"] for row in sample_rows] == list(range(1, 501, 20))
    assert all(row["r009_retention"] in {"sample", "state_transition"} for row in sample_rows)

    stop_session = R009ObservabilitySession(config=config, run_dir=tmp_path / "stop")
    for index in range(40):
        stop_session.state20.observe(_row(index / 500.0, state=20))
    stop_session.state20.observe(_row(40 / 500.0, state=21))
    stop_session.state20.observe(_row(41 / 500.0, state=90))
    stop_session.flush()
    stop_session.close()
    stop_rows = _read_jsonl(tmp_path / "stop" / config.state20_filename)
    by_sequence = {row["r009_trace_sequence"]: row for row in stop_rows}
    assert set(range(38, 43)).issubset(by_sequence)
    assert by_sequence[41]["r009_retention"] == "state_transition"
    assert by_sequence[42]["r009_retention"] == "state_transition"
    assert all(by_sequence[index]["r009_retention"] == "stop_tail" for index in range(38, 41))
    assert len(by_sequence) == len(stop_rows)


def test_queue_overflow_and_typed_drop_counters() -> None:
    fake = _FakeWriter(capacity=0)
    session = R009ObservabilitySession(config=_config(), writers={"state20": fake})
    session.state20.observe(_row(0.0, state=20))
    session.state20.observe(_row(0.002, state=20))
    fake._closed = True
    session.state20.observe(_row(0.004, state=20))
    fake._closed = False
    fake._error = "injected writer error"
    session.state20.observe(_row(0.006, state=20))
    dropped = session.budget.snapshot().dropped
    assert dropped[R009DropCause.QUEUE_OVERFLOW].rows >= 1
    assert dropped[R009DropCause.SAMPLING].rows >= 1
    assert dropped[R009DropCause.CLOSED].rows >= 1
    assert dropped[R009DropCause.ERROR].rows >= 1
    assert set(dropped) == set(R009DropCause)
    audit = session.audit_metadata()
    assert {"queued_rows", "queued_bytes", "written_rows", "written_bytes"} <= set(
        audit["budget"]
    )
    assert set(audit["budget"]["dropped"]) == {cause.value for cause in R009DropCause}


def test_shared_attempt_and_run_caps_cover_both_streams() -> None:
    probe_writer20 = _FakeWriter(capacity=10)
    probe_writer25 = _FakeWriter(capacity=10)
    probe = R009ObservabilitySession(
        config=_config(),
        writers={"state20": probe_writer20, "state25": probe_writer25},
    )
    probe.state20.observe(_row(0.0, state=20, attempt="a"))
    probe.state25.observe(_row(0.0, state=25, attempt="a"))
    first_sizes = [len(item.encoded) for item in probe_writer20.items + probe_writer25.items]
    assert len(first_sizes) == 2
    total_first_attempt = sum(first_sizes)

    attempt_writer20 = _FakeWriter(capacity=10)
    attempt_writer25 = _FakeWriter(capacity=10)
    attempt_session = R009ObservabilitySession(
        config=_config(
            attempt_cap_bytes=total_first_attempt - 1,
            run_cap_bytes=total_first_attempt + 1000,
        ),
        writers={"state20": attempt_writer20, "state25": attempt_writer25},
    )
    attempt_session.state20.observe(_row(0.0, state=20, attempt="a"))
    attempt_session.state25.observe(_row(0.0, state=25, attempt="a"))
    assert attempt_session.budget.snapshot().dropped[R009DropCause.ATTEMPT_CAP].rows >= 1

    run_writer20 = _FakeWriter(capacity=10)
    run_writer25 = _FakeWriter(capacity=10)
    run_session = R009ObservabilitySession(
        config=_config(
            attempt_cap_bytes=total_first_attempt,
            run_cap_bytes=total_first_attempt + first_sizes[0] - 1,
        ),
        writers={"state20": run_writer20, "state25": run_writer25},
    )
    run_session.state20.observe(_row(0.0, state=20, attempt="a"))
    run_session.state25.observe(_row(0.0, state=25, attempt="a"))
    run_session.state20.observe(_row(0.0, state=20, attempt="b"))
    assert run_session.budget.snapshot().dropped[R009DropCause.RUN_CAP].rows >= 1
    assert run_session.budget.snapshot().queued_rows == 2


def test_async_writer_error_is_observable_and_nonfatal(tmp_path: Path) -> None:
    path = tmp_path / "existing.jsonl"
    sentinel = b"historical-r009-trace\n"
    path.write_bytes(sentinel)
    budget = R009AggregateBudget(attempt_cap_bytes=1024, run_cap_bytes=2048)
    writer = R009AsyncBatchWriter(
        path,
        budget=budget,
        queue_max_rows=4,
        batch_max_rows=2,
        batch_max_wait_s=0.01,
        name="r009-test-error-writer",
    )
    deadline = time.monotonic() + 2.0
    while (writer.error is None or writer._thread.is_alive()) and time.monotonic() < deadline:
        time.sleep(0.001)
    assert writer.error is not None
    assert not writer._thread.is_alive()
    fake_stream = R009ObservabilitySession(
        config=_config(),
        writers={"state20": writer},
    )
    result = fake_stream.state20.observe(_row(0.0, state=20))
    assert result.unavailable_cause in {R009DropCause.ERROR, R009DropCause.CLOSED}
    assert path.read_bytes() == sentinel
    writer.close(timeout_s=0.5)
    assert writer.closed
    assert writer._queue.unfinished_tasks == 0


def test_async_writer_close_linearizes_admission_and_never_writes_after_sentinel(
    tmp_path: Path,
) -> None:
    config = _config(
        queue_max_rows=1,
        batch_max_rows=1,
        batch_max_wait_s=0.01,
        stop_tail_rows=1,
    )
    budget = R009AggregateBudget(attempt_cap_bytes=1024 * 1024, run_cap_bytes=4 * 1024 * 1024)
    writer = R009AsyncBatchWriter(
        tmp_path / config.state20_filename,
        budget=budget,
        queue_max_rows=config.queue_max_rows,
        batch_max_rows=config.batch_max_rows,
        batch_max_wait_s=config.batch_max_wait_s,
        name="r009-test-close-admission",
    )
    entered = threading.Event()
    release = threading.Event()
    original_write_batch = writer._write_batch

    def blocked_write_batch(handle: Any, batch: list[_QueuedRow]) -> None:
        entered.set()
        assert release.wait(2.0)
        original_write_batch(handle, batch)

    writer._write_batch = blocked_write_batch  # type: ignore[method-assign]
    session = R009ObservabilitySession(
        config=config,
        budget=budget,
        writers={"state20": writer},
    )
    close_done = threading.Event()
    close_errors: list[BaseException] = []
    close_thread: threading.Thread | None = None

    def close_call() -> None:
        try:
            writer.close(timeout_s=2.0)
        except BaseException as exc:
            close_errors.append(exc)
        finally:
            close_done.set()

    try:
        first = session.state20.observe(_row(0.0, state=20))
        assert first.queued_rows == 1
        assert entered.wait(2.0)

        second = session.state20.observe(_row(0.002, state=21))
        assert second.queued_rows == 1
        close_thread = threading.Thread(
            target=close_call,
            name="r009-test-close-admission-call",
        )
        close_thread.start()
        deadline = time.monotonic() + 2.0
        while not writer.closed and time.monotonic() < deadline:
            time.sleep(0.001)
        assert writer.closed

        third = session.state20.observe(_row(0.004, state=22))
        assert third.unavailable_cause == R009DropCause.CLOSED
        assert writer._queue.qsize() == 1

        release.set()
        assert close_done.wait(2.0)
        close_thread.join(timeout=0.1)
        rows = _read_jsonl(tmp_path / config.state20_filename)
        assert [row["r009_trace_sequence"] for row in rows] == [1, 2]
        snapshot = budget.snapshot()
        assert snapshot.pending_rows == 0
        assert snapshot.pending_bytes == 0
        assert snapshot.dropped[R009DropCause.CLOSED].rows == 1
        assert writer._sentinel_enqueued
        assert writer._queue.unfinished_tasks == 0
    except BaseException as exc:
        raise
    finally:
        release.set()
        if close_thread is not None:
            close_thread.join(timeout=2.0)
        if not close_done.is_set():
            writer.close(timeout_s=2.0)
        assert not close_errors


def test_enqueue_admission_cannot_cross_close_sentinel(tmp_path: Path) -> None:
    config = _config(queue_max_rows=2, batch_max_rows=1, batch_max_wait_s=0.01)
    budget = R009AggregateBudget(attempt_cap_bytes=1024 * 1024, run_cap_bytes=4 * 1024 * 1024)
    writer = R009AsyncBatchWriter(
        tmp_path / config.state20_filename,
        budget=budget,
        queue_max_rows=config.queue_max_rows,
        batch_max_rows=config.batch_max_rows,
        batch_max_wait_s=config.batch_max_wait_s,
        name="r009-test-admission-linearization",
    )
    original_put_nowait = writer._queue.put_nowait
    producer_entered_put = threading.Event()
    release_put = threading.Event()
    gate_once = True

    def gated_put_nowait(item: Any) -> None:
        nonlocal gate_once
        if gate_once and isinstance(item, _QueuedRow):
            gate_once = False
            producer_entered_put.set()
            assert release_put.wait(2.0)
        original_put_nowait(item)

    writer._queue.put_nowait = gated_put_nowait  # type: ignore[method-assign]
    session = R009ObservabilitySession(config=config, budget=budget, writers={"state20": writer})
    producer_done = threading.Event()
    close_started = threading.Event()
    close_done = threading.Event()
    producer_errors: list[BaseException] = []
    close_errors: list[BaseException] = []
    result: list[Any] = []

    def produce() -> None:
        try:
            result.append(session.state20.observe(_row(0.0, state=20)))
        except BaseException as exc:
            producer_errors.append(exc)
        finally:
            producer_done.set()

    def close_call() -> None:
        close_started.set()
        try:
            writer.close(timeout_s=2.0)
        except BaseException as exc:
            close_errors.append(exc)
        finally:
            close_done.set()

    producer_thread = threading.Thread(target=produce, name="r009-test-admission-producer")
    close_thread = threading.Thread(target=close_call, name="r009-test-admission-closer")
    try:
        producer_thread.start()
        assert producer_entered_put.wait(2.0)
        close_thread.start()
        assert close_started.wait(2.0)

        # The producer still owns the admission point.  A nonblocking probe
        # must never observe close acquiring that lock before the gated put.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if writer._admission_lock.acquire(blocking=False):
                writer._admission_lock.release()
                pytest.fail("close crossed the gated row admission")
            time.sleep(0.001)

        release_put.set()
        assert producer_done.wait(2.0)
        assert close_done.wait(2.0)
        producer_thread.join(timeout=0.1)
        close_thread.join(timeout=0.1)
        assert not producer_errors
        assert not close_errors
        assert result and result[0].queued_rows == 1
        rows = _read_jsonl(tmp_path / config.state20_filename)
        assert [row["r009_trace_sequence"] for row in rows] == [1]
        assert budget.snapshot().pending_rows == 0
        assert writer._queue.unfinished_tasks == 0
    finally:
        release_put.set()
        producer_thread.join(timeout=2.0)
        close_thread.join(timeout=2.0)
        if not close_done.is_set():
            writer.close(timeout_s=2.0)


def test_async_writer_flush_then_close_has_deterministic_control_order(
    tmp_path: Path,
) -> None:
    config = _config(queue_max_rows=1, batch_max_rows=1, batch_max_wait_s=0.01)
    budget = R009AggregateBudget(attempt_cap_bytes=1024 * 1024, run_cap_bytes=4 * 1024 * 1024)
    writer = R009AsyncBatchWriter(
        tmp_path / config.state20_filename,
        budget=budget,
        queue_max_rows=config.queue_max_rows,
        batch_max_rows=config.batch_max_rows,
        batch_max_wait_s=config.batch_max_wait_s,
        name="r009-test-flush-close-order",
    )
    entered = threading.Event()
    release = threading.Event()
    original_write_batch = writer._write_batch

    def blocked_write_batch(handle: Any, batch: list[_QueuedRow]) -> None:
        entered.set()
        assert release.wait(2.0)
        original_write_batch(handle, batch)

    writer._write_batch = blocked_write_batch  # type: ignore[method-assign]
    session = R009ObservabilitySession(config=config, budget=budget, writers={"state20": writer})
    flush_done = threading.Event()
    close_done = threading.Event()
    flush_errors: list[BaseException] = []
    close_errors: list[BaseException] = []
    flush_thread: threading.Thread | None = None
    close_thread: threading.Thread | None = None

    def flush_call() -> None:
        try:
            writer.flush(timeout_s=2.0)
        except BaseException as exc:
            flush_errors.append(exc)
        finally:
            flush_done.set()

    def close_call() -> None:
        try:
            writer.close(timeout_s=2.0)
        except BaseException as exc:
            close_errors.append(exc)
        finally:
            close_done.set()

    try:
        assert session.state20.observe(_row(0.0, state=20)).queued_rows == 1
        assert entered.wait(2.0)

        flush_thread = threading.Thread(
            target=flush_call,
            name="r009-test-flush-call",
        )
        flush_thread.start()
        deadline = time.monotonic() + 2.0
        while writer._queue.qsize() != 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert writer._queue.qsize() == 1

        close_thread = threading.Thread(
            target=close_call,
            name="r009-test-flush-close-call",
        )
        close_thread.start()
        assert writer.closed
        # Close linearized after the flush marker; a later flush cannot put a
        # control after the sentinel and therefore returns as closed.
        writer.flush(timeout_s=0.1)
        release.set()
        assert flush_done.wait(2.0)
        assert close_done.wait(2.0)
        flush_thread.join(timeout=0.1)
        close_thread.join(timeout=0.1)
        assert _read_jsonl(tmp_path / config.state20_filename)[0]["r009_trace_sequence"] == 1
        assert budget.snapshot().pending_rows == 0
        assert writer._queue.unfinished_tasks == 0
    finally:
        release.set()
        if flush_thread is not None:
            flush_thread.join(timeout=2.0)
        if close_thread is not None:
            close_thread.join(timeout=2.0)
        if not close_done.is_set():
            writer.close(timeout_s=2.0)
        assert not flush_errors
        assert not close_errors


def test_error_drain_accounts_removed_controls_exactly_once() -> None:
    writer = object.__new__(R009AsyncBatchWriter)
    writer._queue = queue.Queue(maxsize=4)
    writer._admission_lock = threading.Lock()
    writer._error = None
    writer._closed = False
    writer.budget = R009AggregateBudget(attempt_cap_bytes=1024, run_cap_bytes=2048)

    reservation = writer.budget.reserve("attempt-a", 8)
    assert not isinstance(reservation, R009DropCause)
    writer.budget.mark_queued(reservation)
    queued = _QueuedRow(reservation, b'{"n":1}\n', threading.Event())
    request = _FlushRequest(threading.Event())
    writer._queue.put(request)
    writer._queue.put(_WRITER_SENTINEL)
    writer._queue.put(queued)
    removed_control = writer._queue.get_nowait()

    writer._drain_after_error(removed_control)

    assert request.completed.is_set()
    assert writer._queue.unfinished_tasks == 0
    snapshot = writer.budget.snapshot()
    assert snapshot.pending_rows == 0
    assert snapshot.dropped[R009DropCause.ERROR].rows == 1


def test_incremental_seek_tail_rotation_truncation_and_partial_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "trace.jsonl"
    first = json.dumps({"n": 1}, sort_keys=True) + "\n"
    path.write_text(first, encoding="utf-8")
    tail = R009IncrementalJsonlTail(path)
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("full reread")))
    poll1 = tail.poll()
    assert [row["n"] for row in poll1.rows] == [1]
    append = json.dumps({"n": 2}, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(append)
    poll2 = tail.poll()
    assert [row["n"] for row in poll2.rows] == [2]
    assert poll2.bytes_read == len(append.encode())

    partial = json.dumps({"n": 3}, sort_keys=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(partial)
    assert tail.poll().rows == ()
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert [row["n"] for row in tail.poll().rows] == [3]

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text(json.dumps({"n": 4}) + "\n", encoding="utf-8")
    os.replace(replacement, path)
    rotated = tail.poll()
    assert rotated.reset_reason == "replacement"
    assert [row["n"] for row in rotated.rows] == [4]

    path.write_text("{}\n", encoding="utf-8")
    truncated = tail.poll()
    assert truncated.reset_reason in {"truncation", "rewrite"}
    assert list(truncated.rows) == [{}]


def test_missing_tail_resets_partial_offset_and_identity_before_recreation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "recreated.jsonl"
    path.write_text(json.dumps({"n": 1}), encoding="utf-8")
    tail = R009IncrementalJsonlTail(path)
    assert tail.poll().rows == ()
    assert tail.offset == path.stat().st_size

    path.unlink()
    missing = tail.poll()
    assert missing.reset_reason == "missing"
    assert missing.offset == 0
    assert tail.offset == 0

    path.write_text(json.dumps({"n": 2}) + "\n", encoding="utf-8")
    recreated = tail.poll()
    assert recreated.reset_reason == "initial"
    assert [row["n"] for row in recreated.rows] == [2]


def test_exact_ttl_formula_and_stale_sample_exclusion(tmp_path: Path) -> None:
    assert freshness_ttl_s(0.1) == 5.0
    assert freshness_ttl_s(2.0) == 6.0
    path = tmp_path / "r009-state20-observability.jsonl"
    path.write_text(
        json.dumps(
            {
                "schema": "step5d.autotune-v4/r009-state20-observability-v1",
                "r009_observed_at_s": 10.0,
                "force_norm_n": 3.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    observer = R009DashboardObserver(paths={"state20": path}, config=_config())
    observer.poll()
    aliases = {
        "force_sample": "carried",
        "current_force_sample": "carried",
        "force_sample_meta": "carried",
        "current_force_sample_meta": "carried",
        "sample": "carried",
        "current_sample": "carried",
        "sample_meta": "carried",
        "current_sample_meta": "carried",
    }
    fresh = observer.decorate_current_event({"event": "tick", **aliases}, now_s=15.0)
    assert "force_sample" in fresh
    assert "current_force_sample_meta" not in fresh
    assert "current_sample" not in fresh
    stale = observer.decorate_current_event({"event": "tick"}, now_s=15.000001)
    assert "force_sample" not in stale
    assert stale["stale_force_sample_meta"]["stale"] is True

    carried_stale = observer.decorate_current_event(
        {"event": "tick", **aliases}, now_s=15.000001
    )
    assert not set(aliases).intersection(carried_stale)

    no_sample_observer = R009DashboardObserver(
        paths={"state20": tmp_path / "never-created.jsonl"}, config=_config()
    )
    no_sample = no_sample_observer.decorate_current_event(
        {"event": "tick", **aliases}, now_s=0.0
    )
    assert not set(aliases).intersection(no_sample)
