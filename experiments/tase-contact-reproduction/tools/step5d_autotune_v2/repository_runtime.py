"""Writer lease, runtime readiness, and transfer queue persistence."""

from __future__ import annotations

import hashlib
import os
import socket
from datetime import datetime, timezone
from typing import Any, Mapping

from .model import canonical_json_bytes
from .repository_schema import RepositoryError, utc_now


class RepositoryRuntime:
    """Mixin for service-owned state that is not the physical reducer."""

    def queue_transfer(self, *, artifact_id: str, destination: str) -> str:
        if not destination:
            raise RepositoryError("transfer destination is required")
        transfer_id = hashlib.sha256(
            canonical_json_bytes(
                {"artifact_id": artifact_id, "destination": destination}
            )
        ).hexdigest()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM transfers WHERE transfer_id=?", (transfer_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["artifact_id"] != artifact_id
                    or existing["destination"] != destination
                ):
                    raise RepositoryError("transfer id already binds different content")
                return transfer_id
            connection.execute(
                "INSERT INTO transfers VALUES(?,?,?,?,?,?,?)",
                (transfer_id, artifact_id, destination, "pending", 0, None, utc_now()),
            )
            self._append_event(
                connection,
                entity_type="transfer",
                entity_id=transfer_id,
                event_type="transfer_queued",
                payload={"artifact_id": artifact_id, "destination": destination},
            )
        return transfer_id

    def record_transfer_attempt(
        self, transfer_id: str, *, success: bool, error: str | None = None
    ) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT 1 FROM transfers WHERE transfer_id=?", (transfer_id,)
            ).fetchone()
            if row is None:
                raise RepositoryError(f"unknown transfer: {transfer_id}")
            status = "complete" if success else "retry_pending"
            connection.execute(
                "UPDATE transfers SET status=?,attempts=attempts+1,last_error=?,"
                "updated_at=? WHERE transfer_id=?",
                (
                    status,
                    None if success else (error or "transfer_failed"),
                    utc_now(),
                    transfer_id,
                ),
            )
            self._append_event(
                connection,
                entity_type="transfer",
                entity_id=transfer_id,
                event_type="transfer_completed" if success else "transfer_retry_queued",
                payload={"error": None if success else (error or "transfer_failed")},
            )

    def set_runtime_status(
        self,
        *,
        deployment_authorized: bool,
        runtime_ready: bool,
        primary_blocker: str | None,
        details: Mapping[str, Any],
    ) -> None:
        if runtime_ready and (not deployment_authorized or primary_blocker):
            raise RepositoryError("runtime READY cannot coexist with authorization blocker")
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO runtime_status VALUES(1,?,?,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "deployment_authorized=excluded.deployment_authorized,"
                "runtime_ready=excluded.runtime_ready,"
                "primary_blocker=excluded.primary_blocker,"
                "observed_at=excluded.observed_at,details_json=excluded.details_json",
                (
                    int(deployment_authorized),
                    int(runtime_ready),
                    primary_blocker,
                    utc_now(),
                    canonical_json_bytes(dict(details)).decode("ascii"),
                ),
            )

    def claim_writer(self, *, token: str, stale_after_s: float = 15.0) -> None:
        if not token:
            raise RepositoryError("writer token is required")
        now = datetime.now(timezone.utc)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM writer_lease WHERE singleton=1"
            ).fetchone()
            if row is not None:
                try:
                    heartbeat = datetime.fromisoformat(row["heartbeat_at"])
                    age = (now - heartbeat).total_seconds()
                except ValueError:
                    age = 0.0
                same_host = row["hostname"] == socket.gethostname()
                lease_live = (
                    _pid_alive(row["pid"])
                    if same_host
                    else age <= stale_after_s
                )
                if row["token"] != token and lease_live:
                    raise RepositoryError(
                        "another live writer owns the campaign: "
                        f"pid={row['pid']} host={row['hostname']}"
                    )
            connection.execute(
                "INSERT INTO writer_lease VALUES(1,?,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET token=excluded.token,"
                "pid=excluded.pid,hostname=excluded.hostname,"
                "heartbeat_at=excluded.heartbeat_at",
                (
                    token,
                    os.getpid(),
                    socket.gethostname(),
                    now.isoformat(timespec="microseconds"),
                ),
            )

    def heartbeat_writer(self, token: str) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE writer_lease SET heartbeat_at=? WHERE singleton=1 AND token=?",
                (utc_now(), token),
            )
            if cursor.rowcount != 1:
                raise RepositoryError("writer lease was lost")

    def release_writer(self, token: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM writer_lease WHERE singleton=1 AND token=?", (token,)
            )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    return True
