"""Service-owned heartbeat for fresh status and TP watchdog transport."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any, Mapping

from .mailbox import AtomicMailbox
from .repository import Repository


class HeartbeatError(RuntimeError):
    """Raised when the service can no longer maintain its writer heartbeat."""


class HeartbeatPublisher:
    def __init__(
        self,
        *,
        repository: Repository,
        mailbox: AtomicMailbox,
        writer_token: str,
        deployment_id: str,
        ready_details: Mapping[str, Any],
        interval_s: float = 1.0,
    ) -> None:
        if interval_s <= 0 or interval_s > 2.0:
            raise HeartbeatError("heartbeat interval must be inside (0,2] seconds")
        self.repository = repository
        self.mailbox = mailbox
        self.writer_token = writer_token
        self.deployment_id = deployment_id
        self.ready_details = dict(ready_details)
        self.interval_s = interval_s
        self._sequence = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise HeartbeatError("heartbeat publisher is already running")
        self._publish_once()
        self._thread = threading.Thread(
            target=self._run,
            name="step5d-autotune-v2-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def _publish_once(self) -> None:
        self._sequence += 1
        observed_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        self.mailbox.publish(
            sequence=self._sequence,
            payload={
                "command": "HOST_HEARTBEAT",
                "deployment_id": self.deployment_id,
                "service_pid": os.getpid(),
                "writer_token": self.writer_token,
                "observed_at": observed_at,
            },
        )
        self.repository.heartbeat_writer(self.writer_token)
        self.repository.set_runtime_status(
            deployment_authorized=True,
            runtime_ready=True,
            primary_blocker=None,
            details={
                **self.ready_details,
                "heartbeat_sequence": self._sequence,
                "heartbeat_observed_at": observed_at,
            },
        )

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.interval_s):
                self._publish_once()
        except BaseException as exc:  # retained for the live owner to observe
            self._failure = exc
            self._stop.set()

    def check(self) -> None:
        if self._failure is not None:
            raise HeartbeatError(
                f"service heartbeat failed: {type(self._failure).__name__}:{self._failure}"
            ) from self._failure
        if self._thread is None or not self._thread.is_alive():
            raise HeartbeatError("service heartbeat is not running")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(2.0, self.interval_s * 3))
            if thread.is_alive():
                raise HeartbeatError("service heartbeat thread did not stop")
        self._thread = None
