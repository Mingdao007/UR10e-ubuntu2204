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
    missing_events: list[dict[str, Any]] = field(default_factory=list)
    supplemental_events: list[dict[str, Any]] = field(default_factory=list)

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
        path_end = pick(
            self._first("PATH", "end"),
            self._last("PATH", "stop"),
            self._first("STOP"),
        )
        result["formal_path_s"] = None if path_start is None or path_end is None else path_end - path_start
        home_start = pick(
            self._first("HOME", "start"),
            self._first("UNLOAD_RELIEF"),
        )
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
        nested_home = recovery.get("home")
        if not isinstance(nested_home, Mapping):
            nested_home = {}
        recovered_state = str(recovery.get("state", "")).upper()
        home_verified = bool(
            recovery.get("home_verified") is True
            or home_proof.get("verified") is True
            or recovered_state in {"HOME", "HOME_VERIFIED", "HOME_RECOVERED"}
            or (
                nested_home.get("success") is True
                and str(nested_home.get("state", "")).upper()
                in {"HOME", "HOME_VERIFIED", "HOME_RECOVERED"}
            )
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
            # A missing timestamp is evidence that the current owner did not
            # expose that transition.  Keep the typed stage token instead of
            # deriving a timestamp from a neighboring stage or a target
            # duration.
            "missing_events": list(self.missing_events),
            "supplemental_events": list(self.supplemental_events),
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
    missing_events: list[dict[str, Any]] = []

    def collect(source: Mapping[str, Any], source_name: str) -> list[Mapping[str, Any]]:
        raw = source.get("lifecycle_events", source.get("timing_events", ()))
        if raw is None:
            return []
        if not isinstance(raw, Iterable) or isinstance(raw, (str, bytes, Mapping)):
            missing_events.append({
                "source": source_name,
                "stage": None,
                "event": "invalid_lifecycle_events",
                "timestamp_s": None,
            })
            return []
        rows: list[Mapping[str, Any]] = []
        for row in raw:
            if not isinstance(row, Mapping):
                missing_events.append({
                    "source": source_name,
                    "stage": None,
                    "event": "invalid_lifecycle_event",
                    "timestamp_s": None,
                })
                continue
            stage = str(row.get("stage", ""))
            timestamp = row.get("timestamp_s")
            if stage not in _STAGE_INDEX or timestamp is None:
                missing_events.append({
                    "source": source_name,
                    "stage": stage or None,
                    "event": str(row.get("event", "start")),
                    "timestamp_s": None,
                })
                continue
            try:
                _finite(timestamp, f"{stage} timestamp")
            except TimingLedgerError:
                missing_events.append({
                    "source": source_name,
                    "stage": stage,
                    "event": str(row.get("event", "start")),
                    "timestamp_s": None,
                })
                continue
            rows.append(row)
        return rows

    merged_events: list[Mapping[str, Any]] = []
    # Caller-supplied events are retained first for backwards compatibility;
    # receipt-attached events are then merged by exact identity.  The writer
    # owns dispatch events and the supervisor owns recovery events, so neither
    # side can silently replace the other.
    argument_rows = list(lifecycle_events)
    merged_events.extend(collect({"lifecycle_events": argument_rows}, "argument"))
    merged_events.extend(collect(dispatch, "dispatch"))
    merged_events.extend(collect(supervisor, "supervisor"))
    recovery_candidate = dispatch.get("automatic_home_recovery") or supervisor.get("autonomous_home_recovery")
    if isinstance(recovery_candidate, Mapping):
        merged_events.extend(collect(recovery_candidate, "recovery"))
    deduplicated: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str, float]] = set()
    for row in merged_events:
        key = (str(row.get("stage")), str(row.get("event", "start")), float(row["timestamp_s"]))
        if key not in seen:
            seen.add(key)
            deduplicated.append(row)
    # Dispatch, supervisor, and recovery run in the same host monotonic clock
    # but report different ownership boundaries.  Merge by the protocol stage
    # before validation so a later source cannot append HOME_CHECK after PATH
    # and create a false lifecycle regression.  Timestamps are never filled
    # or shifted; they are only ordered within their typed stage.
    deduplicated.sort(
        key=lambda row: (
            _STAGE_INDEX[str(row["stage"])],
            float(row["timestamp_s"]),
            str(row.get("event", "start")),
        )
    )
    # A cleanup owner may report a late STOP after the writer already emitted
    # the PATH-end STOP and the recovery route has emitted UNLOAD/CLEARANCE.
    # Keep that raw boundary for audit, but model only one canonical STOP in
    # the ordered lifecycle.  PATH and HOME retain their legal start/end
    # pairs; repeated single-boundary stages are supplemental evidence.
    canonical: list[Mapping[str, Any]] = []
    supplemental: list[dict[str, Any]] = []
    seen_stage_events: set[tuple[str, str]] = set()
    for row in deduplicated:
        stage = str(row["stage"])
        event = str(row.get("event", "start"))
        legal_pair = stage in {"PATH", "HOME"} and event in {
            "start", "end", "stop", "verified"
        }
        identity = (stage, event)
        if not legal_pair and identity in seen_stage_events:
            supplemental.append(dict(row))
            continue
        if stage == "STOP" and any(str(item["stage"]) == "STOP" for item in canonical):
            supplemental.append(dict(row))
            continue
        if identity in seen_stage_events:
            supplemental.append(dict(row))
            continue
        seen_stage_events.add(identity)
        canonical.append(row)
    ordered_events: list[Mapping[str, Any]] = []
    last_timestamp = -math.inf
    for row in canonical:
        timestamp = float(row["timestamp_s"])
        if timestamp < last_timestamp:
            supplemental.append({
                **dict(row),
                "reason": "timestamp_regressed_across_owner_boundaries",
            })
            missing_events.append({
                "source": "merge",
                "stage": str(row["stage"]),
                "event": str(row.get("event", "start")),
                "timestamp_s": None,
                "reason": "timestamp_regressed_across_owner_boundaries",
            })
            continue
        ordered_events.append(row)
        last_timestamp = timestamp
    ledger = TaseR013TimingLedger.from_events(
        attempt_id,
        ordered_events,
        failure_stage=dispatch.get("failure_stage") or supervisor.get("failure_stage"),
        failure_condition=dispatch.get("failure_condition") or supervisor.get("error"),
        source_success=(
            dispatch.get("success")
            if dispatch.get("success") is not None
            else supervisor.get("success")
        ),
    )
    ledger.missing_events.extend(missing_events)
    ledger.supplemental_events.extend(supplemental)
    recovery = dispatch.get("automatic_home_recovery") or supervisor.get("autonomous_home_recovery")
    ledger.attach_recovery(recovery if isinstance(recovery, Mapping) else None)
    return ledger


