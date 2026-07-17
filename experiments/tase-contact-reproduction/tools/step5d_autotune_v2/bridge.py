"""Fail-closed bridge child process ownership for the systemd service."""

from __future__ import annotations

import json
import os
import fcntl
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
STARTUP_SCHEMA = "step5d.autotune.tp-startup/v1"
MAX_READY_BYTES = 64 * 1024
LIVE_WRITER_LOCK_PATH = Path("/tmp/ur10e-resource-locks/live-writer.lock")


class BridgeError(RuntimeError):
    """Raised when the frozen bridge command cannot prove readiness."""


class LiveWriterLock:
    """One machine-wide owner for RTDE inputs and every other live writer."""

    def __init__(self, path: Path = LIVE_WRITER_LOCK_PATH) -> None:
        if not path.is_absolute():
            raise BridgeError("live-writer lock path must be absolute")
        self.path = path
        self.descriptor: int | None = None

    def acquire(self) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        if parent.is_symlink() or self.path.is_symlink():
            raise BridgeError("live-writer lock path must not be a symlink")
        descriptor = os.open(
            self.path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise BridgeError("another live writer already owns the global lock") from exc
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        self.descriptor = descriptor

    def release(self) -> None:
        if self.descriptor is None:
            return
        fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        os.close(self.descriptor)
        self.descriptor = None


@dataclass(frozen=True)
class BridgeReady:
    pid: int
    sample_rate_hz: int
    deployment_id: str
    launch_nonce: str
    startup_stationary_verified: bool
    evidence_path: Path
    health_evidence_path: Path
    startup_evidence_path: Path
    startup_baseline: str


@dataclass(frozen=True)
class BridgeStartup:
    phase: str
    baseline: str
    observed_at: str
    sample_counter: int
    stable_duration_s: float
    startup_gate_passed: bool
    evidence_path: Path


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
        startup_path = self.runtime_root / "tp_startup.json"
        revocation_path = self.runtime_root / "bridge_revocation.json"
        for snapshot_path, label in (
            (ready_path, "readiness"),
            (health_path, "health"),
            (startup_path, "TP startup"),
            (revocation_path, "revocation"),
        ):
            _remove_stale_snapshot(
                snapshot_path, runtime_root=self.runtime_root, label=label
            )
        log_path = self.runtime_root / "bridge.log"
        self.log_handle = log_path.open("a", encoding="utf-8")
        environment = dict(os.environ)
        environment.update(self.environment)
        startup_gate_required = (
            environment.get("STEP5D_AUTOTUNE_V2_STARTUP_GATE_REQUIRED") == "1"
        )
        environment["STEP5D_AUTOTUNE_V2_DEPLOYMENT_ID"] = self.deployment_id
        environment["STEP5D_AUTOTUNE_V2_RUNTIME_ROOT"] = str(self.runtime_root)
        launch_nonce = secrets.token_hex(32)
        self._launch_nonce = launch_nonce
        environment["STEP5D_AUTOTUNE_V2_LAUNCH_NONCE"] = launch_nonce
        argv = tuple(
            token.replace("{root}", str(self.root)).replace(
                "{runtime_root}", str(self.runtime_root)
            )
            for token in self.argv
        )
        self.process = subprocess.Popen(
            argv,
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
                    and payload.get("startup_stationary_verified") is True
                    and payload.get("command_transport_ready") is True
                    and payload.get("event_transport_ready") is True
                ):
                    startup = None
                    if startup_gate_required:
                        startup = _read_startup_snapshot(startup_path)
                        if startup is None:
                            time.sleep(0.05)
                            continue
                        _validate_startup_identity(
                            startup,
                            deployment_id=self.deployment_id,
                            pid=self.process.pid,
                            launch_nonce=launch_nonce,
                        )
                        if (
                            startup["phase"] != "awaiting_tp_play"
                            or startup["baseline"] not in {"cold_zero", "latched_ready"}
                            or startup["startup_gate_passed"] is not False
                            or startup["motion_allowed"] is not False
                            or startup["operator_action"] != "press_tp_play"
                        ):
                            raise BridgeError(
                                "bridge readiness is not bound to an accepted pre-Play baseline"
                            )
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
                        startup_stationary_verified=True,
                        evidence_path=ready_path,
                        health_evidence_path=health_path,
                        startup_evidence_path=startup_path,
                        startup_baseline=(
                            "legacy_unverified"
                            if startup is None
                            else str(startup["baseline"])
                        ),
                    )
                raise BridgeError(
                    "bridge readiness evidence lacks exact launch/rate/stationary/transport proof"
                )
            time.sleep(0.05)
        raise BridgeError(f"bridge did not publish stable 500 Hz readiness: log={log_path}")

    def wait_startup_gate(
        self,
        *,
        timeout_s: float,
        minimum_stable_s: float,
    ) -> BridgeStartup:
        if timeout_s <= 0 or minimum_stable_s <= 0:
            raise BridgeError("startup gate timeouts must be positive")
        if self.environment.get("STEP5D_AUTOTUNE_V2_STARTUP_GATE_REQUIRED") != "1":
            raise BridgeError("production startup gate is not enabled for this bridge")
        process = self.process
        launch_nonce = self._launch_nonce
        if process is None or launch_nonce is None or process.poll() is not None:
            raise BridgeError("startup gate requires the live bridge child")
        path = self.runtime_root / "tp_startup.json"
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise BridgeError(f"bridge child exited before startup gate: rc={process.returncode}")
            payload = _read_startup_snapshot(path)
            if payload is not None:
                _validate_startup_identity(
                    payload,
                    deployment_id=self.deployment_id,
                    pid=process.pid,
                    launch_nonce=launch_nonce,
                )
                phase = payload["phase"]
                if phase not in {"awaiting_tp_play", "stabilizing", "live"}:
                    raise BridgeError(f"unexpected TP startup phase: {phase}")
                if phase == "live":
                    if (
                        payload["startup_gate_passed"] is not True
                        or payload["motion_allowed"] is not False
                        or payload["operator_action"] is not None
                        or float(payload["stable_duration_s"]) < minimum_stable_s
                        or payload["runtime_state"] != 2
                        or payload["tp_state"] != 10
                        or payload["consumed_command_seq"] != 0
                    ):
                        raise BridgeError("TP startup gate evidence is incomplete")
                    return BridgeStartup(
                        phase="live",
                        baseline=str(payload["baseline"]),
                        observed_at=str(payload["observed_at"]),
                        sample_counter=int(payload["sample_counter"]),
                        stable_duration_s=float(payload["stable_duration_s"]),
                        startup_gate_passed=True,
                        evidence_path=path,
                    )
            self.check_health(max_age_s=2.0, require_progress=False)
            time.sleep(0.02)
        raise BridgeError("timed out waiting for TP Play and stable READY_HOME")

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
        watcher_error: BaseException | None = None
        termination_error: BaseException | None = None
        try:
            self.stop_watcher()
        except BaseException as exc:
            watcher_error = exc
        process = self.process
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=timeout_s)
        except BaseException as exc:
            termination_error = exc
        finally:
            try:
                if self.log_handle is not None:
                    self.log_handle.close()
            except BaseException as exc:
                if termination_error is None:
                    termination_error = exc
            finally:
                self.process = None
                self.log_handle = None
                self._launch_nonce = None
                self._health_sequence = None
                self._sample_counter = None
                self._observed_health_sequence = None
                self._observed_sample_counter = None
        if termination_error is not None:
            raise BridgeError(
                f"bridge child could not be stopped: {termination_error}"
            ) from termination_error
        if watcher_error is not None:
            raise BridgeError(
                f"bridge watcher stop failed after child cleanup: {watcher_error}"
            ) from watcher_error


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
            # The writer commits with os.replace().  A reader may therefore
            # hold the previous complete inode after it has been unlinked;
            # nlink=0 is a valid immutable snapshot, while >1 is still unsafe.
            or before.st_nlink not in {0, 1}
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
            "startup_stationary_verified",
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


