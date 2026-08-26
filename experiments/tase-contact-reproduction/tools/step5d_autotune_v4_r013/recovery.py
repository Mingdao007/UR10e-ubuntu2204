"""Fail-closed recovery decision seam for the V4 stage owner."""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

from .v4_two_stage_campaign import (
    V4FailureClass,
    V4FailureDisposition,
    V4FailureEvidenceV1,
    V4HomeStatus,
    V4RecoveryReceiptV1,
    V4RecoveryStatus,
)


def safe_home_then_resume_for_recoverable_failures_only(
    *,
    failure: V4FailureEvidenceV1,
    current_attempt_ordinal: int,
    attempt_count: int,
    prior_epoch: int,
    fresh_epoch: int | None,
    fresh_readiness: bool,
    dispatch_in_flight: bool,
    dispatch_id: str | None = None,
    dispatched_ids: Iterable[str] = (),
    home_action: Callable[[], Any] | None = None,
) -> V4RecoveryReceiptV1:
    """Apply the sole recovery seam: safe Home first, then fresh resume only.

    ``home_action`` is an owner-provided capability.  The runner never calls
    it unless the owner evidence explicitly sets ``home_permitted``.  This
    helper only returns ``auto_dispatch_permitted`` after a new resident epoch
    and readiness are supplied; callers must use the stage campaign's typed
    ``resume_after_recovery`` boundary to create the next append-only dispatch.
    """

    if current_attempt_ordinal <= 0 or attempt_count < current_attempt_ordinal:
        raise ValueError("recovery attempt ordinal/count is invalid")
    if prior_epoch <= 0:
        raise ValueError("recovery prior epoch is invalid")
    if fresh_epoch is not None and fresh_epoch <= 0:
        raise ValueError("recovery fresh epoch is invalid")
    if type(dispatch_in_flight) is not bool:
        raise ValueError("recovery in-flight flag is invalid")
    if type(fresh_readiness) is not bool:
        raise ValueError("recovery readiness flag is invalid")

    seen_dispatch_ids = {str(value) for value in dispatched_ids}
    home_attempted = False
    home_receipt: Mapping[str, Any] | None = None
    if failure.home_verified:
        home_status = V4HomeStatus.VERIFIED
    elif not failure.home_permitted:
        home_status = (
            V4HomeStatus.BLOCKED
            if failure.protective_stop or failure.emergency_stop
            else V4HomeStatus.NOT_PERMITTED
        )
    elif home_action is None:
        home_status = V4HomeStatus.FAILED
    else:
        home_attempted = True
        try:
            raw_home = home_action()
            if isinstance(raw_home, Mapping):
                home_receipt = dict(raw_home)
                verified = raw_home.get(
                    "home_verified",
                    raw_home.get("safe_return", raw_home.get("verified", False)),
                )
                blocked = raw_home.get("home_blocked") is True
            else:
                verified = raw_home is True
                blocked = False
            home_status = (
                V4HomeStatus.VERIFIED
                if verified is True
                else V4HomeStatus.BLOCKED
                if blocked
                else V4HomeStatus.FAILED
            )
        except Exception as exc:
            home_status = V4HomeStatus.FAILED
            home_receipt = {
                "schema": "step5d.autotune-v4/r013-recovery-home-error-v1",
                "home_verified": False,
                "error": f"{type(exc).__name__}: {exc}",
            }

    recoverable_class = failure.failure_class in {
        V4FailureClass.TIMING_BOUNDARY,
        V4FailureClass.TRANSPORT,
        V4FailureClass.HOST_BOUNDARY,
    }
    hard_fault = bool(
        not recoverable_class
        or failure.safety_fault
        or failure.force_fault
        or failure.sensor_fault
        or failure.joint_fault
        or failure.protective_stop
        or failure.emergency_stop
        or not failure.source_identity_matches
        or home_status is not V4HomeStatus.VERIFIED
    )
    next_attempt_ordinal = attempt_count + 1
    if hard_fault:
        status = (
            V4RecoveryStatus.HOME_BLOCKED
            if home_status is V4HomeStatus.BLOCKED
            else V4RecoveryStatus.HOME_NOT_VERIFIED
            if home_status is not V4HomeStatus.VERIFIED
            else V4RecoveryStatus.TERMINAL
        )
        return V4RecoveryReceiptV1(
            disposition=V4FailureDisposition.HARD_TERMINAL,
            status=status,
            failure_class=failure.failure_class,
            reason=failure.reason,
            current_attempt_ordinal=current_attempt_ordinal,
            attempt_count=attempt_count,
            home_status=home_status,
            home_attempted=home_attempted,
            home_receipt=home_receipt,
            prior_epoch=prior_epoch,
            fresh_epoch=fresh_epoch,
            fresh_readiness=fresh_readiness,
            next_attempt_ordinal=None,
            auto_dispatch_permitted=False,
            dispatch_id=dispatch_id,
        )

    if dispatch_in_flight:
        status = V4RecoveryStatus.IN_FLIGHT
    elif dispatch_id is not None and dispatch_id in seen_dispatch_ids:
        status = V4RecoveryStatus.DUPLICATE_DISPATCH
    elif fresh_epoch is None or fresh_epoch <= prior_epoch:
        status = V4RecoveryStatus.STALE_EPOCH
    elif not fresh_readiness:
        status = V4RecoveryStatus.READINESS_MISSING
    else:
        status = V4RecoveryStatus.RESUME_READY
    return V4RecoveryReceiptV1(
        disposition=V4FailureDisposition.RECOVERABLE_RESUME,
        status=status,
        failure_class=failure.failure_class,
        reason=failure.reason,
        current_attempt_ordinal=current_attempt_ordinal,
        attempt_count=attempt_count,
        home_status=home_status,
        home_attempted=home_attempted,
        home_receipt=home_receipt,
        prior_epoch=prior_epoch,
        fresh_epoch=fresh_epoch,
        fresh_readiness=fresh_readiness,
        next_attempt_ordinal=next_attempt_ordinal,
        auto_dispatch_permitted=status is V4RecoveryStatus.RESUME_READY,
        dispatch_id=dispatch_id,
    )


__all__ = ["safe_home_then_resume_for_recoverable_failures_only"]
