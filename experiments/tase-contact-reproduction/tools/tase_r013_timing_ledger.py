"""Offline lifecycle timing and recovery ledger for the 60 s TASE protocol.

The ledger is intentionally a receipt reducer, not a second runtime owner.  A
live writer may attach its events to the object, while the supervisor remains
the only component allowed to stop, recover, or Home the robot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import statistics
from typing import Any, Iterable, Mapping


STAGES = (
    "HOME_CHECK",
    "CONTACT_SEARCH",
    "CONTACT_LATCH",
    "QUALIFICATION",
    "READINESS_HOLD",
    "ENTRY",
    "PATH",
    "STOP",
    "UNLOAD_RELIEF",
    "CLEARANCE",
    "HOME",
)
_STAGE_INDEX = {name: index for index, name in enumerate(STAGES)}


class TimingLedgerError(ValueError):
    """A lifecycle receipt cannot be interpreted without inventing timing."""


def _finite(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TimingLedgerError(f"{label} is not numeric") from exc
    if not math.isfinite(result):
        raise TimingLedgerError(f"{label} is not finite")
    return result


@dataclass
class TaseR013TimingLedger:
    """One attempt's stage transitions, with no implicit missing timestamps."""

    attempt_id: str
    protocol_id: str = "figure8_window60_r013_compat_v1"
    events: list[dict[str, Any]] = field(default_factory=list)
    failure_stage: str | None = None
    failure_condition: str | None = None
    source_success: bool | None = None
    recovery: Mapping[str, Any] | None = None

    def mark(self, stage: str, timestamp_s: float, *, event: str = "start") -> None:
        stage = str(stage)
        if stage not in _STAGE_INDEX:
            raise TimingLedgerError(f"unknown lifecycle stage {stage!r}")
        timestamp = _finite(timestamp_s, f"{stage} timestamp")
        if self.events:
            previous = self.events[-1]
            previous_index = _STAGE_INDEX[previous["stage"]]
            if _STAGE_INDEX[stage] < previous_index:
                raise TimingLedgerError("lifecycle stage regressed")
            if timestamp < float(previous["timestamp_s"]):
                raise TimingLedgerError("lifecycle timestamp regressed")
        self.events.append({"stage": stage, "event": str(event), "timestamp_s": timestamp})

    @classmethod
    def from_events(
        cls,
        attempt_id: str,
        events: Iterable[Mapping[str, Any]],
        *,
        protocol_id: str = "figure8_window60_r013_compat_v1",
        failure_stage: str | None = None,
        failure_condition: str | None = None,
        source_success: bool | None = None,
    ) -> "TaseR013TimingLedger":
        ledger = cls(
            attempt_id=str(attempt_id),
            protocol_id=str(protocol_id),
            failure_stage=failure_stage,
            failure_condition=failure_condition,
            source_success=source_success,
        )
        for event in events:
            if not isinstance(event, Mapping):
                raise TimingLedgerError("lifecycle event is not a mapping")
            ledger.mark(
                str(event.get("stage", "")),
                event.get("timestamp_s"),
                event=str(event.get("event", "start")),
            )
        return ledger

    def attach_recovery(self, recovery: Mapping[str, Any] | None) -> None:
        if recovery is not None and not isinstance(recovery, Mapping):
            raise TimingLedgerError("recovery receipt is not a mapping")
        self.recovery = recovery

    def _first(self, stage: str, event: str | None = None) -> float | None:
        for row in self.events:
            if row["stage"] == stage and (event is None or row["event"] == event):
                return float(row["timestamp_s"])
        return None

    def _last(self, stage: str, event: str | None = None) -> float | None:
        for row in reversed(self.events):
            if row["stage"] == stage and (event is None or row["event"] == event):
                return float(row["timestamp_s"])
        return None

    def durations(self) -> dict[str, float | None]:
        result: dict[str, float | None] = {}
        def pick(*values: float | None) -> float | None:
            for value in values:
                if value is not None:
                    return value
            return None
        for left, right in zip(STAGES, STAGES[1:]):
            start = pick(self._first(left, "start"), self._first(left))
            end = pick(self._first(right, "start"), self._first(right))
            result[f"{left.lower()}_to_{right.lower()}_s"] = (
                None if start is None or end is None else end - start
            )
        path_start = pick(self._first("PATH", "start"), self._first("PATH"))
        path_end = pick(self._first("PATH", "end"), self._last("PATH", "stop"))
        result["formal_path_s"] = None if path_start is None or path_end is None else path_end - path_start
        home_start = pick(self._first("HOME", "start"), self._first("HOME"))
        home_end = pick(self._last("HOME", "verified"), self._last("HOME", "end"))
        result["home_duration_s"] = None if home_start is None or home_end is None else home_end - home_start
        first_home = pick(self._first("HOME_CHECK", "start"), self._first("HOME_CHECK"))
        result["home_to_home_s"] = None if first_home is None or home_end is None else home_end - first_home
        return result

    def recovery_outcome(self) -> dict[str, Any]:
        recovery = dict(self.recovery or {})
        success = recovery.get("success") is True
        home_proof = recovery.get("home_proof")
        if not isinstance(home_proof, Mapping):
            home_proof = {}
        home_verified = bool(
            recovery.get("home_verified") is True
            or home_proof.get("verified") is True
            or recovery.get("state") == "HOME"
        )
        return {
            "invoked": bool(recovery),
            "success": success,
            "home_verified": home_verified,
            "blocked": bool(recovery.get("home_blocked") or recovery.get("state") == "BLOCKED"),
            "source_attempt_stays_failed": self.source_success is not True,
            "failure_condition": recovery.get("home_blocked_reason") or recovery.get("error"),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "tase.figure8-window60-timing-ledger-v1",
            "attempt_id": self.attempt_id,
            "protocol_id": self.protocol_id,
            "events": list(self.events),
            "durations_s": self.durations(),
            "failure_stage": self.failure_stage,
            "failure_condition": self.failure_condition,
            "source_success": self.source_success,
            "recovery": self.recovery_outcome(),
        }


