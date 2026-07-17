"""Pure crash-recoverable physical lifecycle reducer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import Any, Mapping


class LifecycleError(RuntimeError):
    """Raised when an event would violate the physical transaction order."""


class LifecycleState(str, Enum):
    PENDING = "pending"
    ARM_PERSISTED = "arm_persisted"
    COMMAND_PUBLISHED = "command_published"
    TP_CONSUMED = "tp_consumed"
    RUNNING = "running"
    HOME_VERIFIED = "home_verified"
    RAW_SEALED = "raw_sealed"
    ACK_PERSISTED = "ack_persisted"
    ACK_PUBLISHED = "ack_published"
    READY_HOME_OBSERVED = "ready_home_observed"
    PHYSICAL_CLOSED = "physical_closed"
    COMPLETE = "complete"
    ANALYSIS_FAILED = "analysis_failed"
    UNCERTAIN_ATTEMPT = "uncertain_attempt"
    FAULT = "fault"


class LifecycleEvent(str, Enum):
    PERSIST_ARM = "persist_arm"
    PUBLISH_COMMAND = "publish_command"
    OBSERVE_TP_CONSUMED = "observe_tp_consumed"
    OBSERVE_RUN = "observe_run"
    VERIFY_HOME = "verify_home"
    SEAL_RAW = "seal_raw"
    PERSIST_ACK = "persist_ack"
    PUBLISH_ACK = "publish_ack"
    OBSERVE_READY_HOME = "observe_ready_home"
    CLOSE_PHYSICAL = "close_physical"
    ANALYSIS_SUCCEEDED = "analysis_succeeded"
    ANALYSIS_FAILED = "analysis_failed"
    MARK_UNCERTAIN_ATTEMPT = "mark_uncertain_attempt"
    SAFETY_HALT = "safety_halt"


@dataclass(frozen=True)
class LifecycleSnapshot:
    state: LifecycleState = LifecycleState.PENDING
    attempted: bool = False
    uncertain_attempt: bool = False
    safe_home_verified: bool = False
    raw_sealed: bool = False
    ack_published: bool = False
    physical_closed: bool = False
    analysis_complete: bool = False
    eligible_objective: bool | None = None
    primary_blocker: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state"] = self.state.value
        return payload

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "LifecycleSnapshot":
        values = dict(payload)
        values["state"] = LifecycleState(values.get("state", "pending"))
        return cls(**values)


_ORDERED = {
    LifecycleEvent.PERSIST_ARM: (LifecycleState.PENDING, LifecycleState.ARM_PERSISTED),
    LifecycleEvent.PUBLISH_COMMAND: (
        LifecycleState.ARM_PERSISTED,
        LifecycleState.COMMAND_PUBLISHED,
    ),
    LifecycleEvent.OBSERVE_TP_CONSUMED: (
        LifecycleState.COMMAND_PUBLISHED,
        LifecycleState.TP_CONSUMED,
    ),
    LifecycleEvent.OBSERVE_RUN: (LifecycleState.TP_CONSUMED, LifecycleState.RUNNING),
    LifecycleEvent.VERIFY_HOME: (LifecycleState.RUNNING, LifecycleState.HOME_VERIFIED),
    LifecycleEvent.SEAL_RAW: (LifecycleState.HOME_VERIFIED, LifecycleState.RAW_SEALED),
    LifecycleEvent.PERSIST_ACK: (LifecycleState.RAW_SEALED, LifecycleState.ACK_PERSISTED),
    LifecycleEvent.PUBLISH_ACK: (
        LifecycleState.ACK_PERSISTED,
        LifecycleState.ACK_PUBLISHED,
    ),
    LifecycleEvent.OBSERVE_READY_HOME: (
        LifecycleState.ACK_PUBLISHED,
        LifecycleState.READY_HOME_OBSERVED,
    ),
    LifecycleEvent.CLOSE_PHYSICAL: (
        LifecycleState.READY_HOME_OBSERVED,
        LifecycleState.PHYSICAL_CLOSED,
    ),
}


def reduce_lifecycle(
    snapshot: LifecycleSnapshot,
    event: LifecycleEvent,
    payload: Mapping[str, Any] | None = None,
) -> LifecycleSnapshot:
    """Apply one event without I/O; invalid or reordered events fail closed."""

    data = dict(payload or {})
    ordered = _ORDERED.get(event)
    if ordered is not None:
        expected, target = ordered
        if snapshot.state is not expected:
            raise LifecycleError(
                f"{event.value} requires {expected.value}, got {snapshot.state.value}"
            )
        changes: dict[str, Any] = {"state": target, "primary_blocker": None}
        if event is LifecycleEvent.OBSERVE_TP_CONSUMED:
            changes["attempted"] = True
        elif event is LifecycleEvent.VERIFY_HOME:
            if data.get("safe_home_verified") is not True:
                raise LifecycleError("home verification requires positive measured evidence")
            changes["safe_home_verified"] = True
        elif event is LifecycleEvent.SEAL_RAW:
            if not snapshot.safe_home_verified or not data.get("artifact_sha256"):
                raise LifecycleError("raw seal requires safe Home and an artifact digest")
            changes["raw_sealed"] = True
        elif event is LifecycleEvent.PUBLISH_ACK:
            if not snapshot.safe_home_verified or not snapshot.raw_sealed:
                raise LifecycleError("ACK cannot precede safe Home and raw seal")
            changes["ack_published"] = True
        elif event is LifecycleEvent.CLOSE_PHYSICAL:
            if not snapshot.ack_published:
                raise LifecycleError("physical closure requires published ACK")
            changes["physical_closed"] = True
        return replace(snapshot, **changes)

    if event is LifecycleEvent.MARK_UNCERTAIN_ATTEMPT:
        if snapshot.state not in {
            LifecycleState.PENDING,
            LifecycleState.ARM_PERSISTED,
            LifecycleState.COMMAND_PUBLISHED,
            LifecycleState.TP_CONSUMED,
            LifecycleState.RUNNING,
            LifecycleState.HOME_VERIFIED,
            LifecycleState.RAW_SEALED,
            LifecycleState.ACK_PERSISTED,
            LifecycleState.ACK_PUBLISHED,
            LifecycleState.READY_HOME_OBSERVED,
        }:
            raise LifecycleError("uncertain attempt requires an open physical transaction")
        return replace(
            snapshot,
            state=LifecycleState.UNCERTAIN_ATTEMPT,
            attempted=True,
            uncertain_attempt=True,
            primary_blocker=str(data.get("reason") or "ambiguous_tp_consumption"),
        )

    if event is LifecycleEvent.SAFETY_HALT:
        if snapshot.state in {LifecycleState.COMPLETE, LifecycleState.PHYSICAL_CLOSED}:
            raise LifecycleError("a closed physical trial cannot be retroactively faulted")
        return replace(
            snapshot,
            state=LifecycleState.FAULT,
            attempted=snapshot.attempted
            or snapshot.state
            not in {LifecycleState.PENDING, LifecycleState.ARM_PERSISTED},
            primary_blocker=str(data.get("reason") or "safety_halt"),
        )

    if event in {LifecycleEvent.ANALYSIS_SUCCEEDED, LifecycleEvent.ANALYSIS_FAILED}:
        if snapshot.state is not LifecycleState.PHYSICAL_CLOSED:
            raise LifecycleError("analysis is allowed only after physical closure")
        if event is LifecycleEvent.ANALYSIS_FAILED:
            return replace(
                snapshot,
                state=LifecycleState.ANALYSIS_FAILED,
                primary_blocker=str(data.get("reason") or "analysis_failed"),
            )
        eligible = data.get("eligible_objective")
        if not isinstance(eligible, bool):
            raise LifecycleError("successful analysis must declare objective eligibility")
        return replace(
            snapshot,
            state=LifecycleState.COMPLETE,
            analysis_complete=True,
            eligible_objective=eligible,
            primary_blocker=None,
        )

    raise LifecycleError(f"unsupported lifecycle event: {event.value}")
