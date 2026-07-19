#!/usr/bin/env python3
"""Export attempted v2 parameter tuples into a read-only v3 tombstone ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote


SCHEMA = "step5d.autotune-v3.attempt-ledger/v1"
SOURCE_SCHEMA = "step5d.autotune.db/v2"
ALLOWED_DISPOSITIONS = frozenset({"complete", "uncertain_attempt"})


class LedgerExportError(RuntimeError):
    """The v2 database cannot be represented as a fail-closed v3 ledger."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_decimal(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise LedgerExportError(f"{name} must be a non-empty decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise LedgerExportError(f"{name} is not a decimal string: {value!r}") from exc
    if not number.is_finite() or number <= 0:
        raise LedgerExportError(f"{name} must be finite and positive: {value!r}")
    return value


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    try:
        rows = connection.execute("SELECT key, value FROM metadata ORDER BY key")
    except sqlite3.DatabaseError as exc:
        raise LedgerExportError("v2 metadata table is unavailable") from exc
    result: dict[str, str] = {}
    for key, value in rows:
        if not isinstance(key, str) or not isinstance(value, str) or key in result:
            raise LedgerExportError("v2 metadata contains invalid or duplicate rows")
        result[key] = value
    if result.get("schema") != SOURCE_SCHEMA:
        raise LedgerExportError("v2 metadata schema differs from the accepted source")
    return result


def _candidate_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    connection.row_factory = sqlite3.Row
    try:
        rows = list(
            connection.execute(
                """
                SELECT candidate_id, group_id, p_text, i_text, d_text, profile_id,
                       status, physical_attempted_at, uncertain_attempt_at,
                       replay_nonce, replay_reason, created_at
                FROM candidates
                ORDER BY created_at, candidate_id
                """
            )
        )
    except sqlite3.DatabaseError as exc:
        raise LedgerExportError("v2 candidate table is unavailable or incompatible") from exc
    if not rows:
        raise LedgerExportError("v2 candidate table contains no attempted rows")
    return rows


def _group_attempts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[sqlite3.Row]] = defaultdict(list)
    group_ids: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for row in rows:
        status = row["status"]
        if status not in ALLOWED_DISPOSITIONS:
            raise LedgerExportError(
                f"candidate {row['candidate_id']} has nonterminal status {status!r}; "
                "v3 never imports pending work"
            )
        if not isinstance(row["physical_attempted_at"], str):
            raise LedgerExportError(
                f"terminal candidate {row['candidate_id']} lacks physical attempt evidence"
            )
        if status == "uncertain_attempt" and not isinstance(
            row["uncertain_attempt_at"], str
        ):
            raise LedgerExportError(
                f"uncertain candidate {row['candidate_id']} lacks quarantine timestamp"
            )
        key = (
            _strict_decimal("p_text", row["p_text"]),
            _strict_decimal("i_text", row["i_text"]),
            _strict_decimal("d_text", row["d_text"]),
            row["profile_id"],
        )
        if not isinstance(key[3], str) or not key[3]:
            raise LedgerExportError("candidate profile_id must be a non-empty string")
        if not isinstance(row["group_id"], str) or not row["group_id"]:
            raise LedgerExportError("candidate group_id must be a non-empty string")
        grouped[key].append(row)
        group_ids[key].add(row["group_id"])

    entries: list[dict[str, Any]] = []
    for key, attempts in grouped.items():
        if len(group_ids[key]) != 1:
            raise LedgerExportError("one exact parameter tuple maps to multiple group ids")
        dispositions = {row["status"] for row in attempts}
        disposition = (
            "uncertain_attempt"
            if "uncertain_attempt" in dispositions
            else "complete"
        )
        ordered_attempts = [
            {
                "candidate_id": row["candidate_id"],
                "disposition": row["status"],
                "physical_attempted_at": row["physical_attempted_at"],
                "uncertain_attempt_at": row["uncertain_attempt_at"],
                "replay_nonce": row["replay_nonce"],
                "replay_reason": row["replay_reason"],
            }
            for row in attempts
        ]
        entries.append(
            {
                "group_id": next(iter(group_ids[key])),
                "parameters": {
                    "force_p_gain": key[0],
                    "force_i_gain": key[1],
                    "force_damping": key[2],
                    "execution_profile_id": key[3],
                },
                "disposition": disposition,
                "automatic_retry_allowed": False,
                "attempts": ordered_attempts,
            }
        )

    def group_order(entry: Mapping[str, Any]) -> tuple[int, str]:
        group_id = str(entry["group_id"])
        suffix = group_id[1:] if group_id.startswith("G") else ""
        return (int(suffix) if suffix.isdigit() else 2**31 - 1, group_id)

    entries.sort(key=group_order)
    return entries


