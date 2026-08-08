"""Unit tests for ordered async seal pipeline + continuous-run motion path."""

from __future__ import annotations

import inspect
import threading
import time

from step5d_autotune_v4_r008.async_seal import (
    AsyncSealPipeline,
    SealJob,
    dispatch_s_hides_seal_wall,
    kind_holds_seal_until_path60,
    phase_needs_sync_seal,
)
from step5d_autotune_v4_r008.live_adapter import (
    HostMailboxIntent,
    R008HostLoop,
)


class _Result:
    def __init__(self, seq: int, kind: str = "SPACEFILL") -> None:
        self.attempt_sequence = seq
        self.kind = kind


def test_run_in_fork_uses_child_pid() -> None:
    import os

    from step5d_autotune_v4_r008.gil_isolation import run_in_fork

    parent = os.getpid()
    child_pid = run_in_fork(os.getpid)
    assert child_pid != parent


def test_phase_needs_sync_seal_always_false() -> None:
    assert phase_needs_sync_seal("STAIRCASE", "STAIRCASE") is False
    assert phase_needs_sync_seal("SPACEFILL", "SPACEFILL") is False
    assert phase_needs_sync_seal("ANCHOR", "ANCHOR") is False
    assert phase_needs_sync_seal("QUALIFICATION", "QUALIFICATION") is False


def test_kind_holds_seal_until_path60_path_vs_qual() -> None:
    assert kind_holds_seal_until_path60("SPACEFILL") is True
    assert kind_holds_seal_until_path60("ANCHOR") is True
    assert kind_holds_seal_until_path60("STAIRCASE") is True
    assert kind_holds_seal_until_path60("BO") is True
    assert kind_holds_seal_until_path60("BO_TRIAL") is True
    assert kind_holds_seal_until_path60("RETEST") is True
    assert kind_holds_seal_until_path60("QUALIFICATION") is False
    assert kind_holds_seal_until_path60("RE_TEST") is False


def test_dispatch_s_hides_seal_wall_detects_serial_barrier() -> None:
    assert dispatch_s_hides_seal_wall(33.0, 32.5) is True
    assert dispatch_s_hides_seal_wall(1.4, 32.5) is False
    assert dispatch_s_hides_seal_wall(None, 32.5) is False


def test_run_one_source_has_no_predispatch_join_all() -> None:
    src = inspect.getsource(R008HostLoop.run_one)
    assert "async_seal_join_before_dispatch" not in src
    assert "hold_until_path60" in src
    assert "kind_holds_seal_until_path60" in src
    # QUAL baseline may still join_all; HOME→DISPATCH must not.
    idx_dispatch = src.find('self._phase("DISPATCH")')
    assert idx_dispatch > 0
    assert "join_all()" not in src[:idx_dispatch]


def test_daemon_mode_does_not_disable_search_critical_gating() -> None:
    """Regression: canary "023243" (2026-08-05) -- disabling gating for the
    off-host daemon on the theory that seal CPU living in another process
    means the host never needs to gate/join was falsified live: reason=43
    recurred with async_seal_wall/seal_overlap≈9s and blocked=0.0. Daemon and
    fork must share the same gating unless a future clean canary proves
    otherwise -- do not re-special-case ``offhost``/``_seal_daemon`` here.
    """

    src_init = inspect.getsource(R008HostLoop.__init__)
    assert "not offhost" not in src_init
    assert "join_executing_on_search_critical=True" in src_init
    assert "search_critical_gates_seals=True" in src_init

    src_run_one = inspect.getsource(R008HostLoop.run_one)
    assert "_seal_daemon" not in src_run_one
    assert "hold_until_path60 = kind_holds_seal_until_path60(kind_s)" in src_run_one
    # Seal-join cut: drain executing seal before HOME so ARM join is ~0.
    assert "join_executing()" in src_run_one
    assert src_run_one.index("join_executing()") < src_run_one.index('self._phase("HOME")')


