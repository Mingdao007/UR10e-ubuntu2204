"""Service-owned heartbeat for fresh status and TP watchdog transport."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .mailbox import AtomicMailbox
from .repository import Repository


REVOCATION_SCHEMA = "step5d.autotune.bridge-revocation/v2"


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
        failure_mailbox: AtomicMailbox | None = None,
        runtime_ready: bool = True,
        primary_blocker: str | None = None,
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
        self.failure_mailbox = failure_mailbox
        self.runtime_ready = bool(runtime_ready)
        self.primary_blocker = primary_blocker
        self._sequence = 0
        self._failure_sequence = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None
        self._callback_failure: BaseException | None = None
        self._failure_latched = threading.Event()
        self._failure_callback_registered = threading.Event()
        self._failure_lock = threading.Lock()
        self._failure_callback_thread: threading.Thread | None = None
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
            self._raise_if_failed()
            health_details: Mapping[str, Any] = {}
            if self.health_probe is not None:
                try:
                    health_details = self.health_probe()
                except BaseException as exc:
                    self._latch_failure(exc)
                    raise HeartbeatError(f"bridge health probe failed: {exc}") from exc
            self._raise_if_failed()
            self._sequence += 1
            observed_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            self._raise_if_failed()
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
            self._raise_if_failed()
            self.repository.heartbeat_writer(self.writer_token)
            self._raise_if_failed()
            self.repository.set_runtime_status(
                deployment_authorized=True,
                runtime_ready=self.runtime_ready,
                primary_blocker=self.primary_blocker,
                details={
                    **self.ready_details,
                    "bridge_health": dict(health_details),
                    "heartbeat_sequence": self._sequence,
                    "heartbeat_observed_at": observed_at,
                },
            )
            self._raise_if_failed()

    def update_runtime_status(
        self,
        *,
        runtime_ready: bool,
        primary_blocker: str | None,
        details: Mapping[str, Any],
    ) -> None:
        if runtime_ready and primary_blocker is not None:
            raise HeartbeatError("runtime READY cannot carry a blocker")
        with self._publish_lock:
            self.runtime_ready = bool(runtime_ready)
            self.primary_blocker = primary_blocker
            self.ready_details = dict(details)
        self._publish_once()

    def _raise_if_failed(self) -> None:
        if self._failure_latched.is_set():
            raise HeartbeatError("bridge health was already revoked")

    def _failure_payload(
        self,
        exc: BaseException,
        *,
        durable_revocation: str,
        durable_revocation_error: str | None,
    ) -> dict[str, Any]:
        return {
            "schema": REVOCATION_SCHEMA,
            "deployment_id": self.deployment_id,
            "bridge_pid": self.ready_details.get("bridge_pid"),
            "bridge_launch_nonce": self.ready_details.get("bridge_launch_nonce"),
            "observed_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "reason": f"{type(exc).__name__}:{exc}",
            "durable_revocation": durable_revocation,
            "durable_revocation_error": durable_revocation_error,
        }

    def _publish_failure_evidence(
        self,
        exc: BaseException,
        *,
        durable_revocation: str,
        durable_revocation_error: str | None = None,
    ) -> None:
        if self.failure_mailbox is None:
            return
        with self._failure_lock:
            self._failure_sequence += 1
            sequence = self._failure_sequence
        self.failure_mailbox.publish(
            sequence=sequence,
            payload=self._failure_payload(
                exc,
                durable_revocation=durable_revocation,
                durable_revocation_error=durable_revocation_error,
            ),
        )

    def _run_failure_callback(self, exc: BaseException) -> None:
        assert self.health_failure_callback is not None
        try:
            self.health_failure_callback(exc)
        except BaseException as callback_exc:
            with self._failure_lock:
                self._callback_failure = callback_exc
            try:
                self._publish_failure_evidence(
                    exc,
                    durable_revocation="failed",
                    durable_revocation_error=(
                        f"{type(callback_exc).__name__}:{callback_exc}"
                    ),
                )
            except BaseException:
                pass
        else:
            try:
                self._publish_failure_evidence(
                    exc,
                    durable_revocation="complete",
                )
            except BaseException:
                pass

    def _latch_failure(self, exc: BaseException) -> None:
        with self._failure_lock:
            if self._failure_latched.is_set():
                return
            self._failure = exc
            self._failure_latched.set()
            self._stop.set()
        try:
            self._publish_failure_evidence(
                exc,
                durable_revocation=(
                    "pending" if self.health_failure_callback is not None else "not_configured"
                ),
            )
        except BaseException as evidence_exc:
            with self._failure_lock:
                self._callback_failure = evidence_exc
        try:
            if self.health_failure_callback is not None:
                thread = threading.Thread(
                    target=self._run_failure_callback,
                    args=(exc,),
                    name="step5d-autotune-v2-durable-revocation",
                    daemon=True,
                )
                with self._failure_lock:
                    self._failure_callback_thread = thread
                thread.start()
        except BaseException as callback_start_exc:
            with self._failure_lock:
                self._callback_failure = callback_start_exc
        finally:
            self._failure_callback_registered.set()

    def fail_from_bridge(self, exc: BaseException) -> None:
        """Called by the 100 ms child watcher before any later heartbeat can publish."""

        self._latch_failure(exc)

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.interval_s):
                self._publish_once()
        except BaseException as exc:  # retained for the live owner to observe
            self._latch_failure(exc)

    def check(self) -> None:
        if self._failure_latched.is_set():
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
        if self._failure_latched.is_set() and not self._failure_callback_registered.wait(
            timeout=2.0
        ):
            raise HeartbeatError("durable bridge revocation was not registered")
        callback_thread = self._failure_callback_thread
        if callback_thread is not None:
            callback_thread.join(timeout=5.5)
            if callback_thread.is_alive():
                raise HeartbeatError("durable bridge revocation did not finish")
        self._failure_callback_thread = None
        if self._failure_latched.is_set():
            callback_suffix = (
                ""
                if self._callback_failure is None
                else (
                    "; durable_revocation="
                    f"{type(self._callback_failure).__name__}:{self._callback_failure}"
                )
            )
            raise HeartbeatError(
                "service heartbeat failed: "
                f"{type(self._failure).__name__}:{self._failure}{callback_suffix}"
            ) from self._failure
