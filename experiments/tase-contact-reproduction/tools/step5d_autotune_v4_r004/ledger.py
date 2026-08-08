"""Durable, append-only campaign rows and r004 ledger lineage."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


GENESIS_SHA256 = "0" * 64
LEDGER_SCHEMA = "step5d.autotune-v4/r004-durable-campaign-ledger-v1"
LEDGER_HEADER_RECORD = "ledger_header"
DURABILITY_RECORD = "durability"
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


def _reject_constant(value: str) -> None:
    raise ValueError(f"JSON constant is forbidden: {value}")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_loads(text: str, role: str) -> Any:
    try:
        return json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_pairs,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LedgerError(f"{role} is not strict JSON: {exc}") from exc


def _read_records(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise LedgerError(f"ledger path is not a regular file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise LedgerError(f"ledger read failed: {path}") from exc
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        value = _strict_loads(line, f"ledger row {line_number}")
        if not isinstance(value, dict):
            raise LedgerError(f"ledger row {line_number} is not an object")
        records.append(value)
    return records


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


def _record_hash(record: Mapping[str, Any]) -> str:
    return sha256_mapping({key: value for key, value in record.items() if key != "record_sha256"})


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
    legacy_durability = row.get("durability")
    if legacy_durability is not None:
        if not isinstance(legacy_durability, Mapping):
            raise LedgerError(f"ledger row {index} durability is not a mapping")
        if any(not isinstance(legacy_durability.get(key), bool) for key in ("fsynced", "cold_read", "hash_verified")):
            raise LedgerError(f"ledger row {index} durability is not typed")


def _validate_header(record: Mapping[str, Any], index: int) -> None:
    if record.get("schema") != LEDGER_SCHEMA or record.get("record_type") != LEDGER_HEADER_RECORD:
        raise LedgerError(f"ledger header {index} schema differs")
    if not isinstance(record.get("ledger_id"), str) or not record["ledger_id"]:
        raise LedgerError("ledger header id is invalid")
    if not isinstance(record.get("reason"), str):
        raise LedgerError("ledger header reason is invalid")
    for key in ("supersedes_file_sha256", "supersedes_output_sha256"):
        _digest(record[key], f"ledger header {key}")
    if record.get("supersedes_path") is not None and not isinstance(record["supersedes_path"], str):
        raise LedgerError("ledger header supersedes path is invalid")
    if record.get("record_sha256") != _record_hash(record):
        raise LedgerError("ledger header hash differs")


def _validate_durability(record: Mapping[str, Any], index: int) -> None:
    if record.get("schema") != LEDGER_SCHEMA or record.get("record_type") != DURABILITY_RECORD:
        raise LedgerError(f"durability record {index} schema differs")
    for key in ("attempt_row_sha256", "output_ledger_sha256"):
        _digest(record.get(key), f"durability record {index} {key}")
    for key in ("fsynced", "cold_read", "hash_verified"):
        if not isinstance(record.get(key), bool):
            raise LedgerError(f"durability record {index} {key} is not bool")
    if not isinstance(record.get("verification_pid"), int) or record["verification_pid"] <= 0:
        raise LedgerError(f"durability record {index} process id is invalid")
    if record.get("record_sha256") != _record_hash(record):
        raise LedgerError(f"durability record {index} hash differs")


def _verify_records(records: Iterable[Mapping[str, Any]]) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    raw = [dict(record) for record in records]
    previous = GENESIS_SHA256
    last_ordinal = 0
    header_seen = False
    attempts: list[dict[str, Any]] = []
    attempts_by_hash: dict[str, dict[str, Any]] = {}
    durability_by_hash: dict[str, dict[str, Any]] = {}
    execution_ids: set[str] = set()
    for index, record in enumerate(raw, start=1):
        kind = record.get("record_type")
        if kind == LEDGER_HEADER_RECORD:
            if index != 1 or header_seen or attempts:
                raise LedgerError("ledger header must be the first record")
            _validate_header(record, index)
            header_seen = True
            continue
        if kind == "attempt":
            _validate_row_shape(record, index, previous)
            ordinal = record["logical_attempt_ordinal"]
            if ordinal < last_ordinal or ordinal > last_ordinal + 1:
                raise LedgerError(f"ledger row {index} ordinal is not contiguous")
            if record["attempt_execution_id"] in execution_ids:
                raise LedgerError(f"ledger row {index} reuses an execution id")
            execution_ids.add(record["attempt_execution_id"])
            last_ordinal = ordinal
            previous = record["output_ledger_sha256"]
            attempts.append(dict(record))
            attempts_by_hash[record["row_sha256"]] = attempts[-1]
            continue
        if kind == DURABILITY_RECORD:
            _validate_durability(record, index)
            target = record["attempt_row_sha256"]
            if target not in attempts_by_hash or target in durability_by_hash:
                raise LedgerError(f"durability record {index} has no unique attempt target")
            durability_by_hash[target] = record
            continue
        raise LedgerError(f"ledger record {index} has unknown record type")
    for row in attempts:
        receipt = durability_by_hash.get(row["row_sha256"])
        if receipt is not None:
            row["durability"] = {
                "fsynced": receipt["fsynced"],
                "cold_read": receipt["cold_read"],
                "hash_verified": receipt["hash_verified"],
            }
            row["durable_row"] = all(row["durability"].values())
        elif "durability" not in row:
            row["durability"] = {
                "fsynced": False,
                "cold_read": False,
                "hash_verified": False,
            }
            row["durable_row"] = False
    return tuple(attempts), tuple(raw)


def verify_ledger_hash_chain(source: Path | Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Verify every row and receipt; callers use this before every next ARM."""

    records = _read_records(Path(source)) if isinstance(source, (str, Path)) else [dict(row) for row in source]
    attempts, _ = _verify_records(records)
    return attempts


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _append_record(path: Path, record: Mapping[str, Any]) -> bool:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or (path.exists() and path.is_symlink()):
        raise LedgerError("ledger path or parent must not be a symlink")
    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    try:
        with path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    except (OSError, TypeError, ValueError) as exc:
        raise LedgerError("ledger append/fsync failed") from exc
    return True