def test_join_executing_waits_without_setting_search_critical() -> None:
    started = threading.Event()
    release = threading.Event()
    joined_s = {"v": -1.0}

    def seal_fn(result: _Result) -> _Result:
        started.set()
        release.wait(timeout=5.0)
        return result

    pipe = AsyncSealPipeline(seal_fn=seal_fn, advance_fn=lambda r: None)
    try:
        job = pipe.submit(_Result(1), ticket=None, needs_sync=False, hold_until_path60=False)
        assert started.wait(timeout=2.0)
        assert pipe._search_critical is False  # noqa: SLF001

        def _wait() -> None:
            joined_s["v"] = pipe.join_executing()

        waiter = threading.Thread(target=_wait)
        waiter.start()
        time.sleep(0.05)
        assert pipe._search_critical is False  # noqa: SLF001
        assert waiter.is_alive()
        release.set()
        waiter.join(timeout=2.0)
        assert joined_s["v"] >= 0.0
        job.future_done.wait(timeout=2.0)
    finally:
        release.set()
        pipe.close_and_join(timeout_s=2.0)


def test_record_and_tell_refreshes_parent_r006_cache_after_fork() -> None:
    """Regression: parent's r006_ledger cache must refresh after run_in_fork.

    Found live 2026-08-05 (canary "011258"): "r006 sidecar row 3 hash chain
    differs". The fork child computes previous_sha256 from its own copy of
    r006_ledger._cached, seeded from the parent's memory at fork time. If the
    parent never refreshes its own r006_ledger cache after a successful
    append, every later fork inherits a permanently stale (pre-append) cache
    and computes previous=GENESIS instead of the true prior row hash -- a
    deterministic chain-head bug (reproduces on the 2nd+ non-QUALIFICATION
    seal of every run), not a timing race. The refresh must be unconditional
    (not nested under the QUALIFICATION branch) and must run after
    run_in_fork returns.
    """

    # Fork path lives in _record_and_tell_via_fork (daemon is the live default).
    src = inspect.getsource(R008HostLoop._record_and_tell_via_fork)
    idx_fork = src.find("run_in_fork(_heavy_seal_artifacts)")
    assert idx_fork > 0
    after_fork = src[idx_fork:]
    idx_call = after_fork.find("r006.fresh_process_verify()")
    assert idx_call > 0
    # Must not be nested only under the QUALIFICATION branch after the fork.
    idx_qual_branch = after_fork.find('if kind_s != "QUALIFICATION":')
    assert idx_qual_branch == -1 or idx_call < idx_qual_branch


def test_prepare_after_home_never_joins() -> None:
    """Archived join path: always uses refill_prepare, ignores pending flag."""

    calls: list[str] = []

    def prepare_next() -> str:
        calls.append("prepare")
        return "should-not-run"

    def join_refill_prepare() -> str:
        calls.append("refill_prepare")
        return "ticket"

    ticket = R008HostLoop._prepare_after_home_allowing_seal_overlap(
        has_pending=True,
        prepare_next=prepare_next,
        join_refill_prepare=join_refill_prepare,
    )
    assert ticket == "ticket"
    assert calls == ["refill_prepare"]

    calls.clear()
    ticket = R008HostLoop._prepare_after_home_allowing_seal_overlap(
        has_pending=False,
        prepare_next=prepare_next,
        join_refill_prepare=join_refill_prepare,
    )
    assert ticket == "ticket"
    assert calls == ["refill_prepare"]


def test_mailbox_cancel_does_not_touch_queue_on_seal_thread() -> None:
    loop = object.__new__(R008HostLoop)
    loop.events = []
    loop._mailbox = __import__("queue").SimpleQueue()
    loop._post_mailbox(
        HostMailboxIntent(
            cancel_pending=True,
            reason="r008_staircase_edge",
            phase="SPACEFILL",
            close_staircase=True,
        )
    )
    intent = loop._mailbox.get_nowait()
    assert intent.cancel_pending is True
    assert intent.phase == "SPACEFILL"
    assert intent.close_staircase is True


