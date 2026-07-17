"""Fail-closed bridge child process ownership for the systemd service."""

from __future__ import annotations

import json
import os
import secrets
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


READY_SCHEMA = "step5d.autotune.bridge-ready/v2"
HEALTH_SCHEMA = "step5d.autotune.bridge-health/v2"
MAX_READY_BYTES = 64 * 1024


class BridgeError(RuntimeError):
    """Raised when the frozen bridge command cannot prove readiness."""


@dataclass(frozen=True)
class BridgeReady:
    pid: int
    sample_rate_hz: int
    deployment_id: str
    launch_nonce: str
    startup_home_verified: bool
    evidence_path: Path
    health_evidence_path: Path


@dataclass(frozen=True)
class BridgeHealth:
    pid: int
    deployment_id: str
    launch_nonce: str
    health_sequence: int
    observed_at: str
    configured_rate_hz: int
    sample_counter: int
    rtde_healthy: bool
    command_transport_healthy: bool
    event_transport_healthy: bool
    evidence_path: Path


class BridgeProcess:
    def __init__(
        self,
        *,
        argv: Sequence[str],
        root: Path,
        runtime_root: Path,
        deployment_id: str,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not argv or any(not isinstance(value, str) or not value for value in argv):
            raise BridgeError("bridge argv must be a non-empty token array")
        self.argv = tuple(argv)
        self.root = root.resolve()
        self.runtime_root = runtime_root.resolve()
        self.deployment_id = deployment_id
        self.environment = dict(environment or {})
        self.process: subprocess.Popen[str] | None = None
        self.log_handle: Any | None = None
        self._launch_nonce: str | None = None
        self._health_sequence: int | None = None
        self._sample_counter: int | None = None
        self._observed_health_sequence: int | None = None
        self._observed_sample_counter: int | None = None
        self._health_lock = threading.Lock()
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._watch_failure: BaseException | None = None

    def start(self, *, timeout_s: float = 15.0) -> BridgeReady:
        if self.process is not None:
            raise BridgeError("bridge child has already been started")
        if self.runtime_root.is_symlink():
            raise BridgeError("bridge runtime root must not be a symlink")
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        if not self.runtime_root.is_dir():
            raise BridgeError("bridge runtime root is not a directory")
        ready_path = self.runtime_root / "bridge_ready.json"
        health_path = self.runtime_root / "bridge_health.json"
        for snapshot_path, label in (
            (ready_path, "readiness"),
            (health_path, "health"),
        ):
            _remove_stale_snapshot(
                snapshot_path, runtime_root=self.runtime_root, label=label
            )
        log_path = self.runtime_root / "bridge.log"
        self.log_handle = log_path.open("a", encoding="utf-8")
        environment = dict(os.environ)
        environment.update(self.environment)
        environment["STEP5D_AUTOTUNE_V2_DEPLOYMENT_ID"] = self.deployment_id
        environment["STEP5D_AUTOTUNE_V2_RUNTIME_ROOT"] = str(self.runtime_root)
        launch_nonce = secrets.token_hex(32)
        self._launch_nonce = launch_nonce
        environment["STEP5D_AUTOTUNE_V2_LAUNCH_NONCE"] = launch_nonce
        self.process = subprocess.Popen(
            self.argv,
            cwd=self.root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=False,
        )
        deadline = time.monotonic() + timeout_s
        startup_health: BridgeHealth | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise BridgeError(
                    f"bridge exited before readiness: rc={self.process.returncode} log={log_path}"
                )
            if ready_path.exists() or ready_path.is_symlink():
                payload = _read_ready_snapshot(ready_path)
                if payload is None:
                    time.sleep(0.05)
                    continue
                if (
                    payload.get("schema") == READY_SCHEMA
                    and payload.get("deployment_id") == self.deployment_id
                    and payload.get("sample_rate_hz") == 500
                    and payload.get("bridge_ready") is True
                    and payload.get("pid") == self.process.pid
                    and payload.get("launch_nonce") == launch_nonce
                    and payload.get("startup_home_verified") is True
                    and payload.get("command_transport_ready") is True
                    and payload.get("event_transport_ready") is True
                ):
                    try:
                        health = self.check_health(
                            max_age_s=2.0, require_progress=False
                        )
                    except BridgeError as exc:
                        if "has not been published" in str(exc):
                            time.sleep(0.05)
                            continue
                        raise
                    if startup_health is None:
                        startup_health = health
                        time.sleep(0.002)
                        continue
                    if (
                        health.health_sequence <= startup_health.health_sequence
                        or health.sample_counter <= startup_health.sample_counter
                    ):
                        time.sleep(0.002)
                        continue
                    with self._health_lock:
                        self._health_sequence = health.health_sequence
                        self._sample_counter = health.sample_counter
                    return BridgeReady(
                        pid=self.process.pid,
                        sample_rate_hz=500,
                        deployment_id=self.deployment_id,
                        launch_nonce=launch_nonce,
                        startup_home_verified=True,
                        evidence_path=ready_path,
                        health_evidence_path=health_path,
                    )
                raise BridgeError(
                    "bridge readiness evidence lacks exact launch/rate/Home/transport proof"
                )
            time.sleep(0.05)
        raise BridgeError(f"bridge did not publish stable 500 Hz readiness: log={log_path}")

    def check_health(
        self, *, max_age_s: float = 2.0, require_progress: bool = True
    ) -> BridgeHealth:
        """Bind runtime health to the exact live child and progressing RTDE loop."""

        if max_age_s <= 0:
            raise BridgeError("bridge health freshness must be positive")
        with self._health_lock:
            process = self.process
            launch_nonce = self._launch_nonce
            if process is None or launch_nonce is None:
                raise BridgeError("bridge child is not running")
            if process.poll() is not None:
                raise BridgeError(f"bridge child exited: rc={process.returncode}")
            health_path = self.runtime_root / "bridge_health.json"
            payload = _read_health_snapshot(health_path)
            if payload is None:
                raise BridgeError("bridge health has not been published")
            if (
                payload.get("schema") != HEALTH_SCHEMA
                or payload.get("deployment_id") != self.deployment_id
                or payload.get("pid") != process.pid
                or payload.get("launch_nonce") != launch_nonce
                or payload.get("configured_rate_hz") != 500
                or payload.get("rtde_healthy") is not True
                or payload.get("command_transport_healthy") is not True
                or payload.get("event_transport_healthy") is not True
            ):
                raise BridgeError(
                    "bridge health lacks exact child/rate/transport identity"
                )
            sequence = payload.get("health_sequence")
            sample_counter = payload.get("sample_counter")
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence < 0
                or isinstance(sample_counter, bool)
                or not isinstance(sample_counter, int)
                or sample_counter < 0
            ):
                raise BridgeError("bridge health counters are invalid")
            try:
                observed = datetime.fromisoformat(str(payload.get("observed_at", "")))
            except ValueError as exc:
                raise BridgeError("bridge health timestamp is invalid") from exc
            if observed.tzinfo is None:
                raise BridgeError("bridge health timestamp must include a timezone")
            age_s = (datetime.now(timezone.utc) - observed).total_seconds()
            if age_s < -1.0 or age_s > max_age_s:
                raise BridgeError(f"bridge health is stale: age_s={age_s:.6f}")
            if self._observed_health_sequence is not None:
                if (
                    sequence < self._observed_health_sequence
                    or sample_counter < self._observed_sample_counter
                ):
                    raise BridgeError("bridge health counters regressed")
            if self._health_sequence is not None:
                if require_progress and (
                    sequence == self._health_sequence
                    or sample_counter == self._sample_counter
                ):
                    raise BridgeError("bridge health counters did not progress")
            self._observed_health_sequence = sequence
            self._observed_sample_counter = sample_counter
            if require_progress:
                self._health_sequence = sequence
                self._sample_counter = sample_counter
            return BridgeHealth(
                pid=process.pid,
                deployment_id=self.deployment_id,
                launch_nonce=launch_nonce,
                health_sequence=sequence,
                observed_at=observed.isoformat(timespec="microseconds"),
                configured_rate_hz=500,
                sample_counter=sample_counter,
                rtde_healthy=True,
                command_transport_healthy=True,
                event_transport_healthy=True,
                evidence_path=health_path,
            )

    def start_watcher(
        self,
        on_failure: Callable[[BaseException], None],
        *,
        interval_s: float = 0.1,
    ) -> None:
        if interval_s <= 0 or interval_s > 0.1:
            raise BridgeError("bridge watcher interval must be inside (0,0.1] seconds")
        if self.process is None or self.process.poll() is not None:
            raise BridgeError("bridge watcher requires a live child")
        if self._watch_thread is not None:
            raise BridgeError("bridge watcher is already running")
        self._watch_stop.clear()
        self._watch_failure = None

        def watch() -> None:
            while not self._watch_stop.wait(interval_s):
                try:
                    self.check_health(max_age_s=2.0, require_progress=False)
                except BaseException as exc:
                    self._watch_failure = exc
                    self._watch_stop.set()
                    try:
                        on_failure(exc)
                    except BaseException as callback_exc:
                        self._watch_failure = callback_exc
                    return

        self._watch_thread = threading.Thread(
            target=watch,
            name="step5d-autotune-v2-bridge-watcher",
            daemon=True,
        )
        self._watch_thread.start()

    def stop_watcher(self) -> None:
        self._watch_stop.set()
        thread = self._watch_thread
        if thread is not None:
            thread.join(timeout=1.0)
            if thread.is_alive():
                raise BridgeError("bridge watcher thread did not stop")
        self._watch_thread = None

    def stop(self, *, timeout_s: float = 5.0) -> None:
        self.stop_watcher()
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=timeout_s)
        if self.log_handle is not None:
            self.log_handle.close()
        self.process = None
        self.log_handle = None
        self._launch_nonce = None
        self._health_sequence = None
        self._sample_counter = None
        self._observed_health_sequence = None
        self._observed_sample_counter = None


