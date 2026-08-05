"""Host-side client for the long-lived r008 seal daemon."""

from __future__ import annotations

import json
import os
import pickle
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from step5d_autotune_v4_r008.seal_protocol import REQUEST_SCHEMA, RESPONSE_SCHEMA

R008_SEAL_DAEMON_MODULE = "step5d_autotune_v4_r008.seal_daemon"
_HEADER = struct.Struct(">I")


class SealDaemonError(RuntimeError):
    """Seal daemon IPC or child failure."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


class SealDaemonClient:
    """Length-framed keepalive client; sole ledger writers live in the child."""

    def __init__(
        self,
        *,
        ledger_path: Path,
        r006_sidecar_path: Path,
        campaign_fingerprint: str,
        eoat_sha256: str,
        inbox_dir: Path | None = None,
        timeout_s: float = 600.0,
        python_executable: str | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.ledger_path = Path(ledger_path)
        self.r006_sidecar_path = Path(r006_sidecar_path)
        self.campaign_fingerprint = str(campaign_fingerprint)
        self.eoat_sha256 = str(eoat_sha256)
        self.inbox_dir = Path(inbox_dir) if inbox_dir is not None else self.ledger_path.parent / "seal_inbox"
        self.timeout_s = float(timeout_s)
        self.python_executable = python_executable or sys.executable
        self.cwd = Path(cwd) if cwd is not None else Path(__file__).resolve().parents[2]
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self.inbox_dir.mkdir(parents=True, exist_ok=True)

    @property
    def offhost(self) -> bool:
        return True

    def close(self) -> None:
        with self._lock:
            self._kill_unlocked()

    def _kill_unlocked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.write(_HEADER.pack(0))
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except OSError:
            pass

    def _spawn_unlocked(self) -> None:
        err_path = self.ledger_path.parent / "seal_daemon.stderr.log"
        err_path.parent.mkdir(parents=True, exist_ok=True)
        err_f = open(err_path, "ab", buffering=0)
        env = dict(os.environ)
        # Ensure tools/ is importable the same way host tests do.
        tools = str(self.cwd / "tools")
        prev = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = tools if not prev else tools + os.pathsep + prev
        proc = subprocess.Popen(
            [
                self.python_executable,
                "-B",
                "-u",
                "-m",
                R008_SEAL_DAEMON_MODULE,
                "--ledger-path",
                str(self.ledger_path.resolve()),
                "--r006-sidecar-path",
                str(self.r006_sidecar_path.resolve()),
                "--campaign-fingerprint",
                self.campaign_fingerprint,
                "--eoat-sha256",
                self.eoat_sha256,
            ],
            cwd=str(self.cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err_f,
        )
        err_f.close()
        self._proc = proc

    def _read_exact_unlocked(self, fd: int, size: int, deadline: float) -> bytes:
        proc = self._proc
        assert proc is not None
        buf = b""
        while len(buf) < size:
            if proc.poll() is not None:
                raise SealDaemonError(f"seal daemon exited ({proc.returncode})")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SealDaemonError("seal daemon timed out reading frame")
            ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
            if not ready:
                continue
            chunk = os.read(fd, size - len(buf))
            if not chunk:
                raise SealDaemonError("seal daemon closed stdout")
            buf += chunk
        return buf

    def _read_frame_unlocked(self, timeout_s: float) -> bytes:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise SealDaemonError("seal daemon is missing")
        deadline = time.monotonic() + float(timeout_s)
        fd = proc.stdout.fileno()
        header = self._read_exact_unlocked(fd, _HEADER.size, deadline)
        (size,) = _HEADER.unpack(header)
        if size == 0:
            return b""
        return self._read_exact_unlocked(fd, size, deadline)

    def request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = {"schema": REQUEST_SCHEMA, **dict(payload)}
        encoded = _canonical_bytes(body)
        request_sha256 = _sha256(encoded)
        with self._lock:
            last_exc: Exception | None = None
            for attempt in range(2):
                try:
                    if self._proc is None or self._proc.poll() is not None:
                        self._kill_unlocked()
                        self._spawn_unlocked()
                    assert self._proc is not None and self._proc.stdin is not None
                    self._proc.stdin.write(_HEADER.pack(len(encoded)))
                    self._proc.stdin.write(encoded)
                    self._proc.stdin.flush()
                    raw = self._read_frame_unlocked(self.timeout_s)
                    response = json.loads(raw.decode("utf-8"))
                    if not isinstance(response, Mapping):
                        raise SealDaemonError("seal daemon response is not an object")
                    if response.get("ok") is not True:
                        detail = response.get("detail") or "unknown"
                        raise SealDaemonError(f"seal daemon failed closed: {detail}")
                    if (
                        response.get("schema") != RESPONSE_SCHEMA
                        or response.get("request_sha256") != request_sha256
                    ):
                        raise SealDaemonError("seal daemon response binding differs")
                    result = response.get("result")
                    if not isinstance(result, Mapping):
                        raise SealDaemonError("seal daemon result is missing")
                    return result
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    self._kill_unlocked()
                    if attempt == 0:
                        continue
                    break
            assert last_exc is not None
            raise SealDaemonError(f"seal daemon request failed: {last_exc}") from last_exc

    def seal_result(self, result: Any) -> Mapping[str, Any]:
        """Pickle ``result`` into the inbox and ask the daemon to seal it.

        On success the inbox pickle is removed. On failure it remains for
        crash-recovery / debugging (execution_id dedupe in the daemon).
        """

        seq = int(result.attempt_sequence)
        execution_id = str(result.execution_id)
        pickle_path = self.inbox_dir / f"{seq:06d}-{execution_id}.pkl"
        tmp = pickle_path.with_suffix(".pkl.tmp")
        tmp.write_bytes(pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL))
        os.replace(tmp, pickle_path)
        payload = self.request({"op": "seal", "pickle_path": str(pickle_path.resolve())})
        pickle_path.unlink(missing_ok=True)
        return payload


def kill_stray_seal_daemons() -> None:
    for ent in Path("/proc").iterdir():
        if not ent.name.isdigit():
            continue
        try:
            cmd = (ent / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if R008_SEAL_DAEMON_MODULE not in cmd:
            continue
        try:
            os.kill(int(ent.name), signal.SIGKILL)
        except OSError:
            pass


__all__ = [
    "R008_SEAL_DAEMON_MODULE",
    "SealDaemonClient",
    "SealDaemonError",
    "kill_stray_seal_daemons",
]