def test_fifo_seal_order_and_path60_overlap() -> None:
    sealed: list[int] = []
    advanced: list[int] = []

    def seal_fn(result: _Result) -> _Result:
        time.sleep(0.05)
        sealed.append(result.attempt_sequence)
        return result

    def advance_fn(record: _Result) -> None:
        advanced.append(record.attempt_sequence)

    pipe = AsyncSealPipeline(seal_fn=seal_fn, advance_fn=advance_fn)
    try:
        job1 = pipe.submit(
            _Result(1), ticket=object(), needs_sync=False, hold_until_path60=True
        )
        time.sleep(0.01)
        pipe.mark_next_motion_start()
        # HOME-only mark must not count as useful overlap.
        assert pipe.last_metrics() is None or pipe.last_metrics().seal_overlap_s in (
            None,
            0.0,
        )
        pipe.end_search_critical()  # SAFE_RETURN (post-PATH release)
        job1.future_done.wait(timeout=2.0)
        assert job1.error is None
        metrics = pipe.last_metrics()
        assert metrics is not None
        assert metrics.seal_overlap_s is not None
        assert metrics.seal_overlap_s > 0.0
        assert sealed == [1]
        assert advanced == [1]

        # needs_sync=True still works as an explicit barrier (stop/drain only).
        job2 = pipe.submit(_Result(2), ticket=object(), needs_sync=True)
        assert job2.error is None
        assert sealed == [1, 2]
        assert advanced == [1, 2]
    finally:
        pipe.close_and_join(timeout_s=5.0)


def test_search_critical_defers_seal_until_path60() -> None:
    sealed: list[int] = []
    started = threading.Event()

    def seal_fn(result: _Result) -> _Result:
        started.set()
        sealed.append(result.attempt_sequence)
        return result

    pipe = AsyncSealPipeline(seal_fn=seal_fn, advance_fn=lambda _r: None)
    try:
        # Host pattern: hold_until_path60 under the submit lock (no race).
        job = pipe.submit(
            _Result(1), ticket=object(), needs_sync=False, hold_until_path60=True
        )
        assert not started.wait(timeout=0.15), "seal must not run before SAFE_RETURN release"
        assert sealed == []
        pipe.mark_next_motion_start()  # HOME — still held
        assert not started.is_set()
        pipe.end_search_critical()
        assert job.future_done.wait(timeout=2.0)
        assert job.error is None
        assert sealed == [1]
        assert started.is_set()
        metrics = pipe.last_metrics()
        assert metrics is not None
        assert metrics.seal_overlap_s is not None
        assert metrics.seal_overlap_s > 0.0
        assert job.path60_started_s is not None
    finally:
        pipe.close_and_join(timeout_s=5.0)


def test_home_motion_mark_alone_does_not_count_as_overlap() -> None:
    """Regression: seal_overlap must not equal wall while parked in join_all/HOME."""

    release = threading.Event()

    def seal_fn(result: _Result) -> _Result:
        release.wait(timeout=2.0)
        return result

    pipe = AsyncSealPipeline(seal_fn=seal_fn, advance_fn=lambda _r: None)
    try:
        job = pipe.submit(
            _Result(1), ticket=object(), needs_sync=False, hold_until_path60=True
        )
        pipe.mark_next_motion_start()
        time.sleep(0.05)
        # Still held — no PATH60 mark yet; releasing gate then finishing.
        pipe.end_search_critical()
        release.set()
        assert job.future_done.wait(timeout=2.0)
        metrics = pipe.last_metrics()
        assert metrics is not None
        assert metrics.seal_overlap_s is not None
        # Overlap starts at PATH60, not HOME; wall includes hold wait.
        assert metrics.seal_wall_s is not None
        assert metrics.seal_overlap_s <= metrics.seal_wall_s + 1e-6
    finally:
        release.set()
        pipe.close_and_join(timeout_s=5.0)


