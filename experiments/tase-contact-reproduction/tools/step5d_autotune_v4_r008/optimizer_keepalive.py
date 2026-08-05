"""r008 overlay: keep one CUDA optimizer child warm across asks.

``OptimizerSubprocessClient`` is intentionally one-shot.  On this host that
makes every BO ask pay a multi-minute torch/CUDA cold start, so cutting
``CANDIDATE_SOBOL`` alone cannot bring seal→dispatch idle under ~280s.  This
scope leaves the shared client module byte-identical and only redirects
``request()`` at the live r008 host to a length-framed keepalive worker that
reuses r006 ``optimizer_worker.run``.
"""

from __future__ import annotations

import json
import os
import select
import signal
import struct
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from step5d_optimizer_runtime import (
    REQUEST_SCHEMA,
    RESPONSE_SCHEMA,
    OptimizerRuntimeError,
    OptimizerSubprocessClient,
    ResolvedOptimizerRuntime,
)

R008_KEEPALIVE_WORKER_MODULE = "step5d_autotune_v4_r008.optimizer_worker_loop"
_HEADER = struct.Struct(">I")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


class _KeepaliveWorker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._runtime: ResolvedOptimizerRuntime | None = None
        self._cwd = Path(__file__).resolve().parents[2]

    def close(self) -> None:
        with self._lock:
            self._kill_unlocked()

    def _kill_unlocked(self) -> None:
        proc = self._proc
        self._proc = None
        self._runtime = None
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

    def _spawn_unlocked(self, client: OptimizerSubprocessClient) -> None:
        runtime = client._resolve()  # noqa: SLF001 — process-local overlay
        environment = runtime.child_environment(client._source_environment)  # noqa: SLF001
        # File stderr (not PIPE): a PIPE fills under torch/CUDA noise and
        # deadlocks the child while the parent blocks on a stdout frame.
        err_path = self._cwd / "runs" / "step5d_autotune_v4_r008" / "optimizer_keepalive.stderr.log"
        err_path.parent.mkdir(parents=True, exist_ok=True)
        err_f = open(err_path, "ab", buffering=0)
        proc = subprocess.Popen(
            [str(runtime.python_executable), "-B", "-u", "-m", R008_KEEPALIVE_WORKER_MODULE],
            cwd=self._cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err_f,
        )
        err_f.close()  # child holds the fd
        self._proc = proc
        self._runtime = runtime

    def _read_exact_unlocked(self, fd: int, size: int, deadline: float) -> bytes:
        proc = self._proc
        assert proc is not None
        buf = b""
        while len(buf) < size:
            if proc.poll() is not None:
                raise OptimizerRuntimeError(
                    f"optimizer keepalive child exited ({proc.returncode})"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OptimizerRuntimeError(
                    f"optimizer keepalive child timed out after frame read"
                )
            ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
            if not ready:
                continue
            # os.read — never mix select() with buffered file object reads.
            chunk = os.read(fd, size - len(buf))
            if not chunk:
                raise OptimizerRuntimeError("optimizer keepalive child closed stdout")
            buf += chunk
        return buf

    def _read_frame_unlocked(self, timeout_s: float) -> bytes:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise OptimizerRuntimeError("optimizer keepalive child is missing")
        deadline = time.monotonic() + float(timeout_s)
        fd = proc.stdout.fileno()
        header = self._read_exact_unlocked(fd, _HEADER.size, deadline)
        (size,) = _HEADER.unpack(header)
        if size == 0:
            return b""
        return self._read_exact_unlocked(fd, size, deadline)
    def request(self, client: OptimizerSubprocessClient, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._lock:
            body = {
                "schema": REQUEST_SCHEMA,
                "profile": "optimizer",
                "expected_attestation": None,  # filled after resolve
                "payload": dict(payload),
            }
            last_exc: Exception | None = None
            for attempt in range(2):
                try:
                    if self._proc is None or self._proc.poll() is not None:
                        self._kill_unlocked()
                        self._spawn_unlocked(client)
                    assert self._runtime is not None and self._proc is not None
                    assert self._proc.stdin is not None
                    body["profile"] = self._runtime.declaration.profile
                    body["expected_attestation"] = self._runtime.expected_attestation()
                    encoded = _canonical_bytes(body)
                    request_sha256 = _sha256(encoded)
                    self._proc.stdin.write(_HEADER.pack(len(encoded)))
                    self._proc.stdin.write(encoded)
                    self._proc.stdin.flush()
                    raw = self._read_frame_unlocked(client.timeout_s)
                    response = json.loads(raw.decode("utf-8"))
                    if not isinstance(response, Mapping):
                        raise OptimizerRuntimeError("optimizer keepalive response is not an object")
                    if response.get("ok") is not True:
                        detail = response.get("detail") or response.get("reason_code")
                        raise OptimizerRuntimeError(
                            f"optimizer keepalive child failed closed: {detail}"
                        )
                    if (
                        response.get("schema") != RESPONSE_SCHEMA
                        or response.get("request_sha256") != request_sha256
                    ):
                        raise OptimizerRuntimeError("optimizer keepalive response binding differs")
                    child_attestation = response.get("attestation")
                    if not isinstance(child_attestation, Mapping):
                        raise OptimizerRuntimeError(
                            "optimizer keepalive child did not self-attest its environment"
                        )
                    expected = self._runtime.expected_attestation()
                    if dict(child_attestation) != expected:
                        raise OptimizerRuntimeError(
                            "optimizer keepalive child environment/GPU/version attestation differs"
                        )
                    client.last_child_attestation = dict(child_attestation)
                    result = response.get("result")
                    if not isinstance(result, Mapping):
                        raise OptimizerRuntimeError("optimizer keepalive result is missing")
                    return result
                except Exception as exc:  # noqa: BLE001 — retry once on a fresh child
                    last_exc = exc
                    self._kill_unlocked()
                    if attempt == 0:
                        continue
                    break
            assert last_exc is not None
            raise OptimizerRuntimeError(f"optimizer keepalive request failed: {last_exc}") from last_exc


def _kill_stray_keepalive_workers() -> None:
    """Best-effort reap of orphaned loop workers from a previous crashed host."""

    for ent in Path("/proc").iterdir():
        if not ent.name.isdigit():
            continue
        try:
            cmd = (ent / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "step5d_autotune_v4_r008.optimizer_worker_loop" not in cmd:
            continue
        try:
            os.kill(int(ent.name), signal.SIGKILL)
        except OSError:
            pass


@contextmanager
def r008_optimizer_keepalive_scope() -> Iterator[None]:
    """Process-local patch: reuse one framed CUDA worker for all client requests."""

    _kill_stray_keepalive_workers()
    worker = _KeepaliveWorker()
    original_request = OptimizerSubprocessClient.request

    def _patched(self: OptimizerSubprocessClient, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return worker.request(self, payload)

    try:
        OptimizerSubprocessClient.request = _patched  # type: ignore[method-assign]
        yield
    finally:
        OptimizerSubprocessClient.request = original_request  # type: ignore[method-assign]
        worker.close()
        _kill_stray_keepalive_workers()


__all__ = [
    "R008_KEEPALIVE_WORKER_MODULE",
    "r008_optimizer_keepalive_scope",
]
