from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from export_step5d_autotune_v2_attempt_ledger import (  # noqa: E402
    LedgerExportError,
    build_ledger,
    canonical_bytes,
)


def make_database(path: Path, *, pending: bool = False) -> str:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE candidates (
                candidate_id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                p_text TEXT NOT NULL,
                i_text TEXT NOT NULL,
                d_text TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                status TEXT NOT NULL,
                physical_attempted_at TEXT,
                uncertain_attempt_at TEXT,
                replay_nonce TEXT,
                replay_reason TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES(?,?)",
            (
                ("schema", "step5d.autotune.db/v2"),
                ("created_at", "2026-07-15T00:00:00+00:00"),
                ("event_chain_count", "2"),
                ("event_chain_head_hash", "a" * 64),
            ),
        )
        rows = [
            (
                "c1",
                "G1",
                "0.001",
                "0.00001",
                "7",
                "nf050-slew050-a050",
                "complete",
                "2026-07-15T01:00:00+00:00",
                None,
                None,
                None,
                "2026-07-15T00:01:00+00:00",
            ),
            (
                "c2",
                "G2",
                "0.002",
                "0.001",
                "8",
                "nf050-slew050-a050",
                "uncertain_attempt",
                "2026-07-15T02:00:00+00:00",
                "2026-07-15T02:00:01+00:00",
                "nonce",
                "explicit replay",
                "2026-07-15T00:02:00+00:00",
            ),
        ]
        if pending:
            rows.append(
                (
                    "c3",
                    "G3",
                    "0.003",
                    "0.002",
                    "9",
                    "nf050-slew050-a050",
                    "pending",
                    None,
                    None,
                    None,
                    None,
                    "2026-07-15T00:03:00+00:00",
                )
            )
        connection.executemany(
            "INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_exports_terminal_attempts_and_quarantines_uncertain_tuple(
    tmp_path: Path,
) -> None:
    source = tmp_path / "control.sqlite3"
    digest = make_database(source)
    before = source.read_bytes()

    ledger = build_ledger(source, expected_sha256=digest, source_label="fixture")

    assert source.read_bytes() == before
    assert ledger["summary"] == {
        "unique_parameter_tuples": 2,
        "complete_tuples": 1,
        "uncertain_attempt_tuples": 1,
        "attempt_records": 2,
    }
    assert ledger["entries"][1]["automatic_retry_allowed"] is False
    assert ledger["entries"][1]["disposition"] == "uncertain_attempt"
    assert canonical_bytes(ledger) == canonical_bytes(ledger)


def test_rejects_source_digest_drift(tmp_path: Path) -> None:
    source = tmp_path / "control.sqlite3"
    make_database(source)
    with pytest.raises(LedgerExportError, match="sha256 differs"):
        build_ledger(source, expected_sha256="0" * 64, source_label="fixture")


def test_never_imports_pending_candidates(tmp_path: Path) -> None:
    source = tmp_path / "control.sqlite3"
    digest = make_database(source, pending=True)
    with pytest.raises(LedgerExportError, match="never imports pending work"):
        build_ledger(source, expected_sha256=digest, source_label="fixture")


def test_rejects_symlinked_source_database(tmp_path: Path) -> None:
    source = tmp_path / "control.sqlite3"
    digest = make_database(source)
    link = tmp_path / "source-link.sqlite3"
    link.symlink_to(source)
    with pytest.raises(LedgerExportError, match="real regular file"):
        build_ledger(link, expected_sha256=digest, source_label="fixture")