def test_begin_search_joins_already_executing_seal() -> None:
    release = threading.Event()
    entered = threading.Event()

    def seal_fn(result: _Result) -> _Result:
        entered.set()
        release.wait(timeout=2.0)
        return result

    pipe = AsyncSealPipeline(seal_fn=seal_fn, advance_fn=lambda _r: None)
    try:
        job = pipe.submit(_Result(1), ticket=object(), needs_sync=False)
        assert entered.wait(timeout=2.0)
        t0 = time.perf_counter()
        joined_s: list[float] = []
        # Spillover into next search: motion waits out the executing seal.
        waiter = threading.Thread(target=lambda: joined_s.append(pipe.begin_search_critical()))
        waiter.start()
        time.sleep(0.05)
        assert waiter.is_alive()
        release.set()
        waiter.join(timeout=2.0)
        assert not waiter.is_alive()
        assert job.future_done.wait(timeout=2.0)
        assert time.perf_counter() - t0 >= 0.05
        # Regression (canary "025746"): the per-job "blocked=" event can log
        # 0.0 while this join is still in progress -- the call site must be
        # able to read the join duration directly off the return value.
        assert joined_s and joined_s[0] >= 0.05
        metrics = pipe.last_metrics()
        assert metrics is not None
        # Motion-thread join must be charged into blocked after wait returns.
        assert metrics.motion_blocked_on_seal_s >= 0.05
    finally:
        release.set()
        pipe.close_and_join(timeout_s=5.0)


def test_begin_search_critical_returns_zero_when_nothing_executing() -> None:
    pipe = AsyncSealPipeline(seal_fn=lambda r: r, advance_fn=lambda _r: None)
    try:
        assert pipe.begin_search_critical() == 0.0
    finally:
        pipe.close_and_join(timeout_s=5.0)


def test_refresh_blocked_metrics_does_not_clobber_other_jobs_row() -> None:
    """Regression (canary "025746"): ``_on_done`` logs a job's event *before*
    ``future_done.set()`` releases a motion-thread join on it (see
    ``begin_search_critical`` docstring), so that join's ``blocked_waiting_s``
    update can land after a *later* job has already become ``_last_metrics``.
    Exercise ``_refresh_blocked_metrics`` directly (deterministic; the real
    race depends on OS thread-wake ordering and would be flaky to reproduce
    end-to-end) and assert it no longer clobbers a newer job's row.
    """

    pipe = AsyncSealPipeline(seal_fn=lambda r: r, advance_fn=lambda _r: None)
    try:
        job_a = SealJob(
            result=_Result(1), ticket=object(), attempt_sequence=1, kind="ANCHOR",
            submitted_s=0.0, needs_sync=False,
        )
        job_a.seal_finished_s = 1.0
        job_a.blocked_waiting_s = 9.0

        job_b = SealJob(
            result=_Result(2), ticket=object(), attempt_sequence=2, kind="ANCHOR",
            submitted_s=1.0, needs_sync=False,
        )
        job_b.seal_finished_s = 2.0

        # job_b already finished and became the logged row (as canary "025746"
        # showed: seq2's seal completed while seq1's motion-thread join was
        # still pending).
        pipe._last_metrics = pipe._metrics_for(job_b)  # noqa: SLF001
        assert pipe.last_metrics().attempt_sequence == 2

        # job_a's join now completes and tries to refresh -- must be a no-op.
        pipe._refresh_blocked_metrics(job_a)  # noqa: SLF001

        after = pipe.last_metrics()
        assert after is not None
        assert after.attempt_sequence == 2
        assert after.motion_blocked_on_seal_s == 0.0
    finally:
        pipe.close_and_join(timeout_s=5.0)


def test_packet_stale_must_not_widen_for_seal() -> None:
    from step5d_autotune_v4_r004.contracts import PACKET_STALE_S
    from step5d_autotune_v4_r008.async_seal import (
        PACKET_STALE_S_MUST_REMAIN,
        WIDEN_TO_ABSORB_SEAL_WOULD_NEED_S,
    )

    assert PACKET_STALE_S == PACKET_STALE_S_MUST_REMAIN == 0.080
    lo, hi = WIDEN_TO_ABSORB_SEAL_WOULD_NEED_S
    assert lo >= 15.0 and hi <= 35.0


