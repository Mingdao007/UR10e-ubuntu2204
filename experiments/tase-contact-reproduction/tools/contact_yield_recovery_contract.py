"""Offline recovery lifecycle around the existing contact-yield writer.

This module is a state/identity seam, not a controller or a motion runtime.
The mature R006/R004 writer remains the only command writer.  A caller supplies
an observed Home transition callback for tests or for a later, separately gated
owner; this module never opens an endpoint, sends unlock, or issues motion.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
import math
import threading
from typing import Any, Callable
import weakref


GUARD_FORCE_THRESHOLD_N = 60.0


class RecoveryContractError(RuntimeError):
    """The recovery lifecycle or its identity contract was violated."""


class RecoveryStateError(RecoveryContractError):
    """An operation was attempted in the wrong recovery state."""


class RecoveryOwnershipError(RecoveryContractError):
    """A second writer tried to claim the same resident session."""


class RecoveryBlockedError(RecoveryContractError):
    """A terminal recovery failure remains fail-closed."""


class RecoveryState(str, Enum):
    HOME = "HOME"
    ATTEMPT_ACTIVE = "ATTEMPT_ACTIVE"
    SEALED = "SEALED"
    QUIESCENCE_REQUIRED = "QUIESCENCE_REQUIRED"
    UNLOCK_DECISION = "UNLOCK_DECISION"
    HOME_PENDING = "HOME_PENDING"
    BLOCKED = "BLOCKED"


class FaultKind(str, Enum):
    GUARD_60N = "guard_60n"
    PROTECTIVE_STOP = "protective_stop"
    RECOVERY_FAILURE = "recovery_failure"


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise RecoveryContractError(f"{name} must be finite numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RecoveryContractError(f"{name} must be finite numeric") from exc
    if not math.isfinite(result):
        raise RecoveryContractError(f"{name} must be finite numeric")
    return result


@dataclass(frozen=True)
class AttemptIdentity:
    """The exact resident-session/attempt identity carried through recovery."""

    session_epoch: int
    resident_session_id: str
    attempt_id: str
    attempt_sequence: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.session_epoch, bool)
            or not isinstance(self.session_epoch, int)
            or self.session_epoch <= 0
        ):
            raise RecoveryContractError("session_epoch must be a positive integer")
        if not isinstance(self.resident_session_id, str) or not self.resident_session_id.strip():
            raise RecoveryContractError("resident_session_id must be nonempty")
        if not isinstance(self.attempt_id, str) or not self.attempt_id.strip():
            raise RecoveryContractError("attempt_id must be nonempty")
        if (
            isinstance(self.attempt_sequence, bool)
            or not isinstance(self.attempt_sequence, int)
            or self.attempt_sequence <= 0
        ):
            raise RecoveryContractError("attempt_sequence must be a positive integer")


@dataclass(frozen=True)
class FaultRecord:
    """The first fault observed for one attempt; later faults cannot replace it."""

    kind: FaultKind
    identity: AttemptIdentity
    observed_at_s: float
    reason: str
    force_norm_n: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FaultKind):
            raise RecoveryContractError("fault kind is not typed")
        _finite(self.observed_at_s, "fault observed_at_s")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise RecoveryContractError("fault reason must be nonempty")
        if self.force_norm_n is not None:
            _finite(self.force_norm_n, "fault force_norm_n")


@dataclass(frozen=True)
class SealedAttempt:
    """An immutable boundary between a failed attempt and a later Home state."""

    identity: AttemptIdentity
    samples: tuple[Any, ...]
    first_fault: FaultRecord
    sealed_at_s: float

    def __post_init__(self) -> None:
        if self.first_fault.identity != self.identity:
            raise RecoveryContractError("sealed fault identity differs from attempt")
        _finite(self.sealed_at_s, "sealed_at_s")
        object.__setattr__(self, "samples", tuple(deepcopy(self.samples)))


@dataclass(frozen=True)
class RecoverySnapshot:
    """Deep-copied lifecycle state used for local atomic rollback."""

    session_epoch: int
    resident_session_id: str
    state: RecoveryState
    prepared_identity: AttemptIdentity | None
    active_identity: AttemptIdentity | None
    active_samples: tuple[Any, ...]
    sealed_attempts: tuple[SealedAttempt, ...]
    first_fault: FaultRecord | None
    blocked_reason: str | None
    unlock_decision: bool | None
    last_attempt_sequence: int
    transitions: tuple[RecoveryState, ...]


class WriterSessionRegistry:
    """Weak process-local registry for one writer per resident session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owners: weakref.WeakValueDictionary[tuple[int, str], RecoveryLifecycle] = (
            weakref.WeakValueDictionary()
        )

    def claim(self, key: tuple[int, str], owner: RecoveryLifecycle) -> None:
        with self._lock:
            current = self._owners.get(key)
            if current is not None and current is not owner:
                raise RecoveryOwnershipError(
                    "single writer/resident-session ownership is already held"
                )
            self._owners[key] = owner

    def release(self, key: tuple[int, str], owner: RecoveryLifecycle) -> None:
        with self._lock:
            if self._owners.get(key) is owner:
                del self._owners[key]


