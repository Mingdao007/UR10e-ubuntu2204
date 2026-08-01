"""Durable campaign rows and live hash-chain verification for r004."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


GENESIS_SHA256 = "0" * 64
LEDGER_SCHEMA = "step5d.autotune-v4/r004-durable-campaign-ledger-v1"
REQUIRED_ROW_FIELDS = frozenset(
    {
        "session_epoch",
        "attempt_execution_id",
        "controller_receipt_sha256",
        "script1_receipt_sha256",
        "input_baseline_ledger_sha256",
        "output_ledger_sha256",
    }
)
REQUIRED_APPEND_FIELDS = REQUIRED_ROW_FIELDS - {"output_ledger_sha256"}
DIGEST_FIELDS = frozenset(
    {
        "controller_receipt_sha256",
        "script1_receipt_sha256",
        "input_baseline_ledger_sha256",
        "output_ledger_sha256",
        "previous_output_sha256",
        "row_sha256",
        "sha256",
    }
)


class LedgerError(RuntimeError):
    """A campaign row cannot be durably trusted or resumed."""


def canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_mapping(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _digest(value: Any, role: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise LedgerError(f"{role} must be a lowercase SHA-256")
    return value


def _row_hash(row: Mapping[str, Any]) -> str:
    return sha256_mapping({key: value for key, value in row.items() if key not in {"row_sha256", "sha256"}})


def _output_hash(row: Mapping[str, Any]) -> str:
    return sha256_mapping(
        {
            "schema": LEDGER_SCHEMA,
            "ordinal": row["logical_attempt_ordinal"],
            "attempt_execution_id": row["attempt_execution_id"],
            "previous_output_sha256": row["previous_output_sha256"],
            "input_baseline_ledger_sha256": row["input_baseline_ledger_sha256"],
            "status": row["status"],
            "completed": row["completed"],
            "output_seal": row.get("output_seal", {}),
        }
    )


def _validate_row_shape(row: Mapping[str, Any], index: int, previous_output: str) -> None:
    if row.get("schema") != LEDGER_SCHEMA or row.get("record_type") != "attempt":
        raise LedgerError(f"ledger row {index} schema differs")
    missing = REQUIRED_ROW_FIELDS.difference(row)
    if missing:
        raise LedgerError(f"ledger row {index} omits required fields: {sorted(missing)}")
    if row.get("previous_output_sha256") != previous_output:
        raise LedgerError(f"ledger row {index} previous-output chain differs")
    if row.get("output_ledger_sha256") != _output_hash(row):
        raise LedgerError(f"ledger row {index} output seal differs")
    expected_row_hash = _row_hash(row)
    if row.get("row_sha256") != expected_row_hash or row.get("sha256") != expected_row_hash:
        raise LedgerError(f"ledger row {index} row hash differs")
    for field in DIGEST_FIELDS:
        _digest(row[field], f"ledger row {index} {field}")
    if not isinstance(row["session_epoch"], int) or isinstance(row["session_epoch"], bool) or row["session_epoch"] <= 0:
        raise LedgerError(f"ledger row {index} session epoch is invalid")
    if not isinstance(row["logical_attempt_ordinal"], int) or not 1 <= row["logical_attempt_ordinal"] <= 16:
        raise LedgerError(f"ledger row {index} ordinal is invalid")
    if not isinstance(row["attempt_execution_id"], str) or not row["attempt_execution_id"]:
        raise LedgerError(f"ledger row {index} execution id is invalid")
    if row.get("status") == "interrupted" and row.get("gp_eligible") is True:
        raise LedgerError(f"ledger row {index} interrupted execution entered GP")


def verify_ledger_hash_chain(source: Path | Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Verify every durable row; callers use this before every next ARM."""

    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.is_symlink() or not path.is_file():
            raise LedgerError(f"ledger path is not a regular file: {path}")
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerError(f"ledger row {line_number} is not JSON") from exc
            if not isinstance(value, dict):
                raise LedgerError(f"ledger row {line_number} is not an object")
            rows.append(value)
    else:
        rows = [dict(row) for row in source]
    previous = GENESIS_SHA256
    last_ordinal = 0
    for index, row in enumerate(rows, start=1):
        _validate_row_shape(row, index, previous)
        if row["logical_attempt_ordinal"] < last_ordinal or row["logical_attempt_ordinal"] > last_ordinal + 1:
            raise LedgerError(f"ledger row {index} ordinal is not contiguous")
        last_ordinal = row["logical_attempt_ordinal"]
        previous = row["output_ledger_sha256"]
    return tuple(rows)


