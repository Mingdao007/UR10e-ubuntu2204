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
import resource
import struct
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from step5d_autotune_v4_r008.optimizer_worker_batched import RESPONSE_SCHEMA, run


_HEADER = struct.Struct(">I")

# Diagnostic-only (period-75 investigation, 2026-08-07): one JSONL line per
# request with RSS/CUDA/elapsed/ledger-size. Never raises — a failure here
# must not affect the real request/response protocol on frame_out.
_DIAG_PATH = (
    Path(__file__).resolve().parents[2]
    / "runs"
    / "step5d_autotune_v4_r008"
    / "r008_keepalive_worker_diag.jsonl"
)


def _log_diag(record: dict[str, Any]) -> None:
    try:
        _DIAG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _DIAG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 — diagnostics must never break a request
        pass


def _cuda_stats() -> dict[str, float] | None:
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        return {
            "cuda_allocated_mb": torch.cuda.memory_allocated() / (1024 * 1024),
            "cuda_reserved_mb": torch.cuda.memory_reserved() / (1024 * 1024),
        }
    except Exception:  # noqa: BLE001
        return None


def _ledger_rows(request: Mapping[str, Any]) -> int | None:
    try:
        payload = request.get("payload")
        rows = payload["artifact_binding"]["rows"]  # type: ignore[index]
        return len(rows)
    except Exception:  # noqa: BLE001
        return None


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
    request_count = 0
    while True:
        encoded = _read_frame(stdin)
        if encoded is None:
            return 0
        if encoded == b"":
            return 0
        request_count += 1
        started = time.monotonic()
        ok = True
        detail = None
        request: Any = None
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
            ok = False
            detail = f"{type(exc).__name__}:{exc}"
            err = {
                "schema": RESPONSE_SCHEMA,
                "ok": False,
                "reason_code": "OPTIMIZER_WORKER_FAILED",
                "detail": detail,
            }
            _write_frame(
                frame_out,
                json.dumps(err, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )
        finally:
            record: dict[str, Any] = {
                "ts": time.time(),
                "request_count": request_count,
                "elapsed_s": time.monotonic() - started,
                "ok": ok,
                "rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "ledger_rows": _ledger_rows(request) if isinstance(request, Mapping) else None,
            }
            cuda = _cuda_stats()
            if cuda is not None:
                record.update(cuda)
            if not ok:
                record["detail"] = detail
            _log_diag(record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
