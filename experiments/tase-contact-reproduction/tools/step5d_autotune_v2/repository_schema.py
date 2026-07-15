"""SQLite schema and structural integrity contract for autotune v2."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any


DB_SCHEMA = "step5d.autotune.db/v2"
GENESIS_HASH = "0" * 64
REQUIRED_COLUMNS = {
    "deployments": {
        "deployment_id", "code_fingerprint", "tp_fingerprint", "guard_fingerprint",
        "profile_json", "deployment_authorized", "controller_readback_verified", "created_at",
    },
    "batches": {
        "batch_id", "ordinal", "source", "recovery", "completed_groups_json", "created_at",
    },
    "candidates": {
        "candidate_id", "batch_id", "group_id", "p_text", "i_text", "d_text",
        "profile_id", "coordinates_json", "comparison_key", "purpose", "replay_nonce",
        "replay_reason", "status", "physical_attempted_at", "uncertain_attempt_at", "created_at",
    },
    "trials": {
        "trial_id", "candidate_id", "deployment_id", "state", "snapshot_json",
        "arm_sequence", "ack_sequence", "created_at", "updated_at",
    },
    "events": {
        "event_id", "entity_type", "entity_id", "event_type", "payload_json",
        "previous_hash", "event_hash", "created_at",
    },
    "artifacts": {
        "artifact_id", "trial_id", "role", "path", "sha256", "immutable", "created_at",
    },
    "analyses": {
        "analysis_id", "trial_id", "metrics_json", "force_mae_n", "objective_mae_n",
        "diagnostic_eligible", "objective_eligible", "created_at",
    },
    "transfers": {
        "transfer_id", "artifact_id", "destination", "status", "attempts",
        "last_error", "updated_at",
    },
    "attempt_tombstones": {
        "comparison_key", "p_text", "i_text", "d_text", "profile_id", "disposition",
        "evidence_path", "evidence_sha256", "imported_at",
    },
}


class RepositoryError(RuntimeError):
    """Raised when durable campaign state is inconsistent or unavailable."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def initialize_schema(repository: Any) -> None:
    if repository.path.is_symlink():
        raise RepositoryError("campaign database must not be a symlink")
    with repository.transaction() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deployments (
                deployment_id TEXT PRIMARY KEY,
                code_fingerprint TEXT NOT NULL,
                tp_fingerprint TEXT NOT NULL,
                guard_fingerprint TEXT NOT NULL,
                profile_json TEXT NOT NULL,
                deployment_authorized INTEGER NOT NULL CHECK (deployment_authorized IN (0,1)),
                controller_readback_verified INTEGER NOT NULL CHECK (controller_readback_verified IN (0,1)),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY,
                ordinal INTEGER NOT NULL UNIQUE,
                source TEXT NOT NULL,
                recovery INTEGER NOT NULL CHECK (recovery IN (0,1)),
                completed_groups_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candidates (
                candidate_id TEXT PRIMARY KEY,
                batch_id TEXT REFERENCES batches(batch_id),
                group_id TEXT NOT NULL,
                p_text TEXT NOT NULL,
                i_text TEXT NOT NULL,
                d_text TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                coordinates_json TEXT NOT NULL,
                comparison_key TEXT NOT NULL,
                purpose TEXT NOT NULL CHECK (purpose IN ('search','replay')),
                replay_nonce TEXT,
                replay_reason TEXT,
                status TEXT NOT NULL,
                physical_attempted_at TEXT,
                uncertain_attempt_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS unique_search_tuple
                ON candidates(comparison_key) WHERE purpose='search';
            CREATE UNIQUE INDEX IF NOT EXISTS unique_search_group
                ON candidates(group_id) WHERE purpose='search';
            CREATE UNIQUE INDEX IF NOT EXISTS unique_replay_nonce
                ON candidates(replay_nonce) WHERE purpose='replay';
            CREATE TABLE IF NOT EXISTS trials (
                trial_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL UNIQUE REFERENCES candidates(candidate_id),
                deployment_id TEXT NOT NULL REFERENCES deployments(deployment_id),
                state TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                arm_sequence INTEGER NOT NULL UNIQUE,
                ack_sequence INTEGER UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                trial_id TEXT NOT NULL REFERENCES trials(trial_id),
                role TEXT NOT NULL,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                immutable INTEGER NOT NULL CHECK (immutable IN (0,1)),
                created_at TEXT NOT NULL,
                UNIQUE(trial_id, role)
            );
            CREATE TABLE IF NOT EXISTS analyses (
                analysis_id TEXT PRIMARY KEY,
                trial_id TEXT NOT NULL UNIQUE REFERENCES trials(trial_id),
                metrics_json TEXT NOT NULL,
                force_mae_n REAL,
                objective_mae_n REAL,
                diagnostic_eligible INTEGER NOT NULL CHECK (diagnostic_eligible IN (0,1)),
                objective_eligible INTEGER NOT NULL CHECK (objective_eligible IN (0,1)),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transfers (
                transfer_id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
                destination TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(artifact_id, destination)
            );
            CREATE TABLE IF NOT EXISTS writer_lease (
                singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                token TEXT NOT NULL,
                pid INTEGER NOT NULL,
                hostname TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runtime_status (
                singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                deployment_authorized INTEGER NOT NULL CHECK (deployment_authorized IN (0,1)),
                runtime_ready INTEGER NOT NULL CHECK (runtime_ready IN (0,1)),
                primary_blocker TEXT,
                observed_at TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attempt_tombstones (
                comparison_key TEXT PRIMARY KEY,
                p_text TEXT NOT NULL,
                i_text TEXT NOT NULL,
                d_text TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                disposition TEXT NOT NULL,
                evidence_path TEXT NOT NULL,
                evidence_sha256 TEXT NOT NULL,
                imported_at TEXT NOT NULL
            );
            """
        )
        existing = connection.execute(
            "SELECT value FROM metadata WHERE key='schema'"
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES('schema',?)", (DB_SCHEMA,)
            )
        elif existing["value"] != DB_SCHEMA:
            raise RepositoryError(f"unsupported database schema: {existing['value']}")
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key,value) VALUES('created_at',?)",
            (utc_now(),),
        )


def check_database_layout(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
    if result != "ok" or foreign:
        raise RepositoryError(
            f"database integrity failed: integrity={result!r} foreign={len(foreign)}"
        )
    for table, expected in REQUIRED_COLUMNS.items():
        actual = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if actual != expected:
            raise RepositoryError(
                f"database schema layout mismatch for {table}: "
                f"missing={sorted(expected - actual)} extra={sorted(actual - expected)}"
            )


def math_is_finite(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and number not in {float("inf"), float("-inf")}