def _remove_stale_snapshot(path: Path, *, runtime_root: Path, label: str) -> None:
    try:
        snapshot_stat = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(snapshot_stat.st_mode) or snapshot_stat.st_nlink != 1:
        raise BridgeError(f"stale bridge {label} evidence is unsafe")
    path.unlink()
    directory = os.open(runtime_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read_snapshot(
    path: Path, *, required: set[str], label: str
) -> dict[str, Any] | None:
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BridgeError(f"bridge {label} evidence is unsafe") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > MAX_READY_BYTES
        ):
            raise BridgeError(
                f"bridge {label} evidence is not one bounded regular file"
            )
        chunks: list[bytes] = []
        remaining = MAX_READY_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or len(encoded) != before.st_size:
        raise BridgeError(f"bridge {label} evidence changed while being read")
    try:
        payload = json.loads(encoded.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != required:
        raise BridgeError(f"bridge {label} evidence schema fields differ")
    return payload


def _read_ready_snapshot(path: Path) -> dict[str, Any] | None:
    return _read_snapshot(
        path,
        label="readiness",
        required={
        "schema",
        "bridge_ready",
        "deployment_id",
        "launch_nonce",
        "pid",
        "sample_rate_hz",
        "startup_home_verified",
        "command_transport_ready",
        "event_transport_ready",
        },
    )


def _read_health_snapshot(path: Path) -> dict[str, Any] | None:
    return _read_snapshot(
        path,
        label="health",
        required={
            "schema",
            "deployment_id",
            "pid",
            "launch_nonce",
            "health_sequence",
            "observed_at",
            "configured_rate_hz",
            "sample_counter",
            "rtde_healthy",
            "command_transport_healthy",
            "event_transport_healthy",
        },
    )