def _read_startup_snapshot(path: Path) -> dict[str, Any] | None:
    return _read_snapshot(
        path,
        label="TP startup",
        required={
            "schema",
            "pid",
            "deployment_id",
            "launch_nonce",
            "observed_at",
            "phase",
            "baseline",
            "runtime_state",
            "tp_state",
            "consumed_command_seq",
            "sample_counter",
            "stable_duration_s",
            "startup_gate_passed",
            "motion_allowed",
            "operator_action",
        },
    )


def _validate_startup_identity(
    payload: Mapping[str, Any],
    *,
    deployment_id: str,
    pid: int,
    launch_nonce: str,
) -> None:
    try:
        observed = datetime.fromisoformat(str(payload.get("observed_at", "")))
    except ValueError as exc:
        raise BridgeError("TP startup timestamp is invalid") from exc
    valid = (
        payload.get("schema") == STARTUP_SCHEMA
        and payload.get("deployment_id") == deployment_id
        and payload.get("pid") == pid
        and payload.get("launch_nonce") == launch_nonce
        and observed.tzinfo is not None
        and payload.get("baseline") in {"cold_zero", "latched_ready"}
        and isinstance(payload.get("sample_counter"), int)
        and not isinstance(payload.get("sample_counter"), bool)
        and payload.get("sample_counter") >= 0
        and isinstance(payload.get("stable_duration_s"), (int, float))
        and not isinstance(payload.get("stable_duration_s"), bool)
        and float(payload.get("stable_duration_s")) >= 0.0
    )
    if not valid:
        raise BridgeError("TP startup evidence lacks exact child/deployment identity")
