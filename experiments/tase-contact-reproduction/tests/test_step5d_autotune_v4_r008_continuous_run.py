"""Continuous-run: cursors, mailbox ownership, no motion join_all."""

from __future__ import annotations

import queue
from types import SimpleNamespace
from typing import Any

from step5d_autotune_v4_r005.runtime import CampaignPhase
from step5d_autotune_v4_r008.live_adapter import (
    HostMailboxIntent,
    PENDING_WATERMARK,
    R008HostLoop,
)


class _FakeQueue:
    def __init__(self) -> None:
        self._pending: list[Any] = []
        self.inflight = None
        self.cancelled_reasons: list[str] = []

    def pending(self) -> tuple[Any, ...]:
        return tuple(self._pending)

    def enqueue(self, candidate: Any, *, kind: str, epoch: int) -> None:
        self._pending.append(SimpleNamespace(candidate=candidate, kind=kind, epoch=epoch))

    def cancel_pending(self, *, reason: str) -> tuple[Any, ...]:
        self.cancelled_reasons.append(reason)
        out = tuple(self._pending)
        self._pending.clear()
        return out

    def prepare_next(self) -> Any:
        if not self._pending:
            return None
        return self._pending.pop(0)


def _bare_loop() -> R008HostLoop:
    loop = object.__new__(R008HostLoop)
    loop.events = []
    loop.epoch = 1
    loop.cursor = object()
    loop.phase = CampaignPhase.QUALIFICATION
    loop.queue = _FakeQueue()
    loop._mailbox = queue.SimpleQueue()
    loop._qual_scheduled = 0
    loop._anchor_scheduled = 0
    loop._staircase_scheduled = 0
    loop._spacefill_scheduled = 0
    loop._bo_scheduled = 0
    loop._retest_scheduled = 0
    loop._ANCHOR = R008HostLoop._ANCHOR
    loop._STAIRCASE = R008HostLoop._STAIRCASE
    loop._SPACEFILL = R008HostLoop._SPACEFILL
    loop.qualification_passes = 0
    loop._anchor_verdict = None
    loop._anchor_max_attempts = 9
    loop._anchor_point = SimpleNamespace(
        to_parameter_point=lambda: SimpleNamespace()
    )
    loop._staircase = ()
    loop._spacefill = ()
    loop._retest_records = []
    loop.retest_candidate = None
    loop.stop_reason = None

    def _stop(reason: str) -> None:
        loop.phase = CampaignPhase.INCOMPLETE_STOPPED
        loop.stop_reason = reason

    loop._stop = _stop  # type: ignore[method-assign]
    loop._phase_name = lambda phase: phase.value if hasattr(phase, "value") else str(phase)
    loop._consume_operator_next = lambda: None
    return loop


def test_pending_watermark_is_two() -> None:
    assert PENDING_WATERMARK == 2


def test_queue_request_increments_cursor_once() -> None:
    from step5d_autotune_v4_r006.live_adapter import R006Candidate

    loop = _bare_loop()
    cand = R006Candidate()
    loop.cursor = cand
    R008HostLoop._queue_request(loop, cand, kind="QUALIFICATION", base=cand)
    R008HostLoop._queue_request(loop, cand, kind="QUALIFICATION", base=cand)
    assert loop._qual_scheduled == 2
    assert len(loop.queue.pending()) == 2


def test_refill_qual_uses_scheduled_cursor_not_sealed_count() -> None:
    from step5d_autotune_v4_r006.live_adapter import R006Candidate

    loop = _bare_loop()
    loop.cursor = R006Candidate()
    loop.qualification_passes = 0  # sealed still 0
    loop._qual_scheduled = 1  # one already scheduled historically
    R008HostLoop._refill(loop)
    # Tops pending to watermark=2 using scheduled cursor (not sealed passes).
    assert loop._qual_scheduled == 3
    assert all(item.kind == "QUALIFICATION" for item in loop.queue.pending())
    assert len(loop.queue.pending()) == 2


def test_refill_does_not_promote_qual_to_anchor_on_schedule_alone() -> None:
    from step5d_autotune_v4_r006.live_adapter import R006Candidate

    loop = _bare_loop()
    loop.cursor = R006Candidate()
    loop.qualification_passes = 0
    loop._qual_scheduled = 3
    loop.phase = CampaignPhase.QUALIFICATION
    R008HostLoop._refill(loop)
    assert loop.phase == CampaignPhase.QUALIFICATION
    assert loop.queue.pending() == ()



def test_mailbox_apply_cancels_and_sets_phase() -> None:
    loop = _bare_loop()
    from step5d_autotune_v4_r006.live_adapter import R006Candidate

    loop.cursor = R006Candidate()
    loop.queue.enqueue(R006Candidate(), kind="STAIRCASE", epoch=1)
    loop.queue.enqueue(R006Candidate(), kind="STAIRCASE", epoch=1)
    loop._staircase = (object(), object(), object())
    R008HostLoop._apply_mailbox_intent(
        loop,
        HostMailboxIntent(
            cancel_pending=True,
            reason="r008_staircase_edge",
            phase=R008HostLoop._SPACEFILL,
            close_staircase=True,
            event="R008_STAIRCASE_EDGE:test",
        ),
    )
    assert loop.phase == R008HostLoop._SPACEFILL
    assert loop._staircase_scheduled == 3
    assert loop.queue.pending() == ()
    assert loop.queue.cancelled_reasons == ["r008_staircase_edge"]
    assert "R008_STAIRCASE_EDGE:test" in loop.events


def test_join_refill_prepare_does_not_call_async_join() -> None:
    loop = _bare_loop()
    joined = {"n": 0}

    class _Seal:
        def join_all(self) -> None:
            joined["n"] += 1

    loop._async_seal = _Seal()  # type: ignore[attr-defined]
    loop.phase = CampaignPhase.QUALIFICATION
    from step5d_autotune_v4_r006.live_adapter import R006Candidate

    loop.cursor = R006Candidate()
    ticket = R008HostLoop._join_refill_prepare_next(loop)
    assert joined["n"] == 0
    assert ticket is not None
    assert loop._qual_scheduled == PENDING_WATERMARK


def test_cycle_budget_rejects_seal_hidden_in_dispatch() -> None:
    """Canary gate: motion+dispatch must not re-include prior seal wall."""

    from step5d_autotune_v4_r008.async_seal import dispatch_s_hides_seal_wall

    # Old serial host: dispatch_s ≈ seal_wall ≈ 33s → fail.
    assert dispatch_s_hides_seal_wall(33.0, 32.8) is True
    # Target PATH60 overlap: dispatch_s small, seal_wall still ~33s → pass.
    assert dispatch_s_hides_seal_wall(1.5, 32.8) is False
    contact_search_s = 19.0
    path_60_s = 65.0
    dispatch_s = 1.5
    cycle_proxy = contact_search_s + path_60_s + dispatch_s
    assert 80.0 <= cycle_proxy <= 95.0