def ledger_from_receipts(
    attempt_id: str,
    *,
    lifecycle_events: Iterable[Mapping[str, Any]] = (),
    dispatch_receipt: Mapping[str, Any] | None = None,
    supervisor_result: Mapping[str, Any] | None = None,
) -> TaseR013TimingLedger:
    """Merge writer timing with supervisor/recovery receipts without promotion."""
    dispatch = dict(dispatch_receipt or {})
    supervisor = dict(supervisor_result or {})
    ledger = TaseR013TimingLedger.from_events(
        attempt_id,
        lifecycle_events,
        failure_stage=dispatch.get("failure_stage") or supervisor.get("failure_stage"),
        failure_condition=dispatch.get("failure_condition") or supervisor.get("error"),
        source_success=(
            dispatch.get("success")
            if dispatch.get("success") is not None
            else supervisor.get("success")
        ),
    )
    recovery = dispatch.get("automatic_home_recovery") or supervisor.get("autonomous_home_recovery")
    ledger.attach_recovery(recovery if isinstance(recovery, Mapping) else None)
    return ledger


def summarize_timing_ledgers(ledgers: Iterable[TaseR013TimingLedger]) -> dict[str, Any]:
    rows = list(ledgers)
    if not rows:
        raise TimingLedgerError("cannot summarize an empty timing ledger")
    home_to_home = [
        value for value in (ledger.durations()["home_to_home_s"] for ledger in rows)
        if value is not None
    ]
    path_values = [
        value for value in (ledger.durations()["formal_path_s"] for ledger in rows)
        if value is not None
    ]
    failure_stages: dict[str, int] = {}
    for ledger in rows:
        if ledger.failure_stage:
            failure_stages[ledger.failure_stage] = failure_stages.get(ledger.failure_stage, 0) + 1

    def summary(values: list[float]) -> dict[str, Any]:
        return {
            "count": len(values),
            "median_s": statistics.median(values) if values else None,
            "p90_s": (
                sorted(values)[max(0, math.ceil(0.9 * len(values)) - 1)] if values else None
            ),
        }

    return {
        "schema": "tase.figure8-window60-timing-summary-v1",
        "attempt_count": len(rows),
        "home_to_home": summary(home_to_home),
        "formal_path": summary(path_values),
        "failure_stage_counts": failure_stages,
        "recovery_success_count": sum(ledger.recovery_outcome()["success"] for ledger in rows),
        "recovery_blocked_count": sum(ledger.recovery_outcome()["blocked"] for ledger in rows),
    }


__all__ = [
    "STAGES",
    "TaseR013TimingLedger",
    "TimingLedgerError",
    "ledger_from_receipts",
    "summarize_timing_ledgers",
]
