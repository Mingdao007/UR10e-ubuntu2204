"""Offline fault-injection tests for the contact-yield recovery seam."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from contact_yield_recovery_contract import (
    AttemptIdentity,
    FaultKind,
    RecoveryBlockedError,
    RecoveryContractError,
    RecoveryLifecycle,
    RecoveryOwnershipError,
    RecoveryState,
    WriterSessionRegistry,
)


ROOT = Path(__file__).resolve().parents[1]


def _identity(session: str, sequence: int) -> AttemptIdentity:
    return AttemptIdentity(
        session_epoch=4,
        resident_session_id=session,
        attempt_id=f"contact-attempt-{sequence}",
        attempt_sequence=sequence,
    )


def _start(lifecycle: RecoveryLifecycle, identity: AttemptIdentity) -> None:
    lifecycle.prepare_attempt(identity)
    lifecycle.start_attempt(identity)


def _quiescent(lifecycle: RecoveryLifecycle, identity: AttemptIdentity) -> bool:
    return lifecycle.observe_quiescence(
        safety_mode="PROTECTIVE_STOP",
        stationary=True,
        fresh=True,
        session_epoch=identity.session_epoch,
        resident_session_id=identity.resident_session_id,
        attempt_id=identity.attempt_id,
        observed_at_s=2.0,
    )


def test_60n_guard_seals_first_fault_and_automatically_enters_home() -> None:
    events: list[tuple[str, str]] = []
    lifecycle = RecoveryLifecycle(
        session_epoch=4,
        resident_session_id="guard-session",
        home_transition=lambda sealed: events.append(("home", sealed.identity.attempt_id)) or True,
    )
    identity = _identity("guard-session", 1)
    _start(lifecycle, identity)
    lifecycle.record_sample({"attempt": identity.attempt_id, "sample": 1})

    with pytest.raises(RecoveryContractError, match="threshold"):
        lifecycle.guard_trip(59.999, observed_at_s=1.0)
    sealed = lifecycle.guard_trip(60.0, observed_at_s=1.0)

    assert lifecycle.state is RecoveryState.HOME
    assert sealed.identity == identity
    assert sealed.first_fault.kind is FaultKind.GUARD_60N
    assert sealed.first_fault.force_norm_n == 60.0
    assert lifecycle.first_fault == sealed.first_fault
    assert events == [("home", identity.attempt_id)]
    assert lifecycle.transitions == (
        RecoveryState.HOME,
        RecoveryState.ATTEMPT_ACTIVE,
        RecoveryState.SEALED,
        RecoveryState.HOME_PENDING,
        RecoveryState.HOME,
    )
    lifecycle.close()


def test_protective_stop_requires_quiescence_and_an_unlock_decision_before_home() -> None:
    home_events: list[str] = []
    lifecycle = RecoveryLifecycle(
        session_epoch=4,
        resident_session_id="protective-session",
        home_transition=lambda _sealed: home_events.append("home") or True,
    )
    identity = _identity("protective-session", 1)
    _start(lifecycle, identity)
    lifecycle.record_sample({"attempt": identity.attempt_id, "sample": "before-stop"})
    sealed = lifecycle.protective_stop(observed_at_s=1.0)

    assert lifecycle.state is RecoveryState.QUIESCENCE_REQUIRED
    assert not lifecycle.observe_quiescence(
        safety_mode="NORMAL",
        stationary=True,
        fresh=True,
        session_epoch=identity.session_epoch,
        resident_session_id=identity.resident_session_id,
        attempt_id=identity.attempt_id,
        observed_at_s=2.0,
    )
    assert lifecycle.state is RecoveryState.QUIESCENCE_REQUIRED
    assert _quiescent(lifecycle, identity)
    assert lifecycle.state is RecoveryState.UNLOCK_DECISION

    assert lifecycle.decide_unlock(True, identity=sealed.identity)
    assert lifecycle.unlock_decision is True
    assert lifecycle.state is RecoveryState.HOME
    assert home_events == ["home"]
    lifecycle.close()


def test_duplicate_spawn_and_preparation_are_rejected() -> None:
    registry = WriterSessionRegistry()
    first = RecoveryLifecycle(
        session_epoch=4,
        resident_session_id="duplicate-session",
        ownership_registry=registry,
        home_transition=lambda _sealed: True,
    )
    second = RecoveryLifecycle(
        session_epoch=4,
        resident_session_id="duplicate-session",
        ownership_registry=registry,
        home_transition=lambda _sealed: True,
    )
    identity = _identity("duplicate-session", 1)

    first.prepare_attempt(identity)
    with pytest.raises(RecoveryOwnershipError, match="preparation"):
        first.prepare_attempt(identity)
    with pytest.raises(RecoveryOwnershipError, match="single writer"):
        second.prepare_attempt(identity)

    first.start_attempt(identity)
    first.guard_trip(60.0, observed_at_s=1.0)
    first.close()
    second.close()


def test_failed_home_recovery_remains_blocked_and_preserves_sealed_identity() -> None:
    lifecycle = RecoveryLifecycle(
        session_epoch=4,
        resident_session_id="blocked-session",
        home_transition=lambda _sealed: False,
    )
    identity = _identity("blocked-session", 1)
    _start(lifecycle, identity)
    lifecycle.record_sample({"attempt": identity.attempt_id, "sample": "fault"})
    sealed = lifecycle.protective_stop(observed_at_s=1.0)
    assert _quiescent(lifecycle, identity)

    assert lifecycle.decide_unlock(True, identity=identity) is False
    assert lifecycle.state is RecoveryState.BLOCKED
    assert lifecycle.sealed_attempt == sealed
    assert lifecycle.first_fault == sealed.first_fault
    with pytest.raises(RecoveryBlockedError, match="BLOCKED"):
        lifecycle.prepare_attempt(_identity("blocked-session", 2))
    lifecycle.close()


def test_recovery_does_not_concatenate_samples_from_prior_attempt() -> None:
    lifecycle = RecoveryLifecycle(
        session_epoch=4,
        resident_session_id="separation-session",
        home_transition=lambda _sealed: True,
    )
    first = _identity("separation-session", 1)
    _start(lifecycle, first)
    lifecycle.record_sample({"attempt": first.attempt_id, "sample": "old"})
    sealed = lifecycle.guard_trip(60.0, observed_at_s=1.0)

    second = _identity("separation-session", 2)
    _start(lifecycle, second)
    lifecycle.record_sample({"attempt": second.attempt_id, "sample": "new"})

    assert lifecycle.active_samples == (({"attempt": second.attempt_id, "sample": "new"}),)
    assert sealed.samples == (({"attempt": first.attempt_id, "sample": "old"}),)
    assert lifecycle.sealed_attempts == (sealed,)
    lifecycle.complete_attempt()
    lifecycle.close()


def test_atomic_checkpoint_rolls_back_a_failed_speculative_update() -> None:
    lifecycle = RecoveryLifecycle(
        session_epoch=4,
        resident_session_id="atomic-session",
    )
    identity = _identity("atomic-session", 1)
    _start(lifecycle, identity)
    before = lifecycle.snapshot()

    def failing_update(current: RecoveryLifecycle) -> None:
        current.record_sample({"attempt": identity.attempt_id, "sample": "temporary"})
        raise RuntimeError("injected writer failure")

    with pytest.raises(RuntimeError, match="injected writer failure"):
        lifecycle.atomic(failing_update)
    assert lifecycle.snapshot() == before
    lifecycle.fail_recovery("test cleanup")
    lifecycle.close()


def test_multiprocessing_observer_entrypoint_has_explicit_main_guard() -> None:
    source = (ROOT / "tools/contact_yield_supervisor.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "mp.get_context(\"spawn\")" in source or "mp.get_context('spawn')" in source
    assert any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and any(
            isinstance(comparator, ast.Constant) and comparator.value == "__main__"
            for comparator in node.test.comparators
        )
        for node in ast.walk(tree)
    )


def test_existing_writer_adapter_preserves_resident_identity() -> None:
    from contact_yield_live_writer import NativeYieldLiveWriter

    writer = NativeYieldLiveWriter.__new__(NativeYieldLiveWriter)
    writer.prerequisites = SimpleNamespace(
        session_epoch=4,
        resident_session_id="mature-writer-session",
    )
    lifecycle = RecoveryLifecycle.from_writer(writer)
    identity = _identity("mature-writer-session", 1)
    lifecycle.prepare_attempt(identity)
    assert lifecycle.owner_held
    lifecycle.start_attempt(identity)
    lifecycle.complete_attempt()
    lifecycle.close()
