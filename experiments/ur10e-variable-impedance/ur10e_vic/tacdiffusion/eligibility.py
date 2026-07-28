"""The sole final TacDiffusion training-eligibility verdict writer."""

from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
import math
import os
from pathlib import Path
import threading
from typing import Any, Iterable, Mapping, Sequence


ELIGIBILITY_SCHEMA = "ur10e_tacdiffusion_training_eligibility/v2"
_MISSING = object()
HOST_BATCH_WATCHDOG_S = 0.080
MAX_UNSEALED_TAIL = 9
ACTION_DIMENSION = 12


def _value(row: object, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _validity_flag(row: object, name: str, default: bool) -> bool:
    flags = _value(row, "recorder_validity_flags", {})
    if isinstance(flags, Mapping) and name in flags:
        return bool(flags[name])
    return bool(_value(row, name, default))


def _vector(row: object, name: str) -> tuple[float, ...] | None:
    values = _value(row, name)
    if values is None:
        return None
    try:
        result = tuple(float(value) for value in values)
    except (TypeError, ValueError):
        return None
    if not result or not all(math.isfinite(value) for value in result):
        return None
    return result


@dataclass(frozen=True)
class EligibilityDecision:
    episode_id: str
    training_eligible: bool
    first_live_shadow: bool
    predicates: Mapping[str, bool]
    reasons: tuple[str, ...]
    live_ready: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": ELIGIBILITY_SCHEMA,
            "episode_id": self.episode_id,
            "training_eligible": self.training_eligible,
            "first_live_shadow": self.first_live_shadow,
            "predicates": dict(self.predicates),
            "reasons": list(self.reasons),
            "live_ready": self.live_ready,
        }


