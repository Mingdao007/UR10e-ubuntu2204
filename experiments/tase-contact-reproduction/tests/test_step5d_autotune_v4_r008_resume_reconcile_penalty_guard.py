"""A stale inflight with an existing hard-stop-penalty row must not be
resubmitted by reconcile_inflight_after_home() -- it must be completed
instead.

Context (2026-08-06): reconcile_inflight_after_home() exists for genuinely
ambiguous crashes (process died, outcome unknown) and resubmits the
candidate at the head of the queue. But a recoverable hard-stop (reason_code
61/62/63) already seals a penalty row for that exact dispatch_sequence
before the host process exits; if queue.complete() did not durably land
before teardown, the next process saw a stale non-null inflight and treated
a KNOWN, already-punished failure as an ambiguous crash. Confirmed live:
dispatch 18 hard-stopped and was penalized, queue.complete() did not land,
the next process's reconcile resubmitted the identical candidate as
dispatch 19 -- which hard-stopped again, then again as dispatch 20 (same
point_key all three times). See R008HostLoop._reconcile_or_complete_stale_inflight.
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r008.live_adapter import R008HostLoop  # noqa: E402
from step5d_autotune_v4_r008.hard_stop_penalty import (  # noqa: E402
    record_hard_stop_penalty_for_run,
)


class _FakeInflight:
    def __init__(self, dispatch_sequence: int) -> None:
        self.dispatch_sequence = dispatch_sequence


class _FakeReconciledEntry:
    logical_uid = "logical:v1:fake-ambiguous-resubmit"


class _FakeQueue:
    def __init__(self, *, dispatch_sequence: int, reconcile_result=None, raise_on_reconcile=False) -> None:
        self.inflight = _FakeInflight(dispatch_sequence)
        self.completed: list[tuple[object, str, str]] = []
        self._reconcile_result = reconcile_result
        self._raise_on_reconcile = raise_on_reconcile
        self.reconcile_calls = 0

    def complete(self, ticket: object, *, status: str, detail: str = "") -> None:
        self.completed.append((ticket, status, detail))
        self.inflight = None

    def reconcile_inflight_after_home(self):
        self.reconcile_calls += 1
        if self._raise_on_reconcile:
            raise AssertionError(
                "must not resubmit a dispatch that already has a hard-stop penalty row"
            )
        return self._reconcile_result


def _bare_loop(*, run_dir: Path, queue: _FakeQueue) -> R008HostLoop:
    loop = object.__new__(R008HostLoop)
    loop.events = []
    loop.queue = queue

    class _FakeLedger:
        path = str(run_dir / "r006-observations.jsonl")

    loop.ledger = _FakeLedger()
    return loop


def test_stale_inflight_with_existing_penalty_completes_not_resubmits(tmp_path: Path) -> None:
    record_hard_stop_penalty_for_run(
        tmp_path,
        dispatch_sequence=18,
        attempt_sequence=18,
        kind="SPACEFILL",
        point_key=[15, -3, -3, "ON", 26, 5, 0],
        reason_code=61,
        reason="hard_abs_normal_60n",
    )
    queue = _FakeQueue(dispatch_sequence=18, raise_on_reconcile=True)
    loop = _bare_loop(run_dir=tmp_path, queue=queue)

    result = loop._reconcile_or_complete_stale_inflight()

    assert result is loop._STALE_INFLIGHT_ALREADY_COMPLETED
    assert queue.reconcile_calls == 0
    assert len(queue.completed) == 1
    ticket, status, detail = queue.completed[0]
    assert status == "SAFETY_OR_RETURN_FAILURE"
    assert "dispatch=18" in detail
    assert any("R008_RESUME_INFLIGHT_KNOWN_HARD_STOP_COMPLETE" in e for e in loop.events)


def test_stale_inflight_without_penalty_still_reconciles(tmp_path: Path) -> None:
    """Regression guard: the genuinely-ambiguous-crash case must still work."""

    sentinel = _FakeReconciledEntry()
    queue = _FakeQueue(dispatch_sequence=99, reconcile_result=sentinel)
    loop = _bare_loop(run_dir=tmp_path, queue=queue)

    result = loop._reconcile_or_complete_stale_inflight()

    assert result is sentinel
    assert queue.reconcile_calls == 1
    assert queue.completed == []


def test_stale_inflight_without_penalty_and_ambiguous_reconcile_returns_none(tmp_path: Path) -> None:
    queue = _FakeQueue(dispatch_sequence=99, reconcile_result=None)
    loop = _bare_loop(run_dir=tmp_path, queue=queue)

    result = loop._reconcile_or_complete_stale_inflight()

    assert result is None
    assert queue.reconcile_calls == 1
    assert queue.completed == []


def test_reconcile_method_missing_returns_sentinel(tmp_path: Path) -> None:
    class _NoReconcileQueue:
        def __init__(self) -> None:
            self.inflight = _FakeInflight(99)

    loop = _bare_loop(run_dir=tmp_path, queue=_NoReconcileQueue())

    result = loop._reconcile_or_complete_stale_inflight()

    assert result is loop._RECONCILE_METHOD_MISSING
