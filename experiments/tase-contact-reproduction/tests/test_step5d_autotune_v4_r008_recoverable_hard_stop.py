"""reason_code 61/62/63 seals a penalty and recycles the host (new writer).

In-process continue after 61 is unsafe: the mature writer is fail-closed and
``home()`` is verify-only → ``mature writer is not verified at Home``. Proven
recovery is host recycle via overnight autoresume. See
``R008HostLoop._stop_or_recover_from_run_one_fault``.
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
    dispatch_sequence = 42


class _FakeQueue:
    def __init__(self, *, with_inflight: bool = True) -> None:
        self.inflight = _FakeInflight() if with_inflight else None
        self.completed: list[tuple[object, str, str]] = []

    def complete(self, dispatch: object, *, status: str, detail: str = "") -> None:
        self.completed.append((dispatch, status, detail))
        self.inflight = None


class _FakeLedger:
    def __init__(self, path: str, records=()) -> None:
        self.path = path
        self.records = list(records)


class _FakeRecord:
    def __init__(self, attempt_sequence: int) -> None:
        self.attempt_sequence = attempt_sequence


def _bare_loop(*, with_inflight: bool = True) -> R008HostLoop:
    loop = object.__new__(R008HostLoop)
    loop.events = []
    loop._recoverable_hard_stop_streak = 0
    loop._stopped_with: list[str] = []
    loop.queue = _FakeQueue(with_inflight=with_inflight)

    def _stop(reason: str) -> None:
        loop._stopped_with.append(reason)

    loop._stop = _stop  # type: ignore[method-assign]
    return loop


def _bare_loop_with_run_dir(
    run_dir: Path, *, last_sealed_attempt_sequence: int = 0, with_inflight: bool = True
) -> R008HostLoop:
    """Like _bare_loop but wired to a real run dir + ledger, so the durable
    penalty-ledger streak computation (not the in-memory counter) is what
    actually decides recycle-vs-escalate -- matching real deployment, where
    every recoverable hard-stop tears the process down and restarts it.
    """

    loop = _bare_loop(with_inflight=with_inflight)
    records = (
        [_FakeRecord(last_sealed_attempt_sequence)]
        if last_sealed_attempt_sequence
        else []
    )
    loop.ledger = _FakeLedger(str(run_dir / "r006-observations.jsonl"), records)
    return loop


_FAR005_DISP42_EXC = RuntimeError(
    "r006 stop-dominant packet: reason_code=61 reason=hard_abs_normal_60n; "
    'stop_diag={"tp_state": 25, "force_norm_n": 61.12}'
)


def test_single_reason61_recycles_and_completes_inflight() -> None:
    loop = _bare_loop()
    loop._stop_or_recover_from_run_one_fault("unexpected_code_fault:", _FAR005_DISP42_EXC)
    assert len(loop._stopped_with) == 1
    assert loop._stopped_with[0].startswith("recoverable_hard_stop_recycle:")
    assert "reason_code=61" in loop._stopped_with[0]
    assert loop._recoverable_hard_stop_streak == 1
    assert any("R008_RECOVERABLE_HARD_STOP" in e for e in loop.events)
    assert any("R008_RECOVERABLE_INFLIGHT_COMPLETE" in e for e in loop.events)
    assert loop.queue.completed
    assert loop.queue.completed[0][1] == "SAFETY_OR_RETURN_FAILURE"


def test_streak_up_to_limit_uses_recycle_stop(tmp_path: Path) -> None:
    """RECOVERABLE_HARD_STOP_LIMIT prior *process restarts* (not in-process
    calls -- every recoverable hard-stop tears the process down) still
    recycle. Seed the durable penalty ledger as if LIMIT-1 prior restarts
    already hard-stopped, then record + evaluate this attempt's own trip
    (matching run_one()'s real order: _maybe_record_hard_stop_penalty runs
    before _stop_or_recover_from_run_one_fault)."""

    loop = _bare_loop_with_run_dir(tmp_path)
    limit = loop.RECOVERABLE_HARD_STOP_LIMIT
    for seq in range(1, limit):  # LIMIT-1 prior restarts already penalized
        record_hard_stop_penalty_for_run(
            tmp_path,
            dispatch_sequence=seq,
            attempt_sequence=seq,
            kind="SPACEFILL",
            point_key=[1, 1, 1, "ON", 1, 1, 1],
            reason_code=61,
            reason="hard_abs_normal_60n",
        )
    # This process's own trip (attempt_sequence == limit): matches the real
    # run_one() ordering where the penalty is sealed before this decision.
    record_hard_stop_penalty_for_run(
        tmp_path,
        dispatch_sequence=limit,
        attempt_sequence=limit,
        kind="SPACEFILL",
        point_key=[1, 1, 1, "ON", 1, 1, 1],
        reason_code=61,
        reason="hard_abs_normal_60n",
    )
    loop.queue.inflight = _FakeInflight()
    loop._stop_or_recover_from_run_one_fault("unexpected_code_fault:", _FAR005_DISP42_EXC)

    assert len(loop._stopped_with) == 1
    assert loop._stopped_with[0].startswith("recoverable_hard_stop_recycle:")
    assert f"streak={limit}:" in loop._stopped_with[0]


def test_streak_exceeding_limit_escalates_to_fatal_stop(tmp_path: Path) -> None:
    loop = _bare_loop_with_run_dir(tmp_path)
    limit = loop.RECOVERABLE_HARD_STOP_LIMIT
    for seq in range(1, limit + 1):  # LIMIT prior restarts already penalized
        record_hard_stop_penalty_for_run(
            tmp_path,
            dispatch_sequence=seq,
            attempt_sequence=seq,
            kind="SPACEFILL",
            point_key=[1, 1, 1, "ON", 1, 1, 1],
            reason_code=61,
            reason="hard_abs_normal_60n",
        )
    # This process's own trip is the (LIMIT+1)th consecutive -- trips the breaker.
    record_hard_stop_penalty_for_run(
        tmp_path,
        dispatch_sequence=limit + 1,
        attempt_sequence=limit + 1,
        kind="SPACEFILL",
        point_key=[1, 1, 1, "ON", 1, 1, 1],
        reason_code=61,
        reason="hard_abs_normal_60n",
    )
    loop.queue.inflight = _FakeInflight()
    loop._stop_or_recover_from_run_one_fault("unexpected_code_fault:", _FAR005_DISP42_EXC)

    assert len(loop._stopped_with) == 1
    assert loop._stopped_with[0].startswith("unexpected_code_fault:")
    assert any("R008_HARD_STOP_CIRCUIT_BREAKER_TRIPPED" in e for e in loop.events)


def test_success_between_trips_resets_streak_so_breaker_never_trips(tmp_path: Path) -> None:
    """Many hard-stops across a whole overnight session never trip the
    breaker as long as each one is bracketed by a real sealed success --
    the streak resets against the *last sealed success*, not a raw total."""

    limit = R008HostLoop.RECOVERABLE_HARD_STOP_LIMIT
    for round_ in range(limit + 5):
        seq = round_ * 2 + 1
        loop = _bare_loop_with_run_dir(tmp_path, last_sealed_attempt_sequence=seq)
        record_hard_stop_penalty_for_run(
            tmp_path,
            dispatch_sequence=seq + 1,
            attempt_sequence=seq + 1,
            kind="SPACEFILL",
            point_key=[1, 1, 1, "ON", 1, 1, 1],
            reason_code=61,
            reason="hard_abs_normal_60n",
        )
        loop.queue.inflight = _FakeInflight()
        loop._stop_or_recover_from_run_one_fault(
            "unexpected_code_fault:", _FAR005_DISP42_EXC
        )
        assert len(loop._stopped_with) == 1
        assert loop._stopped_with[0].startswith("recoverable_hard_stop_recycle:"), (
            round_,
            loop._stopped_with,
        )
        assert "streak=1:" in loop._stopped_with[0]


def test_sensor_stale_is_still_immediately_fatal() -> None:
    loop = _bare_loop()
    exc = RuntimeError(
        "r006 stop-dominant packet: reason_code=3 reason=sensor_stale;"
    )
    loop._stop_or_recover_from_run_one_fault("unexpected_code_fault:", exc)
    assert len(loop._stopped_with) == 1
    assert loop._stopped_with[0].startswith("unexpected_code_fault:")
    assert loop._recoverable_hard_stop_streak == 0


def test_unrelated_exception_without_reason_code_is_still_fatal() -> None:
    loop = _bare_loop()
    exc = OSError("connection reset")
    loop._stop_or_recover_from_run_one_fault("runtime_or_evidence_fault:", exc)
    assert len(loop._stopped_with) == 1
    assert loop._stopped_with[0].startswith("runtime_or_evidence_fault:")
