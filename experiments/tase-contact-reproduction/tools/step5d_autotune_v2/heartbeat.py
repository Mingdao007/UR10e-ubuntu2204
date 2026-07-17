"""Service-owned heartbeat for fresh status and TP watchdog transport."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

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
        health_probe: Callable[[], Mapping[str, Any]] | None = None,
        health_failure_callback: Callable[[BaseException], None] | None = None,
    ) -> None:
        if interval_s <= 0 or interval_s > 2.0:
            raise HeartbeatError("heartbeat interval must be inside (0,2] seconds")
        self.repository = repository
        self.mailbox = mailbox
        self.writer_token = writer_token
        self.deployment_id = deployment_id
        self.ready_details = dict(ready_details)
        self.interval_s = interval_s
        self.health_probe = health_probe
        self.health_failure_callback = health_failure_callback
        self._sequence = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None
        self._failure_recorded = False
        self._publish_lock = threading.Lock()

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
        with self._publish_lock:
            if self._failure is not None:
                raise HeartbeatError("bridge health was already revoked")
            health_details: Mapping[str, Any] = {}
            if self.health_probe is not None:
                try:
                    health_details = self.health_probe()
                except BaseException as exc:
                    self._record_failure_locked(exc)
                    raise HeartbeatError(f"bridge health probe failed: {exc}") from exc
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
                    "bridge_health": dict(health_details),
                    "heartbeat_sequence": self._sequence,
                    "heartbeat_observed_at": observed_at,
                },
            )

    def _record_failure_locked(self, exc: BaseException) -> None:
        if self._failure_recorded:
            return
        self._failure_recorded = True
        self._failure = exc
        self._stop.set()
        if self.health_failure_callback is not None:
            try:
                self.health_failure_callback(exc)
            except BaseException as callback_exc:
                self._failure = callback_exc

    def fail_from_bridge(self, exc: BaseException) -> None:
        """Called by the 100 ms child watcher before any later heartbeat can publish."""

        with self._publish_lock:
            self._record_failure_locked(exc)

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.interval_s):
                self._publish_once()
        except BaseException as exc:  # retained for the live owner to observe
            with self._publish_lock:
                self._record_failure_locked(exc)

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
        if self._failure is not None:
            raise HeartbeatError(
                f"service heartbeat failed: {type(self._failure).__name__}:{self._failure}"
            ) from self._failure
