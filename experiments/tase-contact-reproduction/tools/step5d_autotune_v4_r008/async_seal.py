"""Ordered async seal/tell pipeline for R008 HostLoop.

Keeps r005 ``runtime.py`` frozen (source-closure). Physical motion stays on the
main thread; heavy seal/tell/complete/advance runs on a single worker so the
next attempt can HOME/DISPATCH after SAFE_RETURN without waiting for disk I/O.

Seal work is gated out of contact_search *and* PATH60 (TP freshness / reason=43):
the worker defers ``seal_fn`` while search-critical is set. PATH-kind seals use
``hold_until_path60`` so they never start in the HOME/DISPATCH gap; the host
releases via ``end_search_critical`` at the *next* attempt's SAFE_RETURN (after
PATH), not at STAGE25 — live 2026-08-05 canaries showed PATH60‖seal still trips
reason=43 even with fork+tail_rows=1. Already-executing seals are joined at
``begin_search_critical`` so CPU load cannot span the 80 ms freshness window.

QUALIFICATION releases the gate immediately (no PATH / no STAGE25).

Ledger appends remain strictly ordered by attempt_sequence (FIFO worker).

Do not widen PACKET_STALE_S (80 ms) to hide seal cost — that would need ~15–35 s.

Full async contract (authority, ask no-join, joins, closed non-goals):
``docs/r008_async_seal_contract.md``.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Callable

# Do not widen frozen r004 PACKET_STALE_S (0.080). Live PATH60‖seal canaries
# needed ~15–35 s held-age to cover seal_wall — wrong lever for reason=43.
PACKET_STALE_S_MUST_REMAIN = 0.080
WIDEN_TO_ABSORB_SEAL_WOULD_NEED_S = (15.0, 35.0)

# Kinds that reach STAGE25 / PATH60. QUALIFICATION never does — do not hold it.
_PATH60_HOLD_KINDS = frozenset(
    {
        "ANCHOR",
        "STAIRCASE",
        "SPACEFILL",
        "BO",
        "BO_TRIAL",  # live enqueue/submit kind (≠ phase synonym "BO")
        "RETEST",
    }
)


def kind_holds_seal_until_path60(kind: str) -> bool:
    """True when seal/tell must wait for the next attempt's SAFE_RETURN.

    Name is historical (``hold_until_path60``); release is post-PATH, not STAGE25.
    """

    return str(kind).upper() in _PATH60_HOLD_KINDS


@dataclass
class SealJob:
    result: Any
    ticket: Any
    attempt_sequence: int
    kind: str
    submitted_s: float
    needs_sync: bool
    future_done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None
    record: Any | None = None
    seal_finished_s: float | None = None
    motion_started_s: float | None = None  # next HOME (legacy / debug)
    path60_started_s: float | None = None  # end_search_critical (SAFE_RETURN; name historical)
    held_for_path: bool = False  # per-job hold; cleared by end_search_critical
    blocked_waiting_s: float = 0.0
    before_seal: Callable[[], None] | None = None


@dataclass
class SealOverlapMetrics:
    seal_wall_s: float | None
    seal_overlap_s: float | None
    motion_blocked_on_seal_s: float
    attempt_sequence: int | None = None


class AsyncSealPipeline:
    """Single-worker FIFO seal committer with search-critical gating.

    Caller clears queue inflight (``queue.complete``) on the motion thread
    before submit so the next attempt can HOME while this job is queued.
    ``seal_fn`` itself starts only when search-critical is clear (PATH60+).
    """

    def __init__(
        self,
        *,
        seal_fn: Callable[[Any], Any],
        advance_fn: Callable[[Any], None],
        on_done: Callable[[SealJob], None] | None = None,
        join_executing_on_search_critical: bool = True,
        search_critical_gates_seals: bool = True,
    ) -> None:
        self._seal_fn = seal_fn
        self._advance_fn = advance_fn
        self._on_done = on_done
        # When seal CPU lives in an off-host daemon, seal_fn is mostly an IPC
        # wait (GIL released). Joining it at ARM would re-serialize cycles, and
        # gating new starts on search-critical would push seal to next SAFE_RETURN.
        self._join_executing_on_search_critical = bool(join_executing_on_search_critical)
        self._search_critical_gates_seals = bool(search_critical_gates_seals)
        self._q: deque[SealJob] = deque()
        self._cond = threading.Condition()
        self._closed = False
        self._search_critical = False
        self._inflight: SealJob | None = None
        self._seal_executing: SealJob | None = None
        self._worker = threading.Thread(
            target=self._run, name="r008-async-seal", daemon=True
        )
        self._worker.start()
        self._last_metrics: SealOverlapMetrics | None = None

    def close_and_join(self, timeout_s: float = 600.0) -> None:
        self.join_all()
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self._worker.join(timeout=timeout_s)

    def join_all(self) -> None:
        """Drain-only barrier for stop/terminal — never call on the motion path.

        Continuous-run HostLoop archives motion-path joins; open seals must
        finish before campaign exit so ledger/sidecar stay complete.
        Releases search-critical so deferred jobs can run.
        """

        with self._cond:
            self._search_critical = False
            for job in list(self._q):
                job.held_for_path = False
            if self._inflight is not None:
                self._inflight.held_for_path = False
            self._cond.notify_all()
        while True:
            with self._cond:
                targets = [j for j in self._q if not j.future_done.is_set()]
                if self._inflight is not None and not self._inflight.future_done.is_set():
                    targets.append(self._inflight)
            if not targets:
                return
            wait_job = targets[0]
            self._wait_job_done(wait_job)

    def join_executing(self) -> float:
        """Wait for a seal_fn that is already inside ``seal_fn`` (if any).

        Used before HOME so the post-SAFE_RETURN seal drain happens outside
        the ARM/search-critical freshness window. Does not set
        ``_search_critical`` (unlike ``begin_search_critical``).
        Returns seconds spent waiting (0.0 if nothing was executing).
        """

        with self._cond:
            executing = self._seal_executing
        if executing is not None and not executing.future_done.is_set():
            t0 = time.perf_counter()
            self._wait_job_done(executing)
            return time.perf_counter() - t0
        return 0.0

    def begin_search_critical(self) -> float:
        """Defer new seal_fn starts; optionally join any seal_fn already executing.

        Called at CONTACT_SEARCH_START (ARM) so a still-running seal job cannot
        starve the RTDE/writer freshness window (reason=43, canary "023243").
        Returns the wall-clock seconds actually spent joining an in-flight job
        (0.0 if nothing was executing) so the caller can log it directly --
        ``SealJob.blocked_waiting_s`` is updated by this same call but ``_run``'s
        ``_on_done`` fires (and logs) *before* ``future_done.set()`` releases
        this join, so the per-job event can under-report a wait that is still
        in progress when it is written. Log this return value at the call site
        instead of relying on that event's ``blocked`` field (canary "025746":
        both jobs logged ``blocked=0.0`` and it was inconclusive whether the
        join fired).

        Prefer draining via ``join_executing`` before HOME so this returns ~0
        under the seal-join cut (canary 041810 arm_s≈9.2 hitch).
        """

        if not self._search_critical_gates_seals:
            return 0.0
        with self._cond:
            self._search_critical = True
            executing = self._seal_executing
        if (
            self._join_executing_on_search_critical
            and executing is not None
            and not executing.future_done.is_set()
        ):
            t0 = time.perf_counter()
            self._wait_job_done(executing)
            return time.perf_counter() - t0
        return 0.0

    def end_search_critical(self) -> None:
        """Allow deferred seals to run (post-PATH SAFE_RETURN / QUAL release)."""

        now = time.perf_counter()
        with self._cond:
            self._search_critical = False
            targets: list[SealJob] = []
            if self._inflight is not None:
                targets.append(self._inflight)
            targets.extend(self._q)
            for job in targets:
                if not job.future_done.is_set():
                    job.held_for_path = False
                    if job.path60_started_s is None:
                        job.path60_started_s = now
            self._cond.notify_all()

    def submit(
        self,
        result: Any,
        ticket: Any,
        *,
        needs_sync: bool,
        before_seal: Callable[[], None] | None = None,
        hold_until_path60: bool = False,
    ) -> SealJob:
        """Enqueue a seal job.

        ``hold_until_path60=True`` marks *this* job held (not a global lock), so
        a SAFE_RETURN ``end_search_critical`` can release an older job without
        the newer submit immediately re-blocking it. Seal still cannot start
        while ``begin_search_critical`` is active.
        """

        job = SealJob(
            result=result,
            ticket=ticket,
            attempt_sequence=int(result.attempt_sequence),
            kind=str(result.kind),
            submitted_s=time.perf_counter(),
            needs_sync=needs_sync,
            before_seal=before_seal,
            held_for_path=bool(hold_until_path60),
        )
        with self._cond:
            if self._closed:
                raise RuntimeError("async seal pipeline is closed")
            self._q.append(job)
            self._cond.notify()
        if needs_sync:
            self._wait_job_done(job)
        return job

    def mark_next_motion_start(self) -> None:
        """Call at the start of the next attempt's HOME (debug / legacy mark)."""

        now = time.perf_counter()
        with self._cond:
            targets: list[SealJob] = []
            if self._inflight is not None:
                targets.append(self._inflight)
            elif self._q:
                targets.append(self._q[0])
            for job in targets:
                if job.motion_started_s is None and not job.future_done.is_set():
                    job.motion_started_s = now

    def last_metrics(self) -> SealOverlapMetrics | None:
        return self._last_metrics

    def wait_done(self, job: SealJob) -> None:
        """Host barrier for a single job (QUAL baseline / stop helpers only)."""

        self._wait_job_done(job)

    def _wait_job_done(self, job: SealJob) -> None:
        """Motion/stop wait: charge blocked time and refresh metrics."""

        t0 = time.perf_counter()
        job.future_done.wait()
        dt = time.perf_counter() - t0
        job.blocked_waiting_s += dt
        self._refresh_blocked_metrics(job)
        if job.error is not None:
            raise job.error

    def _refresh_blocked_metrics(self, job: SealJob) -> None:
        """Include motion-thread waits that finish after the worker emits metrics.

        Bug found 2026-08-05 (canary "025746"): with two jobs finishing close
        together, this used to overwrite ``_last_metrics`` unconditionally --
        job A's late-arriving blocked time could clobber job B's already-logged
        row if B became ``_last_metrics`` in between. Only refresh when
        ``_last_metrics`` still belongs to *this* job.
        """

        last = self._last_metrics
        if last is None or job.seal_finished_s is None:
            return
        if last.attempt_sequence != job.attempt_sequence:
            return
        self._last_metrics = replace(
            last, motion_blocked_on_seal_s=float(job.blocked_waiting_s)
        )

    def _wait_until_seal_allowed(self, job: SealJob) -> bool:
        """Wait until search-critical and per-job path-hold clear."""

        with self._cond:
            while (self._search_critical or job.held_for_path) and not self._closed:
                self._cond.wait()
            if self._closed and (self._search_critical or job.held_for_path):
                # Closed while still gated without join_all clearing it.
                return False
            self._seal_executing = job
            return True

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._q and not self._closed:
                    self._cond.wait()
                if not self._q and self._closed:
                    return
                job = self._q.popleft()
                self._inflight = job
            if not self._wait_until_seal_allowed(job):
                job.error = RuntimeError("async seal pipeline closed during search gate")
                job.seal_finished_s = time.perf_counter()
                self._last_metrics = self._metrics_for(job)
                job.future_done.set()
                with self._cond:
                    if self._inflight is job:
                        self._inflight = None
                    self._seal_executing = None
                    self._cond.notify_all()
                continue
            try:
                if job.before_seal is not None:
                    job.before_seal()
                record = self._seal_fn(job.result)
                self._advance_fn(record)
                job.record = record
            except BaseException as exc:  # noqa: BLE001 — surface to host loop
                job.error = exc
            finally:
                job.seal_finished_s = time.perf_counter()
                self._last_metrics = self._metrics_for(job)
                if self._on_done is not None:
                    try:
                        self._on_done(job)
                    except Exception as cb_exc:  # noqa: BLE001
                        if job.error is None:
                            job.error = cb_exc
                job.future_done.set()
                with self._cond:
                    if self._inflight is job:
                        self._inflight = None
                    if self._seal_executing is job:
                        self._seal_executing = None
                    self._cond.notify_all()

    @staticmethod
    def _metrics_for(job: SealJob) -> SealOverlapMetrics:
        if job.seal_finished_s is None:
            return SealOverlapMetrics(
                None, None, job.blocked_waiting_s, attempt_sequence=job.attempt_sequence
            )
        seal_wall = max(0.0, job.seal_finished_s - job.submitted_s)
        # Useful overlap = seal wall after end_search_critical (SAFE_RETURN+).
        # HOME-while-joined / pre-release hold must NOT count as overlap.
        overlap = None
        if job.path60_started_s is not None:
            start = max(job.submitted_s, job.path60_started_s)
            overlap = max(0.0, job.seal_finished_s - start)
        return SealOverlapMetrics(
            seal_wall_s=seal_wall,
            seal_overlap_s=overlap,
            motion_blocked_on_seal_s=job.blocked_waiting_s,
            attempt_sequence=job.attempt_sequence,
        )


