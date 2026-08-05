"""r008 keepalive shim: reuse one CUDA worker across asks.

The frozen r006 ``optimizer_worker`` is one-shot (read stdin once, exit).  Each
ask therefore pays cold torch/CUDA import (~minutes).  This module keeps the
r006 worker module byte-identical and adds length-prefixed framing so the
parent can reuse a single child after the first warm-up.

Stage A: scoring is the r008 batched worker (one t-batched qLogNEI forward).
Frozen r006 ``run`` remains the offline equivalence oracle.

Library code (torch/botorch) may print to stdout; the framing channel is kept
on a dedicated stream so those prints cannot corrupt the protocol.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
from typing import Any, Mapping

from step5d_autotune_v4_r008.optimizer_worker_batched import RESPONSE_SCHEMA, run


_HEADER = struct.Struct(">I")


def _read_frame(stream: Any) -> bytes | None:
    header = stream.read(_HEADER.size)
    if not header:
        return None
    if len(header) != _HEADER.size:
        raise RuntimeError("optimizer keepalive frame header truncated")
    (size,) = _HEADER.unpack(header)
    if size == 0:
        return b""
    payload = stream.read(size)
    if len(payload) != size:
        raise RuntimeError("optimizer keepalive frame payload truncated")
    return payload


def _write_frame(stream: Any, payload: bytes) -> None:
    stream.write(_HEADER.pack(len(payload)))
    stream.write(payload)
    stream.flush()


def main() -> int:
    # Dup the real stdout fd for framing, then point sys.stdout at stderr so
    # accidental prints from GPU libs cannot interleave with response frames.
    frame_fd = os.dup(sys.stdout.fileno())
    frame_out = os.fdopen(frame_fd, "wb", buffering=0)
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    stdin = sys.stdin.buffer
    while True:
        encoded = _read_frame(stdin)
        if encoded is None:
            return 0
        if encoded == b"":
            return 0
        try:
            request = json.loads(encoded.decode("utf-8"))
            if not isinstance(request, Mapping):
                raise TypeError("request envelope must be an object")
            result = run(request)
            attestation = result.pop("attestation")
            body = {
                "schema": RESPONSE_SCHEMA,
                "ok": True,
                "request_sha256": hashlib.sha256(encoded).hexdigest(),
                "attestation": attestation,
                "result": result,
            }
            _write_frame(
                frame_out,
                json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )
        except Exception as exc:
            err = {
                "schema": RESPONSE_SCHEMA,
                "ok": False,
                "reason_code": "OPTIMIZER_WORKER_FAILED",
                "detail": f"{type(exc).__name__}:{exc}",
            }
            _write_frame(
                frame_out,
                json.dumps(err, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