_DEFAULT_REGISTRY = WriterSessionRegistry()
HomeTransition = Callable[[SealedAttempt], bool]


class RecoveryLifecycle:
    """Fault-seal, quiescence, unlock-decision, and Home state seam.

    ``home_transition`` is deliberately an observation/test seam.  A callback
    must return ``True`` only when its caller has observed the existing Home
    lifecycle complete.  Returning ``False`` or raising leaves the lifecycle
    ``BLOCKED``; no callback is an unlock or motion command.
    """

    def __init__(
        self,
        *,
        session_epoch: int,
        resident_session_id: str,
        home_transition: HomeTransition | None = None,
        ownership_registry: WriterSessionRegistry | None = None,
        writer: Any | None = None,
    ) -> None:
        if (
            isinstance(session_epoch, bool)
            or not isinstance(session_epoch, int)
            or session_epoch <= 0
        ):
            raise RecoveryContractError("session_epoch must be a positive integer")
        if not isinstance(resident_session_id, str) or not resident_session_id.strip():
            raise RecoveryContractError("resident_session_id must be nonempty")
        if home_transition is not None and not callable(home_transition):
            raise RecoveryContractError("home_transition must be callable")
        self.session_epoch = session_epoch
        self.resident_session_id = resident_session_id
        self.writer = writer
        self._registry = ownership_registry or _DEFAULT_REGISTRY
        self._owner_key = (session_epoch, resident_session_id)
        self._home_transition = home_transition
        self._lock = threading.RLock()
        self._closed = False
        self._owner_claimed = False
        self._state = RecoveryState.HOME
        self._prepared_identity: AttemptIdentity | None = None
        self._active_identity: AttemptIdentity | None = None
        self._active_samples: list[Any] = []
        self._sealed_attempts: list[SealedAttempt] = []
        self._first_fault: FaultRecord | None = None
        self._blocked_reason: str | None = None
        self._unlock_decision: bool | None = None
        self._last_attempt_sequence = 0
        self._transitions = [RecoveryState.HOME]

    @classmethod
    def from_writer(
        cls,
        writer: Any,
        *,
        home_transition: HomeTransition | None = None,
        ownership_registry: WriterSessionRegistry | None = None,
    ) -> "RecoveryLifecycle":
        """Build the seam from the existing mature writer identity."""

        prerequisites = getattr(writer, "prerequisites", None)
        if prerequisites is None:
            raise RecoveryContractError("mature writer prerequisites are required")
        return cls(
            session_epoch=int(prerequisites.session_epoch),
            resident_session_id=str(prerequisites.resident_session_id),
            home_transition=home_transition,
            ownership_registry=ownership_registry,
            writer=writer,
        )

    @property
    def state(self) -> RecoveryState:
        return self._state

    @property
    def owner_held(self) -> bool:
        return self._owner_claimed

    @property
    def prepared_identity(self) -> AttemptIdentity | None:
        return self._prepared_identity

    @property
    def active_identity(self) -> AttemptIdentity | None:
        return self._active_identity

    @property
    def active_samples(self) -> tuple[Any, ...]:
        with self._lock:
            return tuple(deepcopy(self._active_samples))

    @property
    def sealed_attempt(self) -> SealedAttempt | None:
        return self._sealed_attempts[-1] if self._sealed_attempts else None

    @property
    def sealed_attempts(self) -> tuple[SealedAttempt, ...]:
        with self._lock:
            return tuple(deepcopy(self._sealed_attempts))

    @property
    def first_fault(self) -> FaultRecord | None:
        with self._lock:
            if self._first_fault is not None:
                return deepcopy(self._first_fault)
            return None if self.sealed_attempt is None else deepcopy(self.sealed_attempt.first_fault)

    @property
    def blocked_reason(self) -> str | None:
        return self._blocked_reason

    @property
    def unlock_decision(self) -> bool | None:
        return self._unlock_decision

    @property
    def transitions(self) -> tuple[RecoveryState, ...]:
        return tuple(self._transitions)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RecoveryContractError("recovery lifecycle is closed")
        if self._state is RecoveryState.BLOCKED:
            raise RecoveryBlockedError(
                f"recovery remains BLOCKED: {self._blocked_reason or 'unspecified failure'}"
            )

    def _require_state(self, *states: RecoveryState) -> None:
        self._ensure_open()
        if self._state not in states:
            expected = ", ".join(state.value for state in states)
            raise RecoveryStateError(
                f"recovery operation requires {expected}, observed {self._state.value}"
            )

    def _validate_identity(self, identity: AttemptIdentity) -> None:
        if not isinstance(identity, AttemptIdentity):
            raise RecoveryContractError("attempt identity is not typed")
        if (
            identity.session_epoch != self.session_epoch
            or identity.resident_session_id != self.resident_session_id
        ):
            raise RecoveryContractError("attempt identity differs from resident session")

    def _claim_owner(self) -> None:
        if not self._owner_claimed:
            self._registry.claim(self._owner_key, self)
            self._owner_claimed = True

    def _transition(self, state: RecoveryState) -> None:
        self._state = state
        self._transitions.append(state)

    def prepare_attempt(self, identity: AttemptIdentity) -> AttemptIdentity:
        """Reserve one attempt; a second preparation/spawn is rejected."""

        with self._lock:
            self._require_state(RecoveryState.HOME)
            self._validate_identity(identity)
            if self._prepared_identity is not None or self._active_identity is not None:
                raise RecoveryOwnershipError("attempt preparation already exists")
            if identity.attempt_sequence <= self._last_attempt_sequence:
                raise RecoveryContractError("attempt sequence is not strictly newer")
            self._claim_owner()
            self._prepared_identity = identity
            self._last_attempt_sequence = identity.attempt_sequence
            return identity

    def start_attempt(self, identity: AttemptIdentity) -> None:
        with self._lock:
            self._require_state(RecoveryState.HOME)
            if self._prepared_identity != identity:
                raise RecoveryContractError("attempt start identity differs from prepared identity")
            self._active_identity = identity
            self._prepared_identity = None
            self._active_samples = []
            self._first_fault = None
            self._unlock_decision = None
            self._transition(RecoveryState.ATTEMPT_ACTIVE)

    def record_sample(self, sample: Any) -> None:
        """Append only to the current attempt; never to a prior sealed attempt."""

        with self._lock:
            self._require_state(RecoveryState.ATTEMPT_ACTIVE)
            copied = deepcopy(sample)
            self._active_samples.append(copied)

    def complete_attempt(self) -> None:
        """Close a normal attempt without merging it with prior evidence."""

        with self._lock:
            self._require_state(RecoveryState.ATTEMPT_ACTIVE)
            self._active_identity = None
            self._active_samples = []
            self._first_fault = None
            self._transition(RecoveryState.HOME)

    def _seal(self, fault: FaultRecord, sealed_at_s: float) -> SealedAttempt:
        if self._active_identity is None or self._state is not RecoveryState.ATTEMPT_ACTIVE:
            raise RecoveryStateError("cannot seal an inactive attempt")
        sealed = SealedAttempt(
            identity=self._active_identity,
            samples=tuple(self._active_samples),
            first_fault=fault,
            sealed_at_s=sealed_at_s,
        )
        self._sealed_attempts.append(sealed)
        self._active_identity = None
        self._prepared_identity = None
        self._active_samples = []
        self._first_fault = None
        self._transition(RecoveryState.SEALED)
        return sealed

    def _run_home_transition(self, sealed: SealedAttempt) -> bool:
        if self._home_transition is None:
            return False
        try:
            completed = self._home_transition(deepcopy(sealed))
        except BaseException as exc:
            self._block(f"Home transition raised {type(exc).__name__}: {exc}")
            return False
        if completed is not True:
            self._block("Home transition did not complete")
            return False
        self._complete_home(sealed.identity, success=True)
        return True

    def _complete_home(
        self,
        identity: AttemptIdentity,
        *,
        success: bool,
        reason: str = "",
    ) -> bool:
        if self._state is not RecoveryState.HOME_PENDING:
            raise RecoveryStateError("Home completion is not pending")
        sealed = self.sealed_attempt
        if sealed is None or sealed.identity != identity:
            raise RecoveryContractError("Home completion identity differs from sealed attempt")
        if not isinstance(success, bool):
            raise RecoveryContractError("Home completion success must be bool")
        if not success:
            self._block(reason or "Home recovery failed")
            return False
        self._transition(RecoveryState.HOME)
        return True

    def complete_home(
        self,
        identity: AttemptIdentity | None = None,
        *,
        success: bool = True,
        reason: str = "",
    ) -> bool:
        """Commit an observed Home result; failure is terminal BLOCKED."""

        with self._lock:
            self._require_state(RecoveryState.HOME_PENDING)
            sealed = self.sealed_attempt
            expected = None if sealed is None else sealed.identity
            if identity is not None and identity != expected:
                raise RecoveryContractError("Home completion identity differs from sealed attempt")
            if expected is None:
                raise RecoveryContractError("Home completion has no sealed attempt")
            return self._complete_home(expected, success=success, reason=reason)

    def guard_trip(
        self,
        force_norm_n: float,
        *,
        observed_at_s: float,
        reason: str = "60 N guard",
    ) -> SealedAttempt:
        """Seal at or above 60 N and enter the automatic Home seam."""

        with self._lock:
            self._require_state(RecoveryState.ATTEMPT_ACTIVE)
            force = _finite(force_norm_n, "force_norm_n")
            stamp = _finite(observed_at_s, "guard observed_at_s")
            if force < GUARD_FORCE_THRESHOLD_N:
                raise RecoveryContractError("60 N guard threshold was not reached")
            if not isinstance(reason, str) or not reason.strip():
                raise RecoveryContractError("guard reason must be nonempty")
            assert self._active_identity is not None
            fault = FaultRecord(
                kind=FaultKind.GUARD_60N,
                identity=self._active_identity,
                observed_at_s=stamp,
                reason=reason,
                force_norm_n=force,
            )
            self._first_fault = fault
            sealed = self._seal(fault, stamp)
            self._transition(RecoveryState.HOME_PENDING)
            self._run_home_transition(sealed)
            return sealed

    def protective_stop(
        self,
        *,
        observed_at_s: float,
        reason: str = "protective stop",
    ) -> SealedAttempt:
        """Seal first, then require an independent quiescence observation."""

        with self._lock:
            self._require_state(RecoveryState.ATTEMPT_ACTIVE)
            stamp = _finite(observed_at_s, "protective stop observed_at_s")
            if not isinstance(reason, str) or not reason.strip():
                raise RecoveryContractError("protective stop reason must be nonempty")
            assert self._active_identity is not None
            fault = FaultRecord(
                kind=FaultKind.PROTECTIVE_STOP,
                identity=self._active_identity,
                observed_at_s=stamp,
                reason=reason,
            )
            self._first_fault = fault
            sealed = self._seal(fault, stamp)
            self._transition(RecoveryState.QUIESCENCE_REQUIRED)
            return sealed

    def observe_quiescence(
        self,
        *,
        safety_mode: str,
        stationary: bool,
        fresh: bool,
        session_epoch: int,
        resident_session_id: str,
        attempt_id: str,
        observed_at_s: float,
    ) -> bool:
        """Return true only for a fresh, stationary, identity-matched stop."""

        with self._lock:
            self._require_state(RecoveryState.QUIESCENCE_REQUIRED)
            stamp = _finite(observed_at_s, "quiescence observed_at_s")
            if not isinstance(safety_mode, str):
                raise RecoveryContractError("safety_mode must be string typed")
            if not isinstance(stationary, bool) or not isinstance(fresh, bool):
                raise RecoveryContractError("quiescence booleans are not typed")
            sealed = self.sealed_attempt
            assert sealed is not None
            identity_matches = (
                session_epoch == self.session_epoch
                and resident_session_id == self.resident_session_id
                and attempt_id == sealed.identity.attempt_id
            )
            if not (
                safety_mode == "PROTECTIVE_STOP"
                and stationary
                and fresh
                and identity_matches
                and stamp >= sealed.first_fault.observed_at_s
            ):
                return False
            self._transition(RecoveryState.UNLOCK_DECISION)
            return True

    def decide_unlock(
        self,
        allowed: bool,
        *,
        identity: AttemptIdentity | None = None,
    ) -> bool:
        """Record the unlock decision; never call an unlock or motion API."""

        with self._lock:
            self._require_state(RecoveryState.UNLOCK_DECISION)
            if not isinstance(allowed, bool):
                raise RecoveryContractError("unlock decision must be bool")
            sealed = self.sealed_attempt
            if sealed is None or (identity is not None and identity != sealed.identity):
                raise RecoveryContractError("unlock decision identity differs from sealed attempt")
            self._unlock_decision = allowed
            if not allowed:
                self._block("unlock decision denied")
                return False
            self._transition(RecoveryState.HOME_PENDING)
            return self._run_home_transition(sealed)

    def fail_recovery(self, reason: str) -> None:
        """Make a recovery failure terminal while retaining sealed evidence."""

        with self._lock:
            self._ensure_open()
            if not isinstance(reason, str) or not reason.strip():
                raise RecoveryContractError("recovery failure reason must be nonempty")
            if self._state is RecoveryState.ATTEMPT_ACTIVE:
                assert self._active_identity is not None
                stamp = 0.0
                self._first_fault = self._first_fault or FaultRecord(
                    kind=FaultKind.RECOVERY_FAILURE,
                    identity=self._active_identity,
                    observed_at_s=stamp,
                    reason=reason,
                )
                self._seal(self._first_fault, stamp)
            self._block(reason)

    def _block(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            reason = "unspecified recovery failure"
        self._blocked_reason = reason
        self._prepared_identity = None
        self._active_identity = None
        self._active_samples = []
        if self._state is not RecoveryState.BLOCKED:
            self._transition(RecoveryState.BLOCKED)

    def snapshot(self) -> RecoverySnapshot:
        with self._lock:
            return RecoverySnapshot(
                session_epoch=self.session_epoch,
                resident_session_id=self.resident_session_id,
                state=self._state,
                prepared_identity=self._prepared_identity,
                active_identity=self._active_identity,
                active_samples=tuple(deepcopy(self._active_samples)),
                sealed_attempts=tuple(deepcopy(self._sealed_attempts)),
                first_fault=deepcopy(self._first_fault),
                blocked_reason=self._blocked_reason,
                unlock_decision=self._unlock_decision,
                last_attempt_sequence=self._last_attempt_sequence,
                transitions=tuple(self._transitions),
            )

    def _validate_snapshot(self, snapshot: RecoverySnapshot) -> None:
        if not isinstance(snapshot, RecoverySnapshot):
            raise RecoveryContractError("recovery snapshot is not typed")
        if (
            snapshot.session_epoch != self.session_epoch
            or snapshot.resident_session_id != self.resident_session_id
        ):
            raise RecoveryContractError("recovery snapshot identity differs")
        if snapshot.last_attempt_sequence < self._last_attempt_sequence:
            raise RecoveryContractError("recovery snapshot rewinds attempt sequence")
        if len(snapshot.sealed_attempts) < len(self._sealed_attempts):
            raise RecoveryContractError("recovery snapshot removes sealed attempts")
        if self._state is RecoveryState.BLOCKED and snapshot.state is not RecoveryState.BLOCKED:
            raise RecoveryContractError("BLOCKED recovery cannot be reopened")
        if snapshot.state is RecoveryState.ATTEMPT_ACTIVE and snapshot.active_identity is None:
            raise RecoveryContractError("active recovery snapshot has no attempt identity")
        if snapshot.state is not RecoveryState.ATTEMPT_ACTIVE and snapshot.active_identity is not None:
            raise RecoveryContractError("inactive recovery snapshot retains active identity")
        if snapshot.first_fault is not None and snapshot.active_identity != snapshot.first_fault.identity:
            raise RecoveryContractError("snapshot first fault identity differs")

    def restore(self, snapshot: RecoverySnapshot) -> None:
        """Restore a validated checkpoint without partially applying bad state."""

        with self._lock:
            self._ensure_open()
            self._validate_snapshot(snapshot)
            self._state = snapshot.state
            self._prepared_identity = snapshot.prepared_identity
            self._active_identity = snapshot.active_identity
            self._active_samples = list(deepcopy(snapshot.active_samples))
            self._sealed_attempts = list(deepcopy(snapshot.sealed_attempts))
            self._first_fault = deepcopy(snapshot.first_fault)
            self._blocked_reason = snapshot.blocked_reason
            self._unlock_decision = snapshot.unlock_decision
            self._last_attempt_sequence = snapshot.last_attempt_sequence
            self._transitions = list(snapshot.transitions)

    def atomic(self, operation: Callable[["RecoveryLifecycle"], Any]) -> Any:
        """Run a local speculative update and roll it back on any exception."""

        if not callable(operation):
            raise RecoveryContractError("atomic operation must be callable")
        with self._lock:
            self._ensure_open()
            checkpoint = self.snapshot()
            try:
                return operation(self)
            except BaseException:
                self.restore(checkpoint)
                raise

    def close(self) -> None:
        """Release the resident-session lease only after a terminal safe state."""

        with self._lock:
            if self._closed:
                return
            if self._prepared_identity is not None or self._active_identity is not None:
                raise RecoveryOwnershipError("cannot release an active recovery attempt")
            if self._state in {
                RecoveryState.SEALED,
                RecoveryState.QUIESCENCE_REQUIRED,
                RecoveryState.UNLOCK_DECISION,
                RecoveryState.HOME_PENDING,
            }:
                raise RecoveryOwnershipError("cannot release before recovery reaches Home or BLOCKED")
            if self._owner_claimed:
                self._registry.release(self._owner_key, self)
                self._owner_claimed = False
            self._closed = True

    def __enter__(self) -> "RecoveryLifecycle":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "AttemptIdentity",
    "FaultKind",
    "FaultRecord",
    "GUARD_FORCE_THRESHOLD_N",
    "RecoveryBlockedError",
    "RecoveryContractError",
    "RecoveryLifecycle",
    "RecoveryOwnershipError",
    "RecoverySnapshot",
    "RecoveryState",
    "RecoveryStateError",
    "SealedAttempt",
    "WriterSessionRegistry",
]