def _health_value(health: object, name: str, default: Any = None) -> Any:
    if isinstance(health, Mapping):
        return health.get(name, default)
    return getattr(health, name, default)


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class EligibilityValidator:
    """Compute and atomically publish the only final eligibility receipt.

    A frame may be retained with invalid/torn flags.  Such a row is evidence,
    not a reason to discard the row or to let another writer manufacture a
    training view.
    """

    def __init__(self, *, receipt_schema: str = ELIGIBILITY_SCHEMA) -> None:
        if receipt_schema != ELIGIBILITY_SCHEMA:
            raise ValueError("eligibility schema is frozen")
        self.receipt_schema = receipt_schema
        self._write_lock = threading.Lock()

    def evaluate(
        self,
        frames: Iterable[object],
        *,
        recorder_health: object,
        first_live_shadow: bool = True,
        episode_id: str | None = None,
        live_acceptance_evidence: bool = False,
    ) -> EligibilityDecision:
        rows = tuple(frames)
        resolved_episode_id = episode_id or str(
            _value(rows[0], "episode_id", "unknown") if rows else "unknown"
        )
        reasons: list[str] = []
        control_time_strict = bool(rows)
        previous_control = -math.inf
        previous_sample_index = -1
        previous_control_sequence = -1
        for row in rows:
            value = _value(row, "control_time_s")
            try:
                control = float(value)
                sample_index = int(_value(row, "sample_index"))
                control_sequence = int(_value(row, "control_sequence"))
            except (TypeError, ValueError):
                control_time_strict = False
                continue
            if (
                not math.isfinite(control)
                or control <= previous_control
                or sample_index <= previous_sample_index
                or control_sequence <= previous_control_sequence
                or not _validity_flag(row, "control_time_strict", True)
            ):
                control_time_strict = False
            previous_control = control
            previous_sample_index = sample_index
            previous_control_sequence = control_sequence
        exact_expert_applied = bool(rows) and all(
            (_vector(row, "expert_action_12d") is not None)
            and (_vector(row, "expert_action_12d") == _vector(row, "applied_action_12d"))
            for row in rows
        )
        coherent_echoes = bool(rows)
        for row in rows:
            echoed = _vector(row, "echoed_action_12d")
            if not (
                _validity_flag(row, "action_echo_coherent", False)
                and _validity_flag(row, "echoed_action_valid", False)
                and echoed is not None
                and len(echoed) == ACTION_DIMENSION
            ):
                coherent_echoes = False
        causal_sensor_lineage = bool(rows)
        previous_device = -math.inf
        previous_host = -math.inf
        previous_sample = -1
        previous_batch: int | None = None
        previous_batch_host: float | None = None
        for row in rows:
            control_time = _value(row, "control_time_s")
            device_time = _value(row, "external_device_time_s")
            host_time = _value(row, "external_host_visible_time_s")
            sample_index = _value(row, "external_sample_index")
            if not _validity_flag(row, "external_lineage_valid", False):
                causal_sensor_lineage = False
                continue
            if control_time is None or host_time is None or device_time is None:
                causal_sensor_lineage = False
                continue
            try:
                control = float(control_time)
                device = float(device_time)
                host = float(host_time)
                sample = int(sample_index)
            except (TypeError, ValueError):
                causal_sensor_lineage = False
                continue
            if (
                not all(math.isfinite(value) for value in (control, device, host))
                or host > control + 1e-12
                or control - host > HOST_BATCH_WATCHDOG_S + 1e-12
                or device < previous_device - 1e-12
                or host < previous_host - 1e-12
                or sample < previous_sample
                or (
                    previous_batch is not None
                    and _int_value(_value(row, "external_batch_id", -1), -1) == previous_batch
                    and (
                        previous_batch_host is None
                        or not math.isclose(
                            host,
                            previous_batch_host,
                            rel_tol=0.0,
                            abs_tol=1e-12,
                        )
                    )
                )
                or (
                    bool(_value(row, "external_hold", False))
                    and _int_value(_value(row, "external_held_ticks", 0) or 0, 0) <= 0
                )
            ):
                causal_sensor_lineage = False
            previous_device, previous_host, previous_sample = device, host, sample
            previous_batch = _int_value(_value(row, "external_batch_id", -1), -1)
            previous_batch_host = host
        internal_wrench_valid = bool(rows) and all(
            _validity_flag(row, "internal_wrench_valid", False) for row in rows
        )
        retained_rows_valid = bool(rows) and all(
            _validity_flag(row, "row_valid", False) for row in rows
        )
        no_overflow = (
            _health_value(recorder_health, "overflowed", _MISSING) is False
        )
        no_stall = _health_value(recorder_health, "stalled", _MISSING) is False
        dropped_rows = _health_value(
            recorder_health,
            "dropped_rows",
            _MISSING,
        )
        rejected_rows = _health_value(
            recorder_health,
            "rejected_rows",
            _MISSING,
        )
        no_drop = (
            dropped_rows is not _MISSING
            and rejected_rows is not _MISSING
            and _int_value(dropped_rows, -1) == 0
            and _int_value(rejected_rows, -1) == 0
        )
        enqueued_rows = _health_value(
            recorder_health,
            "enqueued_rows",
            _MISSING,
        )
        durable_rows = _health_value(
            recorder_health,
            "durable_rows",
            _MISSING,
        )
        queue_depth = _health_value(
            recorder_health,
            "queue_depth",
            _MISSING,
        )
        unsealed_tail = _health_value(
            recorder_health,
            "unsealed_tail",
            _MISSING,
        )
        max_unsealed_tail = _health_value(
            recorder_health,
            "max_unsealed_tail",
            _MISSING,
        )
        complete_seal = bool(
            _health_value(recorder_health, "sealed", _MISSING) is True
            and _health_value(recorder_health, "manifest_written", _MISSING)
            is True
            and _health_value(recorder_health, "durability_mode", "") == "batch_fsync_10"
            and enqueued_rows is not _MISSING
            and durable_rows is not _MISSING
            and _int_value(enqueued_rows, -1) >= 0
            and _int_value(durable_rows, -2) == _int_value(enqueued_rows, -1)
            and queue_depth is not _MISSING
            and _int_value(queue_depth, -1) == 0
            and unsealed_tail is not _MISSING
            and _int_value(unsealed_tail, -1) == 0
            and max_unsealed_tail is not _MISSING
            and 0 <= _int_value(max_unsealed_tail, -1) <= MAX_UNSEALED_TAIL
        )
        tamper_free = (
            _health_value(recorder_health, "tamper_free", _MISSING) is True
        )
        fault_value = _health_value(recorder_health, "fault", _MISSING)
        writer_error = _health_value(
            recorder_health,
            "writer_error",
            _MISSING,
        )
        no_writer_fault = (
            fault_value is None
            and writer_error is None
        )
        predicates = {
            "non_empty": bool(rows),
            "strict_control_time": control_time_strict,
            "exact_expert_equals_applied": exact_expert_applied,
            "coherent_controller_echoes": coherent_echoes,
            "causal_valid_sensor_lineage": causal_sensor_lineage,
            "internal_wrench_valid": internal_wrench_valid,
            "retained_rows_valid": retained_rows_valid,
            "no_spool_overflow": no_overflow,
            "no_recorder_stall": no_stall,
            "no_drop_or_rejection": no_drop,
            "complete_seal": complete_seal,
            "tamper_free": tamper_free,
            "no_writer_fault": no_writer_fault,
            "first_live_shadow_clear": not first_live_shadow,
        }
        for name, passed in predicates.items():
            if not passed:
                reasons.append(name)
        if first_live_shadow:
            reasons.append("first_live_shadow_forced_ineligible")
        eligible = all(predicates.values())
        return EligibilityDecision(
            episode_id=resolved_episode_id,
            training_eligible=eligible,
            first_live_shadow=first_live_shadow,
            predicates=predicates,
            reasons=tuple(dict.fromkeys(reasons)),
            # Live readiness has independent route/controller/safety evidence;
            # this offline validator never creates that evidence.
            live_ready=bool(eligible and live_acceptance_evidence),
        )

    def write_receipt(
        self,
        path: str | Path,
        decision: EligibilityDecision,
        *,
        recorder_health: object | None = None,
    ) -> dict[str, Any]:
        if not isinstance(decision, EligibilityDecision):
            raise TypeError("EligibilityValidator is the sole receipt writer")
        payload = decision.as_json()
        if recorder_health is not None:
            health_json = (
                recorder_health.as_json()
                if hasattr(recorder_health, "as_json")
                else dict(recorder_health)
            )
            payload["recorder_health"] = health_json
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        payload["decision_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        destination = Path(path)
        with self._write_lock:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise FileExistsError(f"eligibility receipt already exists: {destination}")
            temporary = destination.with_name(
                f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                with temporary.open("x", encoding="utf-8") as handle:
                    json.dump(payload, handle, sort_keys=True, indent=2, allow_nan=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
                directory_fd = os.open(destination.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                temporary.unlink(missing_ok=True)
        return payload

    def evaluate_and_write(
        self,
        path: str | Path,
        frames: Iterable[object],
        *,
        recorder_health: object,
        first_live_shadow: bool = True,
        episode_id: str | None = None,
    ) -> tuple[EligibilityDecision, dict[str, Any]]:
        decision = self.evaluate(
            frames,
            recorder_health=recorder_health,
            first_live_shadow=first_live_shadow,
            episode_id=episode_id,
        )
        return decision, self.write_receipt(path, decision, recorder_health=recorder_health)


def read_eligibility_receipt(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != ELIGIBILITY_SCHEMA:
        raise ValueError("eligibility receipt schema mismatch")
    return payload


__all__ = [
    "ELIGIBILITY_SCHEMA",
    "EligibilityDecision",
    "EligibilityValidator",
    "read_eligibility_receipt",
]