def _cold_read_process(path: Path, *, timeout_s: float = 30.0) -> tuple[tuple[dict[str, Any], ...], int]:
    """Read and verify the committed bytes in a fresh Python process."""

    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--cold-read", str(Path(path).resolve())],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if result.returncode != 0:
        raise LedgerError("independent ledger cold-read failed: " + result.stderr.strip())
    payload = _strict_loads(result.stdout, "independent ledger cold-read output")
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise LedgerError("independent ledger cold-read output is malformed")
    pid = payload.get("pid")
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        raise LedgerError("ledger cold-read was not performed by a fresh process")
    records = payload["records"]
    if any(not isinstance(record, dict) for record in records):
        raise LedgerError("independent ledger cold-read records are malformed")
    _verify_records(records)
    return tuple(dict(record) for record in records), pid


cold_read_ledger_subprocess = _cold_read_process


@dataclass(frozen=True)
class DurabilityReceipt:
    row_sha256: str
    output_ledger_sha256: str
    fsynced: bool
    cold_read: bool
    hash_verified: bool
    verification_pid: int | None = None

    @property
    def ready_for_next_arm(self) -> bool:
        return bool(self.fsynced and self.cold_read and self.hash_verified)

    @property
    def fresh_process_cold_read(self) -> bool:
        return self.cold_read

    @property
    def cold_read_verified(self) -> bool:
        return self.cold_read and self.hash_verified


@dataclass(frozen=True)
class ResumeRequest:
    logical_attempt_ordinal: int
    new_attempt_execution_id: str
    requires_script1: bool
    requires_new_epoch: bool
    last_completed_output_seal: str