def build_ledger(
    source: Path,
    *,
    expected_sha256: str,
    source_label: str,
) -> dict[str, Any]:
    source_input = source.absolute()
    if source_input.is_symlink() or not source_input.is_file():
        raise LedgerExportError("source database must be a real regular file")
    source = source_input.resolve(strict=True)
    if (
        len(expected_sha256) != 64
        or any(char not in "0123456789abcdef" for char in expected_sha256)
    ):
        raise LedgerExportError("expected source sha256 is invalid")
    before_sha256 = sha256_file(source)
    if before_sha256 != expected_sha256:
        raise LedgerExportError(
            f"source database sha256 differs: expected {expected_sha256}, "
            f"observed {before_sha256}"
        )

    uri = f"file:{quote(str(source), safe='/')}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            metadata = _metadata(connection)
            entries = _group_attempts(_candidate_rows(connection))
    except sqlite3.DatabaseError as exc:
        raise LedgerExportError("source database could not be opened read-only") from exc

    after_sha256 = sha256_file(source)
    if after_sha256 != before_sha256:
        raise LedgerExportError("source database changed during read-only export")
    uncertain = sum(row["disposition"] == "uncertain_attempt" for row in entries)
    return {
        "schema": SCHEMA,
        "source": {
            "label": source_label,
            "sha256": before_sha256,
            "schema": metadata["schema"],
            "created_at": metadata.get("created_at"),
            "event_chain_count": int(metadata["event_chain_count"]),
            "event_chain_head_hash": metadata["event_chain_head_hash"],
        },
        "policy": {
            "pending_candidates_imported": False,
            "automatic_retry": "forbidden",
            "explicit_replay_requires": [
                "new_nonce",
                "reason",
                "current_owner_authorization",
            ],
        },
        "summary": {
            "unique_parameter_tuples": len(entries),
            "complete_tuples": len(entries) - uncertain,
            "uncertain_attempt_tuples": uncertain,
            "attempt_records": sum(len(row["attempts"]) for row in entries),
        },
        "entries": entries,
    }


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")


def write_atomic(path: Path, encoded: bytes) -> None:
    path_input = path.absolute()
    if path_input.is_symlink() or path_input.parent.is_symlink():
        raise LedgerExportError("output path and parent must not be symlinks")
    path = path_input.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--source-label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.output.resolve() == args.source.resolve():
        raise LedgerExportError("output must differ from the read-only source database")
    ledger = build_ledger(
        args.source,
        expected_sha256=args.expected_sha256,
        source_label=args.source_label,
    )
    encoded = canonical_bytes(ledger)
    if args.check:
        if not args.output.is_file() or args.output.read_bytes() != encoded:
            raise LedgerExportError("checked ledger differs from read-only v2 source")
    else:
        write_atomic(args.output, encoded)
    print(
        json.dumps(
            {
                "ok": True,
                "schema": SCHEMA,
                "source_sha256": ledger["source"]["sha256"],
                "output": str(args.output.resolve()),
                "summary": ledger["summary"],
                "check_only": args.check,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
