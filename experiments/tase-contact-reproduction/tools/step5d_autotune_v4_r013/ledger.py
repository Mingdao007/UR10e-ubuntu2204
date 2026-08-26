"""Fresh append-only R013 ledger; no R012/R008 observation migration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


LEDGER_SCHEMA = "step5d.autotune-v4/r013-ledger-v1"
REVISION = 13
RECORD_TYPES = frozenset({
    "header", "dispatch", "observation", "rejected", "hard_guard", "fit_receipt",
    "anchor_retest_plan", "local_refinement_plan", "high_ki_probe_plan",
    "normal_velocity_gain_probe_plan",
    "normal_velocity_gain_probe_stop",
    "lower_p_over_d_probe_plan",
    "strategy_canary_plan",
    "floor_coordinator",
})


class R013LedgerError(ValueError):
    """R013 ledger is malformed, mixed-lineage, or non-monotonic."""


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise R013LedgerError("R013 ledger payload is not strict JSON") from exc


def _validate(records: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    if not records:
        raise R013LedgerError("R013 ledger is empty")
    header = records[0]
    required_header = {
        "schema", "revision", "record_type", "sequence", "campaign_id", "run_id",
        "attempt_id", "optimizer_snapshot", "training_lineage",
    }
    if set(header) != required_header:
        raise R013LedgerError("R013 ledger header fields differ")
    if (
        header.get("schema") != LEDGER_SCHEMA
        or header.get("revision") != REVISION
        or header.get("record_type") != "header"
        or header.get("sequence") != 0
        or header.get("training_lineage") != "fresh_r013_only"
    ):
        raise R013LedgerError("R013 ledger header contract differs")
    owner = tuple(header.get(name) for name in ("campaign_id", "run_id", "attempt_id"))
    if not all(isinstance(value, str) and value for value in owner):
        raise R013LedgerError("R013 ledger identity is invalid")
    for expected_sequence, record in enumerate(records[1:], 1):
        if record.get("schema") != LEDGER_SCHEMA or record.get("revision") != REVISION:
            raise R013LedgerError("non-R013 record found in R013 ledger")
        if record.get("record_type") not in RECORD_TYPES - {"header"}:
            raise R013LedgerError("R013 ledger record type differs")
        if record.get("sequence") != expected_sequence:
            raise R013LedgerError("R013 ledger sequence is not monotonic")
        if tuple(record.get(name) for name in ("campaign_id", "run_id", "attempt_id")) != owner:
            raise R013LedgerError("R013 ledger record identity differs")
        if set(record) != {
            "schema", "revision", "record_type", "sequence", "campaign_id", "run_id",
            "attempt_id", "payload",
        }:
            raise R013LedgerError("R013 ledger record fields differ")
        if not isinstance(record.get("payload"), dict):
            raise R013LedgerError("R013 ledger payload is invalid")
    return tuple(records)


class Ledger:
    def __init__(self, path: Path, records: tuple[dict[str, Any], ...]) -> None:
        self.path = Path(path)
        self._records = records

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        campaign_id: str,
        run_id: str,
        attempt_id: str,
        optimizer_snapshot: Mapping[str, Any],
    ) -> "Ledger":
        destination = Path(path)
        if destination.exists():
            raise R013LedgerError(f"fresh R013 ledger already exists: {destination}")
        if not all(isinstance(value, str) and value for value in (campaign_id, run_id, attempt_id)):
            raise R013LedgerError("R013 ledger identity is invalid")
        if optimizer_snapshot.get("schema") != "step5d.autotune-v4/r013-optimizer-snapshot-v1":
            raise R013LedgerError("R013 ledger requires an optimizer snapshot")
        destination.parent.mkdir(parents=True, exist_ok=True)
        header = {
            "schema": LEDGER_SCHEMA,
            "revision": REVISION,
            "record_type": "header",
            "sequence": 0,
            "campaign_id": campaign_id,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "optimizer_snapshot": dict(optimizer_snapshot),
            "training_lineage": "fresh_r013_only",
        }
        with destination.open("xb") as stream:
            stream.write(_canonical(header) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        return cls(destination, (header,))

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        source = Path(path)
        if source.is_symlink() or not source.is_file():
            raise R013LedgerError(f"R013 ledger must be a regular file: {source}")
        records: list[dict[str, Any]] = []
        try:
            with source.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        raise R013LedgerError(f"R013 ledger contains blank line {line_number}")
                    value = json.loads(
                        line,
                        parse_constant=lambda token: (_ for _ in ()).throw(
                            R013LedgerError(f"R013 ledger contains {token}")
                        ),
                    )
                    if not isinstance(value, dict):
                        raise R013LedgerError(f"R013 ledger line {line_number} is not an object")
                    records.append(value)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise R013LedgerError(f"cannot read R013 ledger: {source}") from exc
        return cls(source, _validate(records))

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        return self._records

    @property
    def header(self) -> Mapping[str, Any]:
        return self._records[0]

    def append(self, record_type: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if record_type not in RECORD_TYPES - {"header"}:
            raise R013LedgerError("R013 append record type differs")
        header = self.header
        record = {
            "schema": LEDGER_SCHEMA,
            "revision": REVISION,
            "record_type": record_type,
            "sequence": len(self._records),
            "campaign_id": header["campaign_id"],
            "run_id": header["run_id"],
            "attempt_id": header["attempt_id"],
            "payload": dict(payload),
        }
        with self.path.open("ab") as stream:
            stream.write(_canonical(record) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._records = (*self._records, record)
        return record


__all__ = ["LEDGER_SCHEMA", "Ledger", "R013LedgerError", "REVISION"]