def lifecycle_events_from_writer(
    writer: Any,
    *,
    home_check_s: float | None = None,
    contact_search_s: float | None = None,
    stop_s: float | None = None,
    home_verified: bool = False,
) -> list[dict[str, Any]]:
    """Extract typed stage transitions already exposed by the sole writer.

    This is a read-only reducer over writer observations.  It never calls a
    clock and never fills a missing stage from a nominal duration.  TP state
    20/21/25/40/78, the writer PATH-end handshake, and the evidence-backed
    Home proof are the only accepted transition sources.
    """
    # R006/R005 adapters keep the actual R004 writer in ``.writer``.  Reduce
    # the physical writer's buffers and boundary markers; the adapter itself
    # is only a compatibility shell and does not own those observations.
    base_writer = getattr(writer, "writer", writer)

    def finite_or_none(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    def row_time(row: Any) -> float | None:
        if isinstance(row, Mapping):
            return finite_or_none(row.get("received_monotonic_s", row.get("monotonic_s")))
        return finite_or_none(getattr(row, "received_monotonic_s", getattr(row, "monotonic_s", None)))

    def row_state(row: Any) -> int | None:
        echoes = row.get("integer_echoes") if isinstance(row, Mapping) else getattr(row, "integer_echoes", None)
        if isinstance(echoes, Mapping):
            value = echoes.get(26, echoes.get("26"))
            try:
                return None if value is None else int(value)
            except (TypeError, ValueError):
                return None
        try:
            return int(echoes[26])
        except (TypeError, KeyError, IndexError, ValueError):
            return None

    first_state: dict[int, float] = {}
    rows = list(getattr(base_writer, "robot_observations", ()) or ())
    rows.extend(list(getattr(base_writer, "admission_robot_observations", ()) or ()))
    for row in rows:
        state = row_state(row)
        timestamp = row_time(row)
        if state in {20, 21, 25, 40, 78} and timestamp is not None and state not in first_state:
            first_state[state] = timestamp

    candidates: dict[str, tuple[float | None, str]] = {
        "HOME_CHECK": (finite_or_none(home_check_s), "verified_preflight"),
        "CONTACT_SEARCH": (
            first_state.get(20, finite_or_none(contact_search_s)),
            "tp_state_20" if 20 in first_state else "arm_complete",
        ),
        "CONTACT_LATCH": (first_state.get(21), "tp_state_20_to_21"),
        "QUALIFICATION": (first_state.get(21), "tp_state_21"),
        # The raw writer receipt does not expose the end of the 10 s readiness
        # dwell as a typed transition.  Keep it missing instead of duplicating
        # the CONTACT_LATCH time and claiming a false duration.
        "READINESS_HOLD": (None, "missing_readiness_boundary"),
        "ENTRY": (first_state.get(25), "tp_state_25"),
        "PATH": (
            finite_or_none(getattr(base_writer, "_path_command_started_mono_s", None)),
            "first_consumed_path_command",
        ),
        "STOP": (
            finite_or_none(getattr(base_writer, "_r013_path_end_request_mono_s", None))
            or finite_or_none(stop_s),
            "path_end_handshake" if getattr(base_writer, "_r013_path_end_request_mono_s", None) is not None else "stop_request",
        ),
        "UNLOAD_RELIEF": (first_state.get(40), "tp_state_40"),
        "CLEARANCE": (first_state.get(78), "tp_state_78"),
        "HOME": (first_state.get(78) if home_verified else None, "home_proof"),
    }
    return [
        {"stage": stage, "event": event, "timestamp_s": timestamp}
        for stage in STAGES
        for timestamp, event in (candidates[stage],)
    ]


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
    "lifecycle_events_from_writer",
    "summarize_timing_ledgers",
]