def phase_needs_sync_seal(phase: Any, kind: str) -> bool:
    """Always False: motion never waits on seal/tell/MAE (continuous-run lock).

    Phase/threshold side-effects are applied on the motion thread via mailbox
    after seal completes; overshoot of not-yet-dispatched tickets is allowed.
    ``phase``/``kind`` are accepted for call-site compatibility.
    """

    _ = phase, kind
    return False


def dispatch_s_hides_seal_wall(
    dispatch_s: float | None,
    seal_wall_s: float | None,
    *,
    ratio_threshold: float = 0.85,
    abs_slack_s: float = 2.0,
) -> bool:
    """True when HOME→DISPATCH ≈ prior seal wall (false \"overlap\" / serial barrier)."""

    if dispatch_s is None or seal_wall_s is None:
        return False
    if seal_wall_s <= 0.0 or dispatch_s <= 0.0:
        return False
    return dispatch_s + abs_slack_s >= ratio_threshold * seal_wall_s


__all__ = [
    "AsyncSealPipeline",
    "PACKET_STALE_S_MUST_REMAIN",
    "SealJob",
    "SealOverlapMetrics",
    "WIDEN_TO_ABSORB_SEAL_WOULD_NEED_S",
    "dispatch_s_hides_seal_wall",
    "kind_holds_seal_until_path60",
    "phase_needs_sync_seal",
]