class DurableCampaignLedger:
    """Append-only ledger with actual fsync and fresh-process cold-read proof."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if self.path.exists() and (self.path.is_symlink() or not self.path.is_file()):
            raise LedgerError("ledger path must be a regular file")

    @classmethod
    def create_new(
        cls,
        path: Path,
        *,
        supersedes: Path | None = None,
        reason: str = "",
    ) -> "DurableCampaignLedger":
        return new_ledger(path, supersedes=supersedes, reason=reason, ledger_cls=cls)

    @property
    def rows(self) -> tuple[dict[str, Any], ...]:
        if not self.path.exists():
            return ()
        return verify_ledger_hash_chain(self.path)

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        if not self.path.exists():
            return ()
        return tuple(_read_records(self.path))

    @property
    def header(self) -> dict[str, Any] | None:
        records = self.records
        return dict(records[0]) if records and records[0].get("record_type") == LEDGER_HEADER_RECORD else None

    @property
    def last_output_sha256(self) -> str:
        rows = self.rows
        return rows[-1]["output_ledger_sha256"] if rows else GENESIS_SHA256

    def append_attempt(self, row: Mapping[str, Any]) -> DurabilityReceipt:
        payload = dict(row)
        payload.pop("durability", None)
        payload.pop("durable_row", None)
        payload.setdefault("schema", LEDGER_SCHEMA)
        payload.setdefault("record_type", "attempt")
        existing_rows = self.rows
        if any(existing.get("attempt_execution_id") == payload.get("attempt_execution_id") for existing in existing_rows):
            raise LedgerError("attempt execution id is immutable and cannot be reused")
        previous = self.last_output_sha256
        payload["previous_output_sha256"] = previous
        for field in REQUIRED_APPEND_FIELDS:
            if field not in payload:
                raise LedgerError(f"new row omits required field {field}")
            if field in DIGEST_FIELDS:
                _digest(payload[field], field)
        payload["output_ledger_sha256"] = _output_hash(payload)
        payload["row_sha256"] = _row_hash(payload)
        payload["sha256"] = payload["row_sha256"]
        _validate_row_shape(payload, len(existing_rows) + 1, previous)

        fsynced = _append_record(self.path, payload)
        cold_records, verification_pid = _cold_read_process(self.path)
        cold_rows, _ = _verify_records(cold_records)
        cold_match = bool(cold_rows and cold_rows[-1].get("row_sha256") == payload["row_sha256"])
        hash_verified = bool(cold_match and cold_rows[-1].get("output_ledger_sha256") == payload["output_ledger_sha256"])
        cold_read = bool(cold_match)
        if not (cold_read and hash_verified):
            raise LedgerError("fresh-process ledger cold-read differs after fsync")

        receipt_record: dict[str, Any] = {
            "schema": LEDGER_SCHEMA,
            "record_type": DURABILITY_RECORD,
            "attempt_row_sha256": payload["row_sha256"],
            "output_ledger_sha256": payload["output_ledger_sha256"],
            "fsynced": fsynced,
            "cold_read": cold_read,
            "hash_verified": hash_verified,
            "verification_pid": verification_pid,
        }
        receipt_record["record_sha256"] = _record_hash(receipt_record)
        _append_record(self.path, receipt_record)
        final_records, final_pid = _cold_read_process(self.path)
        final_rows, _ = _verify_records(final_records)
        if not any(row.get("row_sha256") == payload["row_sha256"] for row in final_rows):
            raise LedgerError("durability receipt cold-read lost the attempt row")
        return DurabilityReceipt(
            row_sha256=payload["row_sha256"],
            output_ledger_sha256=payload["output_ledger_sha256"],
            fsynced=fsynced,
            cold_read=cold_read,
            hash_verified=hash_verified,
            verification_pid=final_pid or verification_pid,
        )

    def next_arm_allowed(self) -> bool:
        rows = self.rows
        if not rows:
            return True
        last = rows[-1]
        durability = last.get("durability")
        return bool(
            last.get("status") == "completed"
            and last.get("completed") is True
            and last.get("durable_row") is True
            and isinstance(durability, Mapping)
            and durability.get("fsynced") is True
            and durability.get("cold_read") is True
            and durability.get("hash_verified") is True
        )

    def supersede(self, new_path: Path, *, reason: str) -> "DurableCampaignLedger":
        return new_ledger(new_path, supersedes=self.path, reason=reason, ledger_cls=type(self))

    def resume_after_interruption(self, *, new_epoch: int, script1_receipt_sha256: str) -> ResumeRequest:
        rows = self.rows
        if not rows:
            raise LedgerError("no interrupted attempt to resume")
        last = rows[-1]
        if last.get("status") != "interrupted":
            raise LedgerError("last row is not an interrupted execution")
        if last.get("gp_eligible") is True:
            raise LedgerError("interrupted execution is not eligible for GP")
        prior = next(
            (
                row
                for row in reversed(rows[:-1])
                if row.get("status") == "completed" and row.get("completed") is True
            ),
            None,
        )
        if prior is None:
            raise LedgerError("resume requires a prior completed output seal")
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
            last_completed_output_seal=prior.get("output_ledger_sha256", GENESIS_SHA256),
        )


def new_ledger(
    path: Path,
    *,
    supersedes: Path | None = None,
    reason: str = "",
    ledger_cls: type[DurableCampaignLedger] = DurableCampaignLedger,
) -> DurableCampaignLedger:
    """Create a separate ledger and leave every prior ledger byte untouched."""

    destination = Path(path)
    if destination.exists():
        raise LedgerError(f"new ledger path already exists: {destination}")
    if not isinstance(reason, str) or (supersedes is not None and not reason.strip()):
        raise LedgerError("supersession reason is required")
    old_file_sha = GENESIS_SHA256
    old_output_sha = GENESIS_SHA256
    old_path_text: str | None = None
    if supersedes is not None:
        old = ledger_cls(Path(supersedes))
        old_records = old.records
        old_file_sha = hashlib.sha256(old.path.read_bytes()).hexdigest()
        old_output_sha = old.last_output_sha256
        old_path_text = str(old.path)
        _verify_records(old_records)
    header: dict[str, Any] = {
        "schema": LEDGER_SCHEMA,
        "record_type": LEDGER_HEADER_RECORD,
        "ledger_id": f"r004-ledger-{uuid.uuid4().hex}",
        "supersedes_path": old_path_text,
        "supersedes_file_sha256": old_file_sha,
        "supersedes_output_sha256": old_output_sha,
        "reason": reason,
    }
    header["record_sha256"] = _record_hash(header)
    _validate_header(header, 1)
    _append_record(destination, header)
    _cold_read_process(destination)
    return ledger_cls(destination)


def _main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "--cold-read":
        return 2
    path = Path(args[1])
    records = _read_records(path)
    _verify_records(records)
    sys.stdout.write(
        json.dumps(
            {"pid": os.getpid(), "records": records},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = [
    "DURABILITY_RECORD",
    "DurabilityReceipt",
    "DurableCampaignLedger",
    "GENESIS_SHA256",
    "LEDGER_HEADER_RECORD",
    "LEDGER_SCHEMA",
    "LedgerError",
    "REQUIRED_ROW_FIELDS",
    "ResumeRequest",
    "canonical",
    "cold_read_ledger_subprocess",
    "new_ledger",
    "sha256_mapping",
    "verify_ledger_hash_chain",
]