def test_per_job_hold_older_released_while_newer_stays_held() -> None:
    """SAFE_RETURN release must not be re-blocked by the next submit(hold)."""

    sealed: list[int] = []
    started1 = threading.Event()

    def seal_fn(result: _Result) -> _Result:
        sealed.append(result.attempt_sequence)
        if result.attempt_sequence == 1:
            started1.set()
            time.sleep(0.05)
        return result

    pipe = AsyncSealPipeline(seal_fn=seal_fn, advance_fn=lambda _r: None)
    try:
        job1 = pipe.submit(
            _Result(1), ticket=object(), needs_sync=False, hold_until_path60=True
        )
        assert not started1.wait(timeout=0.1)
        pipe.end_search_critical()  # prior SAFE_RETURN
        job2 = pipe.submit(
            _Result(2), ticket=object(), needs_sync=False, hold_until_path60=True
        )
        assert started1.wait(timeout=2.0), "job1 must run after release"
        assert job1.future_done.wait(timeout=2.0)
        assert sealed == [1]
        assert not job2.future_done.wait(timeout=0.15), "job2 still held"
        pipe.end_search_critical()
        assert job2.future_done.wait(timeout=2.0)
        assert sealed == [1, 2]
    finally:
        pipe.close_and_join(timeout_s=5.0)


def test_notify_stage25_does_not_end_search_critical() -> None:
    src = inspect.getsource(R008HostLoop.notify_stage25_start)
    assert "async_seal.end_search_critical" not in src
    assert "self._async_seal.end_search_critical" not in src
    src_run = inspect.getsource(R008HostLoop.run_one)
    idx_safe = src_run.find('self._phase("SAFE_RETURN")')
    assert idx_safe > 0
    assert "end_search_critical()" in src_run[idx_safe:]


def test_qual_end_search_critical_allows_seal_without_stage25() -> None:
    started = threading.Event()

    def seal_fn(result: _Result) -> _Result:
        started.set()
        return result

    pipe = AsyncSealPipeline(seal_fn=seal_fn, advance_fn=lambda _r: None)
    try:
        pipe.begin_search_critical()  # ARM of QUAL
        job = pipe.submit(
            _Result(1, kind="QUALIFICATION"),
            ticket=object(),
            needs_sync=False,
            hold_until_path60=False,
        )
        assert not started.wait(timeout=0.1), "still critical after QUAL ARM"
        pipe.end_search_critical()  # host releases after QUAL submit
        assert job.future_done.wait(timeout=2.0)
        assert started.is_set()
        assert job.error is None
    finally:
        pipe.close_and_join(timeout_s=5.0)


def test_worker_phase_state_isolated_from_next_attempt_on_main_thread() -> None:
    """Regression for the live 2026-08-04 Wave3 seq11 reason=43 investigation."""

    loop = object.__new__(R008HostLoop)
    loop.events = []
    loop._phase_origin_s = None
    loop._phase_last_s = None
    loop._phase_marks = {}
    loop._stage25_marked = False

    loop._begin_phase_clock()
    loop._phase("HOME")
    loop._phase("DISPATCH")
    loop._phase("SAFE_RETURN")
    attempt_1_snapshot = {
        "marks": dict(loop._phase_marks),
        "origin_s": loop._phase_origin_s,
        "last_s": loop._phase_last_s,
    }
    assert "SEAL" not in attempt_1_snapshot["marks"]

    worker_saw_marks: dict[str, float] = {}

    def seal_fn(result: _Result) -> _Result:
        time.sleep(0.05)
        loop._phase("SEAL")
        worker_saw_marks.update(loop._phase_marks)
        return result

    pipe = AsyncSealPipeline(seal_fn=seal_fn, advance_fn=lambda record: None)
    try:
        job = pipe.submit(
            _Result(1),
            ticket=object(),
            needs_sync=False,
            before_seal=lambda: loop._seed_phase_state_for_worker(attempt_1_snapshot),
        )

        loop._begin_phase_clock()
        loop._phase("HOME")
        assert "SEAL" not in loop._phase_marks, (
            "attempt 1's worker-thread SEAL mark leaked into attempt 2's dict"
        )

        job.future_done.wait(timeout=2.0)
        assert job.error is None
    finally:
        pipe.close_and_join(timeout_s=5.0)

    assert "HOME" in worker_saw_marks and "SEAL" in worker_saw_marks
    assert "DISPATCH" in worker_saw_marks
    assert "SEAL" not in loop._phase_marks
    assert "DISPATCH" not in loop._phase_marks
