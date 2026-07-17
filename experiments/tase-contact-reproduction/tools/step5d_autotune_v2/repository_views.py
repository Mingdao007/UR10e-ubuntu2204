"""Read-only campaign projections for the SQLite repository."""

from __future__ import annotations

import json
from typing import Any

from .reducer import LifecycleState
from .repository_schema import RepositoryError


class RepositoryViews:
    """Mixin containing read-only queries over durable campaign state."""

    def pending_transfers(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT t.*,a.path,a.sha256,a.role FROM transfers t "
                "JOIN artifacts a ON a.artifact_id=t.artifact_id "
                "WHERE t.status IN ('pending','retry_pending') "
                "ORDER BY t.updated_at,t.transfer_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            runtime = connection.execute(
                "SELECT * FROM runtime_status WHERE singleton=1"
            ).fetchone()
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status,COUNT(*) AS count FROM candidates GROUP BY status"
                )
            }
            pending = connection.execute(
                "SELECT group_id,p_text,i_text,d_text,purpose FROM candidates "
                "WHERE status='pending' "
                "ORDER BY CASE purpose WHEN 'replay' THEN 0 ELSE 1 END,"
                "created_at,candidate_id LIMIT 1"
            ).fetchone()
            latest_event = connection.execute(
                "SELECT event_id,event_type,event_hash,created_at FROM events "
                "ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
            active_rows = connection.execute(
                "SELECT t.trial_id,t.state,t.deployment_id,c.group_id "
                "FROM trials t JOIN candidates c ON c.candidate_id=t.candidate_id "
                "WHERE t.state NOT IN ('complete','analysis_failed','uncertain_attempt','fault') "
                "ORDER BY t.created_at,t.trial_id"
            ).fetchall()
            latest_report = connection.execute(
                "SELECT a.path,a.sha256,a.created_at,t.trial_id,c.group_id "
                "FROM artifacts a JOIN trials t ON t.trial_id=a.trial_id "
                "JOIN candidates c ON c.candidate_id=t.candidate_id "
                "WHERE a.role='trial_markdown_report' "
                "ORDER BY a.created_at DESC,a.artifact_id DESC LIMIT 1"
            ).fetchone()
            transfer_counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status,COUNT(*) AS count FROM transfers GROUP BY status"
                )
            }
        if len(active_rows) > 1:
            raise RepositoryError("multiple active trials violate the one-writer invariant")
        status: dict[str, Any] = {
            "schema": "step5d.autotune.status/v2",
            "database": str(self.path),
            "candidate_counts": counts,
            "next_candidate": dict(pending) if pending else None,
            "latest_event": dict(latest_event) if latest_event else None,
            "active_trial": dict(active_rows[0]) if active_rows else None,
            "latest_report": dict(latest_report) if latest_report else None,
            "transfer_counts": transfer_counts,
            "deployment_authorized": False,
            "runtime_ready": False,
            "primary_blocker": "runtime_not_observed",
            "observed_at": None,
            "fresh": False,
        }
        if runtime:
            status.update(
                {
                    "deployment_authorized": bool(runtime["deployment_authorized"]),
                    "runtime_ready": bool(runtime["runtime_ready"]),
                    "primary_blocker": runtime["primary_blocker"],
                    "observed_at": runtime["observed_at"],
                    "details": json.loads(runtime["details_json"]),
                }
            )
        return status

    def get_metadata(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def next_pending_candidate(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE status='pending' "
                "ORDER BY CASE purpose WHEN 'replay' THEN 0 ELSE 1 END,"
                "created_at,candidate_id LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def candidate(self, candidate_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
        if row is None:
            raise RepositoryError(f"unknown candidate: {candidate_id}")
        result = dict(row)
        result["coordinates"] = json.loads(result.pop("coordinates_json"))
        return result

    def list_candidates(self, *, batch_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM candidates"
        parameters: tuple[Any, ...] = ()
        if batch_id is not None:
            sql += " WHERE batch_id=?"
            parameters = (batch_id,)
        sql += " ORDER BY created_at,candidate_id"
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]

    def batch_detail(self, batch_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["completed_groups"] = json.loads(result.pop("completed_groups_json"))
        result["recovery"] = bool(result["recovery"])
        return result

    def latest_batch_id(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT batch_id FROM batches ORDER BY ordinal DESC LIMIT 1"
            ).fetchone()
        return row["batch_id"] if row else None

    def latest_trial_id(self, *, batch_id: str | None = None) -> str | None:
        sql = (
            "SELECT t.trial_id FROM trials t "
            "JOIN candidates c ON c.candidate_id=t.candidate_id"
        )
        parameters: tuple[Any, ...] = ()
        if batch_id is not None:
            sql += " WHERE c.batch_id=?"
            parameters = (batch_id,)
        sql += " ORDER BY t.created_at DESC,t.trial_id DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(sql, parameters).fetchone()
        return row["trial_id"] if row else None

    def active_trial_id(self) -> str | None:
        terminal = (
            LifecycleState.COMPLETE.value,
            LifecycleState.ANALYSIS_FAILED.value,
            LifecycleState.UNCERTAIN_ATTEMPT.value,
            LifecycleState.FAULT.value,
        )
        placeholders = ",".join("?" for _ in terminal)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT trial_id FROM trials WHERE state NOT IN ({placeholders}) "
                "ORDER BY created_at,trial_id",
                terminal,
            ).fetchall()
        if len(rows) > 1:
            raise RepositoryError("multiple active trials violate the one-writer invariant")
        return rows[0]["trial_id"] if rows else None

    def artifact_for_trial(
        self, trial_id: str, *, role: str | None = None
    ) -> dict[str, Any] | None:
        role_filter = ""
        parameters: tuple[Any, ...] = (trial_id,)
        if role is not None:
            role_filter = " AND role=?"
            parameters = (trial_id, role)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE trial_id=? AND immutable=1 "
                + role_filter
                + " ORDER BY created_at,artifact_id LIMIT 1",
                parameters,
            ).fetchone()
        return dict(row) if row else None

    def artifact_record_for_trial(
        self, trial_id: str, *, role: str
    ) -> dict[str, Any] | None:
        """Return an artifact binding even when a corrupt row claims mutability."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE trial_id=? AND role=?",
                (trial_id, role),
            ).fetchone()
        return dict(row) if row else None

    def completed_trials(self, *, deployment_id: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT trial_id FROM trials WHERE deployment_id=? AND state='complete' "
                "ORDER BY created_at,trial_id",
                (deployment_id,),
            ).fetchall()
        return [str(row["trial_id"]) for row in rows]

    def trial_detail(self, trial_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT t.*,c.batch_id,c.group_id,c.p_text,c.i_text,c.d_text,"
                "c.profile_id,c.purpose FROM trials t JOIN candidates c "
                "ON c.candidate_id=t.candidate_id WHERE t.trial_id=?",
                (trial_id,),
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown trial: {trial_id}")
            analysis = connection.execute(
                "SELECT * FROM analyses WHERE trial_id=?", (trial_id,)
            ).fetchone()
        result = dict(row)
        result["snapshot"] = json.loads(result.pop("snapshot_json"))
        if analysis:
            result["analysis"] = {
                **dict(analysis),
                "metrics": json.loads(analysis["metrics_json"]),
            }
        else:
            result["analysis"] = None
        return result

    def diagnostic_incumbent(
        self,
        *,
        deployment_id: str,
        profile_id: str,
        exclude_trial_id: str | None = None,
    ) -> dict[str, Any] | None:
        exclusion = ""
        parameters: tuple[Any, ...] = (deployment_id, profile_id)
        if exclude_trial_id is not None:
            exclusion = " AND t.trial_id<>?"
            parameters += (exclude_trial_id,)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT t.trial_id,t.deployment_id,c.group_id,c.p_text,c.i_text,c.d_text,"
                "c.profile_id,"
                "a.metrics_json,a.force_mae_n FROM analyses a "
                "JOIN trials t ON t.trial_id=a.trial_id "
                "JOIN candidates c ON c.candidate_id=t.candidate_id "
                "WHERE a.diagnostic_eligible=1 AND c.purpose='search' "
                "AND a.force_mae_n IS NOT NULL "
                "AND t.deployment_id=? AND c.profile_id=? "
                + exclusion
                + " ORDER BY a.force_mae_n ASC,a.created_at ASC LIMIT 1",
                parameters,
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metrics"] = json.loads(result.pop("metrics_json"))
        return result

    def objective_incumbent(
        self,
        *,
        deployment_id: str,
        profile_id: str,
        exclude_trial_id: str | None = None,
    ) -> dict[str, Any] | None:
        exclusion = ""
        parameters: tuple[Any, ...] = (deployment_id, profile_id)
        if exclude_trial_id is not None:
            exclusion = " AND t.trial_id<>?"
            parameters += (exclude_trial_id,)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT t.trial_id,t.deployment_id,c.group_id,c.p_text,c.i_text,c.d_text,"
                "c.profile_id,"
                "a.metrics_json,a.objective_mae_n FROM analyses a "
                "JOIN trials t ON t.trial_id=a.trial_id "
                "JOIN candidates c ON c.candidate_id=t.candidate_id "
                "WHERE a.objective_eligible=1 AND c.purpose='search' "
                "AND a.objective_mae_n IS NOT NULL "
                "AND t.deployment_id=? AND c.profile_id=? "
                + exclusion
                + " ORDER BY a.objective_mae_n ASC,a.created_at ASC LIMIT 1",
                parameters,
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metrics"] = json.loads(result.pop("metrics_json"))
        return result

    def historical_diagnostic_reference(
        self,
        *,
        profile_id: str,
        group_id: str | None = "G10",
        exclude_trial_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Cross-deployment evidence projection; never an optimizer incumbent."""

        filters = [
            "a.diagnostic_eligible=1",
            "c.purpose='search'",
            "a.force_mae_n IS NOT NULL",
            "c.profile_id=?",
        ]
        parameters: list[Any] = [profile_id]
        if group_id is not None:
            filters.append("c.group_id=?")
            parameters.append(group_id)
        if exclude_trial_id is not None:
            filters.append("t.trial_id<>?")
            parameters.append(exclude_trial_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT t.trial_id,t.deployment_id,c.group_id,c.p_text,c.i_text,c.d_text,"
                "c.profile_id,a.metrics_json,a.force_mae_n FROM analyses a "
                "JOIN trials t ON t.trial_id=a.trial_id "
                "JOIN candidates c ON c.candidate_id=t.candidate_id WHERE "
                + " AND ".join(filters)
                + " ORDER BY a.force_mae_n ASC,a.created_at ASC LIMIT 1",
                tuple(parameters),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metrics"] = json.loads(result.pop("metrics_json"))
        result["historical_reference"] = True
        result["optimizer_history"] = False
        return result
