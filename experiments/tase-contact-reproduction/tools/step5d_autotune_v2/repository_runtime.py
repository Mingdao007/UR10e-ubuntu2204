"""Writer lease, runtime readiness, and transfer queue persistence."""

from __future__ import annotations

import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .model import canonical_json_bytes
from .repository_schema import RepositoryError, utc_now


WRITER_OWNER_SCHEMA = "step5d.autotune.writer-owner/v2"


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
                owner = _decode_writer_owner(row["hostname"])
                same_host = owner["hostname"] == socket.gethostname()
                lease_live = (
                    _process_matches_owner(row["pid"], owner)
                    if same_host
                    else age <= stale_after_s
                )
                if row["token"] != token and lease_live:
                    raise RepositoryError(
                        "another live writer owns the campaign: "
                        f"pid={row['pid']} host={owner['hostname']}"
                    )
            connection.execute(
                "INSERT INTO writer_lease VALUES(1,?,?,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET token=excluded.token,"
                "pid=excluded.pid,hostname=excluded.hostname,"
                "heartbeat_at=excluded.heartbeat_at",
                (
                    token,
                    os.getpid(),
                    _encode_writer_owner(os.getpid()),
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


def _boot_id() -> str | None:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip()
    except (OSError, UnicodeError):
        return None
    return value or None


def _process_start_ticks(pid: int) -> int | None:
    try:
        encoded = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii")
        closing_parenthesis = encoded.rfind(")")
        if closing_parenthesis < 0:
            return None
        fields_after_command = encoded[closing_parenthesis + 2 :].split()
        # The first value after the command is field 3 (state); starttime is 22.
        return int(fields_after_command[19])
    except (IndexError, OSError, UnicodeError, ValueError):
        return None


def _encode_writer_owner(pid: int) -> str:
    return canonical_json_bytes(
        {
            "schema": WRITER_OWNER_SCHEMA,
            "hostname": socket.gethostname(),
            "boot_id": _boot_id(),
            "process_start_ticks": _process_start_ticks(pid),
        }
    ).decode("ascii")


def _decode_writer_owner(value: str) -> dict[str, Any]:
    legacy = {
        "hostname": value,
        "boot_id": None,
        "process_start_ticks": None,
    }
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return legacy
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"schema", "hostname", "boot_id", "process_start_ticks"}
        or payload.get("schema") != WRITER_OWNER_SCHEMA
        or not isinstance(payload.get("hostname"), str)
        or not payload.get("hostname")
        or (
            payload.get("boot_id") is not None
            and not isinstance(payload.get("boot_id"), str)
        )
        or (
            payload.get("process_start_ticks") is not None
            and (
                isinstance(payload.get("process_start_ticks"), bool)
                or not isinstance(payload.get("process_start_ticks"), int)
                or payload.get("process_start_ticks") < 0
            )
        )
    ):
        return legacy
    return payload


def _process_matches_owner(pid: int, owner: Mapping[str, Any]) -> bool:
    stored_boot_id = owner.get("boot_id")
    stored_start_ticks = owner.get("process_start_ticks")
    if stored_boot_id is None or stored_start_ticks is None:
        return _pid_alive(pid)
    current_boot_id = _boot_id()
    if current_boot_id is not None and current_boot_id != stored_boot_id:
        return False
    observed_start_ticks = _process_start_ticks(pid)
    if current_boot_id is None or observed_start_ticks is None:
        return _pid_alive(pid)
    return observed_start_ticks == stored_start_ticks
