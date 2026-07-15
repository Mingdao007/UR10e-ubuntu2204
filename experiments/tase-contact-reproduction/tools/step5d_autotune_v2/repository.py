"""SQLite single source of truth for Step5d autotune v2."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .model import (
    BatchSpec,
    AttemptTupleSpec,
    CandidateSpec,
    DeploymentSpec,
    canonical_json_bytes,
)
from .reducer import (
    LifecycleEvent,
    LifecycleSnapshot,
    LifecycleState,
    reduce_lifecycle,
)
from .repository_schema import (
    GENESIS_HASH,
    RepositoryError,
    check_database_layout,
    initialize_schema,
    math_is_finite,
    utc_now,
)
from .repository_runtime import RepositoryRuntime
from .repository_views import RepositoryViews


class Repository(RepositoryRuntime, RepositoryViews):
    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise RepositoryError("database path must be absolute")
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        initialize_schema(self)
        self.integrity_check()

    def integrity_check(self) -> None:
        with self._connect() as connection:
            check_database_layout(connection)
            self._verify_event_chain(connection)

    @staticmethod
    def _verify_event_chain(connection: sqlite3.Connection) -> None:
        previous = GENESIS_HASH
        rows = connection.execute(
            "SELECT entity_type,entity_id,event_type,payload_json,previous_hash,event_hash,created_at "
            "FROM events ORDER BY event_id"
        )
        for row in rows:
            if row["previous_hash"] != previous:
                raise RepositoryError("event chain previous hash mismatch")
            material = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "previous_hash": row["previous_hash"],
                "created_at": row["created_at"],
            }
            actual = hashlib.sha256(canonical_json_bytes(material)).hexdigest()
            if actual != row["event_hash"]:
                raise RepositoryError("event chain content hash mismatch")
            previous = actual

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> str:
        row = connection.execute(
            "SELECT event_hash FROM events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous = row["event_hash"] if row else GENESIS_HASH
        created_at = utc_now()
        material = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "payload": dict(payload),
            "previous_hash": previous,
            "created_at": created_at,
        }
        event_hash = hashlib.sha256(canonical_json_bytes(material)).hexdigest()
        connection.execute(
            "INSERT INTO events(entity_type,entity_id,event_type,payload_json,previous_hash,event_hash,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                canonical_json_bytes(dict(payload)).decode("ascii"),
                previous,
                event_hash,
                created_at,
            ),
        )
        return event_hash

    @staticmethod
    def _reserve_sequence(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='command_sequence'"
        ).fetchone()
        sequence = int(row["value"] if row else "0") + 1
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES('command_sequence',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(sequence),),
        )
        return sequence

    def register_deployment(self, deployment: DeploymentSpec) -> None:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM deployments WHERE deployment_id=?",
                (deployment.deployment_id,),
            ).fetchone()
            payload = (
                deployment.code_fingerprint,
                deployment.tp_fingerprint,
                deployment.guard_fingerprint,
                canonical_json_bytes(dict(deployment.profile)).decode("ascii"),
                int(deployment.deployment_authorized),
                int(deployment.controller_readback_verified),
            )
            if existing is not None:
                actual = tuple(
                    existing[name]
                    for name in (
                        "code_fingerprint",
                        "tp_fingerprint",
                        "guard_fingerprint",
                        "profile_json",
                        "deployment_authorized",
                        "controller_readback_verified",
                    )
                )
                if actual != payload:
                    raise RepositoryError("deployment id already binds different immutable content")
                return
            connection.execute(
                "INSERT INTO deployments VALUES(?,?,?,?,?,?,?,?)",
                (deployment.deployment_id, *payload, utc_now()),
            )
            self._append_event(
                connection,
                entity_type="deployment",
                entity_id=deployment.deployment_id,
                event_type="deployment_registered",
                payload={
                    "code_fingerprint": deployment.code_fingerprint,
                    "tp_fingerprint": deployment.tp_fingerprint,
                    "guard_fingerprint": deployment.guard_fingerprint,
                    "deployment_authorized": deployment.deployment_authorized,
                    "controller_readback_verified": deployment.controller_readback_verified,
                },
            )

    def enqueue_batch(self, batch: BatchSpec) -> None:
        """Append parameters only; this transaction never mutates a deployment."""

        try:
            with self.transaction() as connection:
                if connection.execute(
                    "SELECT 1 FROM batches WHERE batch_id=?", (batch.batch_id,)
                ).fetchone():
                    raise RepositoryError(f"batch already exists: {batch.batch_id}")
                if batch.recovery:
                    completed = connection.execute(
                        "SELECT status FROM candidates "
                        "WHERE group_id='G11' AND purpose='search'"
                    ).fetchone()
                    if completed is None or completed["status"] != "complete":
                        raise RepositoryError(
                            "four-candidate recovery requires durable completed G11"
                        )
                ordinal = connection.execute(
                    "SELECT COALESCE(MAX(ordinal),0)+1 FROM batches"
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO batches VALUES(?,?,?,?,?,?)",
                    (
                        batch.batch_id,
                        ordinal,
                        batch.source,
                        int(batch.recovery),
                        canonical_json_bytes(list(batch.completed_groups)).decode("ascii"),
                        utc_now(),
                    ),
                )
                for candidate in batch.candidates:
                    if connection.execute(
                        "SELECT 1 FROM attempt_tombstones WHERE comparison_key=?",
                        (candidate.comparison_key,),
                    ).fetchone():
                        raise RepositoryError(
                            f"candidate {candidate.group_id} matches an imported physical-attempt tombstone"
                        )
                    self._insert_candidate(connection, candidate, batch.batch_id)
                self._append_event(
                    connection,
                    entity_type="batch",
                    entity_id=batch.batch_id,
                    event_type="batch_enqueued",
                    payload={
                        "candidate_ids": [row.candidate_id for row in batch.candidates],
                        "groups": [row.group_id for row in batch.candidates],
                        "recovery": batch.recovery,
                        "completed_groups": list(batch.completed_groups),
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise RepositoryError(f"batch violates append-only uniqueness: {exc}") from exc

    @staticmethod
    def _insert_candidate(
        connection: sqlite3.Connection,
        candidate: CandidateSpec,
        batch_id: str | None,
    ) -> None:
        coordinates = {
            "log2_p": candidate.log2_p,
            "log2_i": candidate.log2_i,
            "log2_d": candidate.log2_d,
            "i_multiplier": candidate.i_multiplier,
        }
        connection.execute(
            "INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                candidate.candidate_id,
                batch_id,
                candidate.group_id,
                candidate.p,
                candidate.i,
                candidate.d,
                candidate.profile_id,
                canonical_json_bytes(coordinates).decode("ascii"),
                candidate.comparison_key,
                candidate.purpose,
                candidate.replay_nonce,
                candidate.replay_reason,
                "pending",
                None,
                None,
                utc_now(),
            ),
        )

    def create_replay(self, *, group_id: str, reason: str, nonce: str) -> CandidateSpec:
        with self.transaction() as connection:
            source = connection.execute(
                "SELECT * FROM candidates WHERE group_id=? AND purpose='search'",
                (group_id,),
            ).fetchone()
            if source is None:
                raise RepositoryError(f"unknown search group: {group_id}")
            coordinates = json.loads(source["coordinates_json"])
            replay = CandidateSpec(
                group_id=group_id,
                p=source["p_text"],
                i=source["i_text"],
                d=source["d_text"],
                profile_id=source["profile_id"],
                log2_p=coordinates.get("log2_p"),
                log2_i=coordinates.get("log2_i"),
                log2_d=coordinates.get("log2_d"),
                i_multiplier=coordinates.get("i_multiplier"),
                purpose="replay",
                replay_nonce=nonce,
                replay_reason=reason,
            )
            try:
                self._insert_candidate(connection, replay, None)
            except sqlite3.IntegrityError as exc:
                raise RepositoryError(f"replay nonce or identity already exists: {exc}") from exc
            self._append_event(
                connection,
                entity_type="candidate",
                entity_id=replay.candidate_id,
                event_type="replay_created",
                payload={"source_group": group_id, "reason": reason, "nonce": nonce},
            )
            return replay

    def create_trial(
        self,
        *,
        candidate_id: str,
        deployment_id: str,
        trial_id: str | None = None,
    ) -> str:
        trial_id = trial_id or hashlib.sha256(
            f"{candidate_id}:{deployment_id}:{secrets.token_hex(16)}".encode("ascii")
        ).hexdigest()
        snapshot = LifecycleSnapshot()
        try:
            with self.transaction() as connection:
                candidate = connection.execute(
                    "SELECT status FROM candidates WHERE candidate_id=?", (candidate_id,)
                ).fetchone()
                if candidate is None or candidate["status"] != "pending":
                    raise RepositoryError("candidate is missing or no longer pending")
                if connection.execute(
                    "SELECT 1 FROM deployments WHERE deployment_id=?", (deployment_id,)
                ).fetchone() is None:
                    raise RepositoryError("deployment is not registered")
                now = utc_now()
                arm_sequence = self._reserve_sequence(connection)
                connection.execute(
                    "INSERT INTO trials VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        trial_id,
                        candidate_id,
                        deployment_id,
                        snapshot.state.value,
                        canonical_json_bytes(snapshot.as_dict()).decode("ascii"),
                        arm_sequence,
                        None,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE candidates SET status='active' WHERE candidate_id=?",
                    (candidate_id,),
                )
                self._append_event(
                    connection,
                    entity_type="trial",
                    entity_id=trial_id,
                    event_type="trial_created",
                    payload={"candidate_id": candidate_id, "deployment_id": deployment_id},
                )
        except sqlite3.IntegrityError as exc:
            raise RepositoryError(f"candidate already has a physical trial: {exc}") from exc
        return trial_id

    def reserve_ack_sequence(self, trial_id: str) -> int:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT state,ack_sequence FROM trials WHERE trial_id=?", (trial_id,)
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown trial: {trial_id}")
            if row["ack_sequence"] is not None:
                return int(row["ack_sequence"])
            if row["state"] != LifecycleState.RAW_SEALED.value:
                raise RepositoryError("ACK sequence can be reserved only after raw seal")
            sequence = self._reserve_sequence(connection)
            connection.execute(
                "UPDATE trials SET ack_sequence=?,updated_at=? WHERE trial_id=?",
                (sequence, utc_now(), trial_id),
            )
            self._append_event(
                connection,
                entity_type="trial",
                entity_id=trial_id,
                event_type="ack_sequence_reserved",
                payload={"ack_sequence": sequence},
            )
            return sequence

    def apply_lifecycle_event(
        self,
        trial_id: str,
        event: LifecycleEvent,
        payload: Mapping[str, Any] | None = None,
    ) -> LifecycleSnapshot:
        data = dict(payload or {})
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT candidate_id,snapshot_json FROM trials WHERE trial_id=?", (trial_id,)
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown trial: {trial_id}")
            previous = LifecycleSnapshot.from_mapping(json.loads(row["snapshot_json"]))
            current = reduce_lifecycle(previous, event, data)
            connection.execute(
                "UPDATE trials SET state=?,snapshot_json=?,updated_at=? WHERE trial_id=?",
                (
                    current.state.value,
                    canonical_json_bytes(current.as_dict()).decode("ascii"),
                    utc_now(),
                    trial_id,
                ),
            )
            candidate_status = None
            if not previous.attempted and current.attempted:
                connection.execute(
                    "UPDATE candidates SET physical_attempted_at=COALESCE(physical_attempted_at,?) "
                    "WHERE candidate_id=?",
                    (utc_now(), row["candidate_id"]),
                )
            if current.uncertain_attempt:
                candidate_status = "uncertain_attempt"
                connection.execute(
                    "UPDATE candidates SET uncertain_attempt_at=COALESCE(uncertain_attempt_at,?) "
                    "WHERE candidate_id=?",
                    (utc_now(), row["candidate_id"]),
                )
            elif current.state is LifecycleState.COMPLETE:
                candidate_status = "complete"
            elif current.state in {LifecycleState.FAULT, LifecycleState.ANALYSIS_FAILED}:
                candidate_status = current.state.value
            elif current.physical_closed:
                candidate_status = "physical_closed"
            if candidate_status:
                connection.execute(
                    "UPDATE candidates SET status=? WHERE candidate_id=?",
                    (candidate_status, row["candidate_id"]),
                )
            self._append_event(
                connection,
                entity_type="trial",
                entity_id=trial_id,
                event_type=event.value,
                payload={"input": data, "snapshot": current.as_dict()},
            )
            return current

    def add_artifact(
        self,
        *,
        trial_id: str,
        role: str,
        path: Path,
        sha256: str,
        immutable: bool,
    ) -> str:
        artifact_id = hashlib.sha256(
            canonical_json_bytes(
                {"trial_id": trial_id, "role": role, "path": str(path), "sha256": sha256}
            )
        ).hexdigest()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM artifacts WHERE trial_id=? AND role=?", (trial_id, role)
            ).fetchone()
            if existing is not None:
                if (
                    existing["artifact_id"] != artifact_id
                    or existing["path"] != str(path)
                    or existing["sha256"] != sha256
                    or bool(existing["immutable"]) != immutable
                ):
                    raise RepositoryError("artifact role already binds different immutable content")
                return artifact_id
            connection.execute(
                "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
                (artifact_id, trial_id, role, str(path), sha256, int(immutable), utc_now()),
            )
            self._append_event(
                connection,
                entity_type="artifact",
                entity_id=artifact_id,
                event_type="artifact_registered",
                payload={"trial_id": trial_id, "role": role, "sha256": sha256, "immutable": immutable},
            )
        return artifact_id

    def record_analysis(
        self,
        *,
        trial_id: str,
        metrics: Mapping[str, Any],
        diagnostic_eligible: bool,
        objective_eligible: bool,
    ) -> str:
        analysis_id = hashlib.sha256(
            canonical_json_bytes({"trial_id": trial_id, "metrics": dict(metrics)})
        ).hexdigest()
        mae = metrics.get("force_mae_n")
        if mae is not None and (isinstance(mae, bool) or not math_is_finite(mae)):
            raise RepositoryError("force_mae_n must be finite")
        objective_mae = metrics.get("objective_mae_n")
        if objective_mae is not None and (
            isinstance(objective_mae, bool) or not math_is_finite(objective_mae)
        ):
            raise RepositoryError("objective_mae_n must be finite")
        if diagnostic_eligible and mae is None:
            raise RepositoryError("diagnostic eligibility requires force_mae_n")
        if objective_eligible and objective_mae is None:
            raise RepositoryError("objective eligibility requires objective_mae_n")
        with self.transaction() as connection:
            encoded_metrics = canonical_json_bytes(dict(metrics)).decode("ascii")
            existing = connection.execute(
                "SELECT * FROM analyses WHERE trial_id=?", (trial_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["analysis_id"] != analysis_id
                    or existing["metrics_json"] != encoded_metrics
                    or bool(existing["diagnostic_eligible"]) != diagnostic_eligible
                    or bool(existing["objective_eligible"]) != objective_eligible
                ):
                    raise RepositoryError("trial analysis already binds different content")
                return analysis_id
            connection.execute(
                "INSERT INTO analyses VALUES(?,?,?,?,?,?,?,?)",
                (
                    analysis_id,
                    trial_id,
                    encoded_metrics,
                    None if mae is None else float(mae),
                    None if objective_mae is None else float(objective_mae),
                    int(diagnostic_eligible),
                    int(objective_eligible),
                    utc_now(),
                ),
            )
            self._append_event(
                connection,
                entity_type="analysis",
                entity_id=analysis_id,
                event_type="analysis_recorded",
                payload={
                    "trial_id": trial_id,
                    "diagnostic_eligible": diagnostic_eligible,
                    "objective_eligible": objective_eligible,
                },
            )
        return analysis_id

    def set_metadata(self, key: str, value: str) -> None:
        if not key or not isinstance(value, str):
            raise RepositoryError("metadata key/value must be strings")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def add_attempt_tombstone(
        self,
        *,
        candidate: CandidateSpec | AttemptTupleSpec,
        disposition: str,
        evidence_path: Path,
        evidence_sha256: str,
    ) -> bool:
        if getattr(candidate, "purpose", "search") != "search":
            raise RepositoryError("attempt tombstones bind physical search tuples only")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM attempt_tombstones WHERE comparison_key=?",
                (candidate.comparison_key,),
            ).fetchone()
            expected = (
                candidate.p,
                candidate.i,
                candidate.d,
                candidate.profile_id,
                disposition,
                str(evidence_path),
                evidence_sha256,
            )
            if existing is not None:
                actual = tuple(
                    existing[name]
                    for name in (
                        "p_text",
                        "i_text",
                        "d_text",
                        "profile_id",
                        "disposition",
                        "evidence_path",
                        "evidence_sha256",
                    )
                )
                if actual != expected:
                    raise RepositoryError(
                        "attempt tombstone already binds different physical evidence"
                    )
                return True
            mapped = connection.execute(
                "SELECT candidate_id,status FROM candidates "
                "WHERE comparison_key=? AND purpose='search'",
                (candidate.comparison_key,),
            ).fetchone()
            if mapped is not None and mapped["status"] != "pending":
                return False
            connection.execute(
                "INSERT INTO attempt_tombstones VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    candidate.comparison_key,
                    *expected,
                    utc_now(),
                ),
            )
            if mapped is not None:
                now = utc_now()
                connection.execute(
                    "UPDATE candidates SET status='uncertain_attempt',"
                    "physical_attempted_at=COALESCE(physical_attempted_at,?),"
                    "uncertain_attempt_at=COALESCE(uncertain_attempt_at,?) "
                    "WHERE candidate_id=?",
                    (now, now, mapped["candidate_id"]),
                )
            self._append_event(
                connection,
                entity_type="attempt_tombstone",
                entity_id=candidate.comparison_key,
                event_type="legacy_attempt_imported",
                payload={
                    "disposition": disposition,
                    "evidence_path": str(evidence_path),
                    "evidence_sha256": evidence_sha256,
                    "mapped_candidate_id": None
                    if mapped is None
                    else mapped["candidate_id"],
                },
            )
            return True
