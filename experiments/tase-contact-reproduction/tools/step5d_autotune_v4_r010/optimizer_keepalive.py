"""R010 calibration-bound keepalive client; no CPU/degraded fallback."""

from __future__ import annotations

import hashlib
import json
import os
import select
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from step5d_optimizer_runtime import (
    OptimizerRuntimeError,
    OptimizerSubprocessClient,
    ResolvedOptimizerRuntime,
)

from .optimizer_worker import REQUEST_SCHEMA, RESPONSE_SCHEMA
from .optimizer_worker_loop import FRAME_RESPONSE_SCHEMA


R010_KEEPALIVE_WORKER_MODULE = "step5d_autotune_v4_r010.optimizer_worker_loop"
_HEADER = struct.Struct(">I")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


class R010OptimizerKeepalive:
    """One warm child bound to one immutable calibration for its lifetime."""

    def __init__(
        self,
        client: OptimizerSubprocessClient,
        calibration_binding: Mapping[str, Any],
    ) -> None:
        if not isinstance(client, OptimizerSubprocessClient):
            raise TypeError("R010 keepalive requires OptimizerSubprocessClient runtime resolution")
        if not isinstance(calibration_binding, Mapping):
            raise TypeError("R010 keepalive calibration binding must be an object")
        self.client = client
        self.calibration_binding = dict(calibration_binding)
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._runtime: ResolvedOptimizerRuntime | None = None
        self._cwd = Path(__file__).resolve().parents[2]

    def _spawn(self) -> None:
        runtime = self.client._resolve()  # noqa: SLF001 - same typed runtime owner
        environment = runtime.child_environment(self.client._source_environment)  # noqa: SLF001
        process = subprocess.Popen(
            [str(runtime.python_executable), "-B", "-u", "-m", R010_KEEPALIVE_WORKER_MODULE],
            cwd=self._cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._runtime = runtime
        self._process = process

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        process = self._process
        self._process = None
        self._runtime = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.write(_HEADER.pack(0))
                process.stdin.flush()
            process.wait(timeout=5.0)
        except (OSError, BrokenPipeError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=5.0)

    def _read_exact(self, size: int, deadline: float) -> bytes:
        process = self._process
        if process is None or process.stdout is None:
            raise OptimizerRuntimeError("R010 keepalive child is missing")
        result = b""
        fd = process.stdout.fileno()
        while len(result) < size:
            if process.poll() is not None:
                raise OptimizerRuntimeError(f"R010 keepalive child exited ({process.returncode})")
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise OptimizerRuntimeError("R010 keepalive child timed out")
            ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
            if not ready:
                continue
            chunk = os.read(fd, size - len(result))
            if not chunk:
                raise OptimizerRuntimeError("R010 keepalive child closed stdout")
            result += chunk
        return result

    def request(self, r008_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._close_unlocked()
                self._spawn()
            process = self._process
            runtime = self._runtime
            assert process is not None and process.stdin is not None and runtime is not None
            body = {
                "schema": REQUEST_SCHEMA,
                "profile": "optimizer",
                "expected_attestation": runtime.expected_attestation(),
                "payload": {
                    "calibration": dict(self.calibration_binding),
                    "r008_payload": dict(r008_payload),
                },
            }
            encoded = _canonical(body)
            request_sha = hashlib.sha256(encoded).hexdigest()
            try:
                process.stdin.write(_HEADER.pack(len(encoded)))
                process.stdin.write(encoded)
                process.stdin.flush()
                deadline = time.monotonic() + self.client.timeout_s
                header = self._read_exact(_HEADER.size, deadline)
                (size,) = _HEADER.unpack(header)
                raw = self._read_exact(size, deadline)
                frame = json.loads(raw.decode("utf-8"))
            except Exception:
                process.kill()
                process.wait(timeout=5.0)
                self._process = None
                self._runtime = None
                raise
            if (
                not isinstance(frame, Mapping)
                or frame.get("schema") != FRAME_RESPONSE_SCHEMA
                or frame.get("request_sha256") != request_sha
                or frame.get("ok") is not True
            ):
                raise OptimizerRuntimeError(f"R010 keepalive response failed closed: {frame}")
            result = frame.get("result")
            if not isinstance(result, Mapping) or result.get("schema") != RESPONSE_SCHEMA:
                raise OptimizerRuntimeError("R010 keepalive result schema differs")
            payload = result.get("payload")
            if not isinstance(payload, Mapping):
                raise OptimizerRuntimeError("R010 keepalive result payload is absent")
            attestation = payload.get("attestation")
            if not isinstance(attestation, Mapping):
                raise OptimizerRuntimeError("R010 keepalive attestation is absent")
            if attestation.get("environment") != runtime.expected_attestation():
                raise OptimizerRuntimeError("R010 keepalive environment attestation differs")
            calibration = attestation.get("calibration")
            if not isinstance(calibration, Mapping) or calibration.get("calibration_sha256") != self.calibration_binding.get("calibration_sha256"):
                raise OptimizerRuntimeError("R010 keepalive calibration attestation differs")
            self.client.last_child_attestation = dict(attestation)
            return dict(payload)

    def __enter__(self) -> "R010OptimizerKeepalive":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()


__all__ = ["R010_KEEPALIVE_WORKER_MODULE", "R010OptimizerKeepalive"]