@dataclass(frozen=True)
class DurabilityReceipt:
    row_sha256: str
    output_ledger_sha256: str
    fsynced: bool
    cold_read: bool
    hash_verified: bool

    @property
    def ready_for_next_arm(self) -> bool:
        return self.fsynced and self.cold_read and self.hash_verified


@dataclass(frozen=True)
class ResumeRequest:
    logical_attempt_ordinal: int
    new_attempt_execution_id: str
    requires_script1: bool
    requires_new_epoch: bool
    last_completed_output_seal: str


class DurableCampaignLedger:
    """Append-only ledger whose append path performs fsync, cold-read, and verify."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if self.path.exists() and (self.path.is_symlink() or not self.path.is_file()):
            raise LedgerError("ledger path must be a regular file")

    @property
    def rows(self) -> tuple[dict[str, Any], ...]:
        if not self.path.exists():
            return ()
        return verify_ledger_hash_chain(self.path)

    @property
    def last_output_sha256(self) -> str:
        rows = self.rows
        return rows[-1]["output_ledger_sha256"] if rows else GENESIS_SHA256

    def append_attempt(self, row: Mapping[str, Any]) -> DurabilityReceipt:
        payload = dict(row)
        payload.setdefault("schema", LEDGER_SCHEMA)
        payload.setdefault("record_type", "attempt")
        existing_rows = self.rows
        if any(existing.get("attempt_execution_id") == payload.get("attempt_execution_id") for existing in existing_rows):
            raise LedgerError("attempt execution id is immutable and cannot be reused")
        payload["previous_output_sha256"] = self.last_output_sha256
        # output_ledger_sha256 is a durable seal produced from this row; the
        # caller must not be able to provide a stale placeholder for it.
        for field in REQUIRED_APPEND_FIELDS:
            if field not in payload:
                raise LedgerError(f"new row omits required field {field}")
            if field in DIGEST_FIELDS:
                _digest(payload[field], field)
        payload["output_ledger_sha256"] = _output_hash(payload)
        payload["row_sha256"] = _row_hash(payload)
        payload["sha256"] = payload["row_sha256"]
        _validate_row_shape(payload, len(self.rows) + 1, self.last_output_sha256)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        with self.path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        cold_rows = verify_ledger_hash_chain(self.path)
        if not cold_rows or cold_rows[-1] != payload:
            raise LedgerError("cold-read ledger row differs after fsync")
        verified = verify_ledger_hash_chain(self.path)
        return DurabilityReceipt(
            row_sha256=payload["row_sha256"],
            output_ledger_sha256=payload["output_ledger_sha256"],
            fsynced=True,
            cold_read=True,
            hash_verified=bool(verified and verified[-1]["row_sha256"] == payload["row_sha256"]),
        )

    def next_arm_allowed(self) -> bool:
        rows = self.rows
        if not rows:
            return True
        last = rows[-1]
        return bool(
            last.get("status") == "completed"
            and last.get("completed") is True
            and last.get("durable_row") is True
            and last.get("durability") == {"fsynced": True, "cold_read": True, "hash_verified": True}
        )

    def resume_after_interruption(self, *, new_epoch: int, script1_receipt_sha256: str) -> ResumeRequest:
        rows = self.rows
        if not rows:
            raise LedgerError("no interrupted attempt to resume")
        last = rows[-1]
        if last.get("status") != "interrupted":
            raise LedgerError("last row is not an interrupted execution")
        if last.get("gp_eligible") is True:
            raise LedgerError("interrupted execution is not eligible for GP")
        prior = rows[-2] if len(rows) >= 2 else None
        if prior is not None and (prior.get("status") != "completed" or prior.get("completed") is not True):
            raise LedgerError("resume requires the last completed output seal")
        if not isinstance(new_epoch, int) or isinstance(new_epoch, bool) or new_epoch <= last["session_epoch"]:
            raise LedgerError("resume requires a strictly new session epoch")
        _digest(script1_receipt_sha256, "resume Script 1 receipt")
        if script1_receipt_sha256 == last.get("script1_receipt_sha256"):
            raise LedgerError("resume requires a new Script 1 receipt")
        return ResumeRequest(
            logical_attempt_ordinal=last["logical_attempt_ordinal"],
            new_attempt_execution_id=f"resume-{last['logical_attempt_ordinal']:02d}-{uuid.uuid4().hex}",
            requires_script1=True,
            requires_new_epoch=True,
            last_completed_output_seal=last.get("previous_output_sha256", GENESIS_SHA256),
        )


__all__ = [
    "DurabilityReceipt",
    "DurableCampaignLedger",
    "GENESIS_SHA256",
    "LEDGER_SCHEMA",
    "LedgerError",
    "REQUIRED_ROW_FIELDS",
    "ResumeRequest",
    "canonical",
    "sha256_mapping",
    "verify_ledger_hash_chain",
]
