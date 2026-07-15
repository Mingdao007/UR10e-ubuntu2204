"""Fail-closed bridge child process ownership for the systemd service."""

from __future__ import annotations

import json
import os
import secrets
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


READY_SCHEMA = "step5d.autotune.bridge-ready/v2"
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

    def start(self, *, timeout_s: float = 15.0) -> BridgeReady:
        if self.process is not None:
            raise BridgeError("bridge child has already been started")
        if self.runtime_root.is_symlink():
            raise BridgeError("bridge runtime root must not be a symlink")
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        if not self.runtime_root.is_dir():
            raise BridgeError("bridge runtime root is not a directory")
        ready_path = self.runtime_root / "bridge_ready.json"
        try:
            ready_stat = ready_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(ready_stat.st_mode) or ready_stat.st_nlink != 1:
                raise BridgeError("stale bridge readiness evidence is unsafe")
            ready_path.unlink()
            directory = os.open(
                self.runtime_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        log_path = self.runtime_root / "bridge.log"
        self.log_handle = log_path.open("a", encoding="utf-8")
        environment = dict(os.environ)
        environment.update(self.environment)
        environment["STEP5D_AUTOTUNE_V2_DEPLOYMENT_ID"] = self.deployment_id
        environment["STEP5D_AUTOTUNE_V2_RUNTIME_ROOT"] = str(self.runtime_root)
        launch_nonce = secrets.token_hex(32)
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
                    return BridgeReady(
                        pid=self.process.pid,
                        sample_rate_hz=500,
                        deployment_id=self.deployment_id,
                        launch_nonce=launch_nonce,
                        startup_home_verified=True,
                        evidence_path=ready_path,
                    )
                raise BridgeError(
                    "bridge readiness evidence lacks exact launch/rate/Home/transport proof"
                )
            time.sleep(0.05)
        raise BridgeError(f"bridge did not publish stable 500 Hz readiness: log={log_path}")

    def stop(self, *, timeout_s: float = 5.0) -> None:
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


def _read_ready_snapshot(path: Path) -> dict[str, Any] | None:
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BridgeError("bridge readiness evidence is unsafe") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > MAX_READY_BYTES
        ):
            raise BridgeError("bridge readiness evidence is not one bounded regular file")
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
        raise BridgeError("bridge readiness evidence changed while being read")
    try:
        payload = json.loads(encoded.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    required = {
        "schema",
        "bridge_ready",
        "deployment_id",
        "launch_nonce",
        "pid",
        "sample_rate_hz",
        "startup_home_verified",
        "command_transport_ready",
        "event_transport_ready",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise BridgeError("bridge readiness evidence schema fields differ")
    return payload
