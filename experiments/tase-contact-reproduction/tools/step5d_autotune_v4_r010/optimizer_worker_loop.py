"""Length-framed warm R010 CUDA optimizer worker."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
from typing import Any, Mapping

from .optimizer_worker import RESPONSE_SCHEMA, run


FRAME_RESPONSE_SCHEMA = "step5d.autotune-v4/r010-optimizer-frame-response-v1"
_HEADER = struct.Struct(">I")


def _read_frame(stream: Any) -> bytes | None:
    header = stream.read(_HEADER.size)
    if not header:
        return None
    if len(header) != _HEADER.size:
        raise RuntimeError("R010 optimizer frame header is truncated")
    (size,) = _HEADER.unpack(header)
    if size == 0:
        return b""
    payload = stream.read(size)
    if len(payload) != size:
        raise RuntimeError("R010 optimizer frame payload is truncated")
    return payload


def _write_frame(stream: Any, value: Mapping[str, Any]) -> None:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    stream.write(_HEADER.pack(len(encoded)))
    stream.write(encoded)
    stream.flush()


def main() -> int:
    frame_fd = os.dup(sys.stdout.fileno())
    frame_out = os.fdopen(frame_fd, "wb", buffering=0)
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    while True:
        encoded = _read_frame(sys.stdin.buffer)
        if encoded is None or encoded == b"":
            return 0
        request_sha = hashlib.sha256(encoded).hexdigest()
        try:
            request = json.loads(encoded.decode("utf-8"))
            if not isinstance(request, Mapping):
                raise TypeError("R010 optimizer request must be an object")
            result = run(request)
            if result.get("schema") != RESPONSE_SCHEMA:
                raise RuntimeError("R010 optimizer response schema differs")
            _write_frame(
                frame_out,
                {
                    "schema": FRAME_RESPONSE_SCHEMA,
                    "ok": True,
                    "request_sha256": request_sha,
                    "result": result,
                },
            )
        except Exception as exc:  # noqa: BLE001 - typed frame is the fail-closed seam
            _write_frame(
                frame_out,
                {
                    "schema": FRAME_RESPONSE_SCHEMA,
                    "ok": False,
                    "request_sha256": request_sha,
                    "reason_code": "R010_OPTIMIZER_WORKER_FAILED",
                    "detail": f"{type(exc).__name__}:{exc}",
                },
            )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["FRAME_RESPONSE_SCHEMA", "main"]
