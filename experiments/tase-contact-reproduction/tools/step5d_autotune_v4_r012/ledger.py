"""Direct-append R012 JSONL ledger."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .censor import CensoredObservation, ExactObservation, Observation, validate_observation
from .common import R012ValueError, canonical_bytes, strict_json_object
from .safety_filter import SafetyIntervention, validate_safety_intervention


LEDGER_SCHEMA = "step5d.autotune-v4/r012-ledger-v2"
HEADER_RECORD_TYPE = "header"
DISPATCH_RECORD_TYPE = "dispatch"
OBSERVATION_RECORD_TYPE = "observation"
SAFETY_INTERVENTION_RECORD_TYPE = "safety_intervention"


class R012LedgerError(R012ValueError):
    """R012 ledger JSONL is malformed or inconsistent."""


def header_record(*, campaign_id: str, run_id: str, attempt_id: str) -> dict[str, Any]:
    return {
        "schema": LEDGER_SCHEMA,
        "record_type": HEADER_RECORD_TYPE,
        "revision": 12,
        "campaign_id": campaign_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "sequence": 0,
    }


def _scheduled_dispatch(value: Any) -> dict[str, Any]:
    raw = value.as_dict() if hasattr(value, "as_dict") else value
    if not isinstance(raw, Mapping):
        raise R012LedgerError("R012 dispatch must be a scheduled candidate")
    required = {
        "schema", "candidate", "kind", "ordinal", "confirmation_index", "abort_allowed",
    }
    if set(raw) != required or raw.get("schema") != "step5d.autotune-v4/r012-scheduled-candidate-v1":
        raise R012LedgerError("R012 dispatch fields differ")
    if not isinstance(raw.get("candidate"), Mapping):
        raise R012LedgerError("R012 dispatch candidate is invalid")
    if not isinstance(raw.get("kind"), str) or not raw["kind"]:
        raise R012LedgerError("R012 dispatch kind is invalid")
    ordinal = raw.get("ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0:
        raise R012LedgerError("R012 dispatch ordinal is invalid")
    confirmation = raw.get("confirmation_index")
    if confirmation is not None and (
        isinstance(confirmation, bool) or not isinstance(confirmation, int) or confirmation <= 0
    ):
        raise R012LedgerError("R012 dispatch confirmation index is invalid")
    if not isinstance(raw.get("abort_allowed"), bool):
        raise R012LedgerError("R012 dispatch abort policy is invalid")
    return dict(raw)


def _read_records(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise R012LedgerError(f"R012 ledger must be a regular file: {path}")
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for index, line in enumerate(stream, 1):
                if not line.strip():
                    raise R012LedgerError(f"R012 ledger contains blank line {index}")
                try:
                    value = json.loads(line, object_pairs_hook=dict, parse_constant=lambda token: (_ for _ in ()).throw(R012LedgerError(f"R012 ledger contains {token}")))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise R012LedgerError(f"R012 ledger line {index} is invalid") from exc
                if not isinstance(value, dict):
                    raise R012LedgerError(f"R012 ledger line {index} is not an object")
                rows.append(value)
    except OSError as exc:
        raise R012LedgerError(f"cannot read R012 ledger: {path}") from exc
    if not rows:
        raise R012LedgerError("R012 ledger is empty")
    return rows


def _validate_records(records: list[dict[str, Any]], *, campaign_id: str | None = None, run_id: str | None = None, attempt_id: str | None = None) -> tuple[dict[str, Any], ...]:
    header = records[0]
    expected = {"schema", "record_type", "revision", "campaign_id", "run_id", "attempt_id", "sequence"}
    if set(header) != expected or header.get("schema") != LEDGER_SCHEMA or header.get("record_type") != HEADER_RECORD_TYPE or header.get("revision") != 12 or header.get("sequence") != 0:
        raise R012LedgerError("R012 ledger header fields differ")
    owner = (header["campaign_id"], header["run_id"], header["attempt_id"])
    if not all(isinstance(value, str) and value for value in owner):
        raise R012LedgerError("R012 ledger header identity is invalid")
    if campaign_id is not None and owner != (campaign_id, run_id, attempt_id):
        raise R012LedgerError("R012 ledger header identity differs")
    previous = 0
    for record in records[1:]:
        common = {"schema", "record_type", "revision", "campaign_id", "run_id", "attempt_id", "sequence"}
        if not common.issubset(record) or record.get("schema") != LEDGER_SCHEMA or record.get("revision") != 12:
            raise R012LedgerError("R012 ledger record fields differ")
        if (record["campaign_id"], record["run_id"], record["attempt_id"]) != owner:
            raise R012LedgerError("R012 ledger record identity differs")
        if not isinstance(record["attempt_id"], str) or not record["attempt_id"]:
            raise R012LedgerError("R012 ledger attempt identity is invalid")
        sequence = record.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence != previous + 1:
            raise R012LedgerError("R012 ledger sequence is not monotonic")
        if record.get("record_type") == DISPATCH_RECORD_TYPE:
            if set(record) != common | {"dispatch"}:
                raise R012LedgerError("R012 dispatch ledger fields differ")
            _scheduled_dispatch(record["dispatch"])
        elif record.get("record_type") == OBSERVATION_RECORD_TYPE:
            if set(record) != common | {"observation"}:
                raise R012LedgerError("R012 observation ledger fields differ")
            validate_observation(record["observation"])
        elif record.get("record_type") == SAFETY_INTERVENTION_RECORD_TYPE:
            if set(record) != common | {"intervention"}:
                raise R012LedgerError("R012 safety ledger fields differ")
            validate_safety_intervention(record["intervention"])
        else:
            raise R012LedgerError("R012 ledger record type differs")
        previous = sequence
    return tuple(records)


class Ledger:
    """Append one JSON object per event; existing rows are never rewritten."""

    def __init__(self, path: Path, *, campaign_id: str, run_id: str, attempt_id: str, records: tuple[dict[str, Any], ...]) -> None:
        self.path = Path(path)
        self.campaign_id = campaign_id
        self.run_id = run_id
        self.attempt_id = attempt_id
        self._records = records

    @classmethod
    def create(cls, path: Path, *, campaign_id: str, run_id: str, attempt_id: str) -> "Ledger":
        path = Path(path)
        if path.exists():
            raise R012LedgerError(f"R012 ledger already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        header = header_record(campaign_id=campaign_id, run_id=run_id, attempt_id=attempt_id)
        path.write_bytes(canonical_bytes(header) + b"\n")
        return cls(path, campaign_id=campaign_id, run_id=run_id, attempt_id=attempt_id, records=(header,))

    @classmethod
    def load(cls, path: Path, *, campaign_id: str | None = None, run_id: str | None = None, attempt_id: str | None = None) -> "Ledger":
        records = _validate_records(_read_records(Path(path)), campaign_id=campaign_id, run_id=run_id, attempt_id=attempt_id)
        header = records[0]
        return cls(Path(path), campaign_id=header["campaign_id"], run_id=header["run_id"], attempt_id=header["attempt_id"], records=records)

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        return self._records

    @property
    def next_sequence(self) -> int:
        return len(self._records)

    def _append(self, record_type: str, payload_key: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        record = {
            "schema": LEDGER_SCHEMA,
            "record_type": record_type,
            "revision": 12,
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "sequence": self.next_sequence,
            payload_key: dict(payload),
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(canonical_bytes(record).decode("utf-8") + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._records = (*self._records, record)
        return record

    def append_observation(self, observation: Observation | Mapping[str, Any]) -> dict[str, Any]:
        row = validate_observation(observation)
        if (row.campaign_id, row.run_id, row.attempt_id) != (self.campaign_id, self.run_id, self.attempt_id):
            raise R012LedgerError("R012 observation identity differs from ledger")
        return self._append(OBSERVATION_RECORD_TYPE, "observation", row.as_dict())

    def append_dispatch(self, dispatch: Any) -> dict[str, Any]:
        return self._append(DISPATCH_RECORD_TYPE, "dispatch", _scheduled_dispatch(dispatch))

    def append_safety_intervention(self, intervention: SafetyIntervention | Mapping[str, Any]) -> dict[str, Any]:
        row = validate_safety_intervention(intervention)
        if (row.campaign_id, row.run_id, row.attempt_id) != (self.campaign_id, self.run_id, self.attempt_id):
            raise R012LedgerError("R012 safety identity differs from ledger")
        return self._append(SAFETY_INTERVENTION_RECORD_TYPE, "intervention", row.as_dict())


__all__ = ["DISPATCH_RECORD_TYPE", "HEADER_RECORD_TYPE", "LEDGER_SCHEMA", "Ledger", "OBSERVATION_RECORD_TYPE", "R012LedgerError", "SAFETY_INTERVENTION_RECORD_TYPE", "header_record"]
