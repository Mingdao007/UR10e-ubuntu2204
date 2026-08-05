"""Long-lived sole writer for r005 ledger + r006 sidecar seals.

Host motion never runs heavy seal CPU in-process. This daemon owns
``_cached`` hash-chain state (fixes canary 011258) and keeps
``fresh_subprocess_raw_recompute_only`` by spawning a fresh subprocess per
cold verify inside the existing ledger/sidecar append paths.

IPC: length-prefixed JSON frames (same family as optimizer_keepalive).
Each SEAL request points at a pickle of the AttemptResult under seal_inbox/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pickle
import struct
import sys
import traceback
from pathlib import Path
from typing import Any, Mapping

from step5d_autotune_v4_r008.seal_protocol import REQUEST_SCHEMA, RESPONSE_SCHEMA

_HEADER = struct.Struct(">I")


def _read_frame(stream: Any) -> bytes | None:
    header = stream.read(_HEADER.size)
    if not header:
        return None
    if len(header) != _HEADER.size:
        raise RuntimeError("seal daemon frame header truncated")
    (size,) = _HEADER.unpack(header)
    if size == 0:
        return b""
    payload = stream.read(size)
    if len(payload) != size:
        raise RuntimeError("seal daemon frame payload truncated")
    return payload


def _write_frame(stream: Any, payload: bytes) -> None:
    stream.write(_HEADER.pack(len(payload)))
    stream.write(payload)
    stream.flush()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


class _SealOwners:
    def __init__(
        self,
        *,
        ledger_path: Path,
        r006_sidecar_path: Path,
        campaign_fingerprint: str,
        eoat_sha256: str,
    ) -> None:
        # Local imports keep daemon startup light and avoid host-only deps.
        from step5d_autotune_v4_r005.observations import ObservationLedger
        from step5d_autotune_v4_r006.live_adapter import (
            R006ObservationLedger,
            _point_from_candidate,
        )
        from step5d_autotune_v4_r008.bounded_sidecar_verify import r008_bounded_sidecar_scope

        self._point_from_candidate = _point_from_candidate
        self._scope = r008_bounded_sidecar_scope(tail_rows=1)
        self._scope.__enter__()
        self.ledger = ObservationLedger(
            ledger_path,
            campaign_fingerprint=campaign_fingerprint,
            eoat_sha256=eoat_sha256,
        )
        # Sidecar path is derived from ledger stem; enforce the caller's path.
        self.r006 = R006ObservationLedger(
            self.ledger, campaign_fingerprint=campaign_fingerprint
        )
        if self.r006.raw_sidecar_path.resolve() != Path(r006_sidecar_path).resolve():
            raise RuntimeError(
                "seal daemon r006 sidecar path differs from ledger-derived path: "
                f"want={r006_sidecar_path} got={self.r006.raw_sidecar_path}"
            )
        self.campaign_fingerprint = campaign_fingerprint

    def close(self) -> None:
        try:
            self._scope.__exit__(None, None, None)
        except Exception:
            pass

    def seal_pickled_result(self, pickle_path: Path) -> dict[str, Any]:
        result = pickle.loads(pickle_path.read_bytes())
        seq = int(result.attempt_sequence)
        kind_s = str(result.kind)
        execution_id = str(result.execution_id)
        # Resume safety: skip if this execution_id is already sealed.
        existing = [
            row
            for row in self.ledger.records
            if int(row.attempt_sequence) == seq
            and str((row.metrics or {}).get("execution_id") or "") == execution_id
        ]
        def _pack(record: Any, *, trainable: bool, receipt_dict: Any, deduped: bool) -> dict[str, Any]:
            import copyreg
            from types import MappingProxyType

            record_path = pickle_path.with_name(pickle_path.stem + "-record.pkl")

            def _reduce_mappingproxy(proxy: MappingProxyType) -> tuple[Any, ...]:
                return (dict, (dict(proxy),))

            copyreg.pickle(MappingProxyType, _reduce_mappingproxy)
            record_path.write_bytes(pickle.dumps(record, protocol=pickle.HIGHEST_PROTOCOL))
            # Host extends its in-memory ledger cache from this row (no cold verify).
            row = None
            for cached_row, cached_rec in zip(
                self.ledger._cached_rows, self.ledger._cached_records, strict=True  # noqa: SLF001
            ):
                if int(cached_rec.attempt_sequence) == int(record.attempt_sequence):
                    # JSON round-trip drops mappingproxy / non-JSON bits safely.
                    row = json.loads(
                        json.dumps(dict(cached_row), default=lambda o: dict(o) if isinstance(o, Mapping) else str(o))
                    )
                    break
            if row is None:
                raise RuntimeError("seal daemon missing ledger row after append")
            return {
                "sealed_seq": int(record.attempt_sequence),
                "eligible": bool(record.eligible),
                "trainable": bool(trainable),
                "receipt": receipt_dict,
                "deduped": bool(deduped),
                "record_pickle_path": str(record_path.resolve()),
                "ledger_row": row,
            }

        if existing:
            record = existing[0]
            trainable = False
            receipt_dict = None
            if kind_s != "QUALIFICATION":
                rows = self.r006.sidecar.fresh_process_verify()
                matched = [
                    row
                    for row in rows
                    if row.get("attempt_sequence") == seq
                    and row.get("execution_id") == execution_id
                ]
                if matched:
                    trainable = bool(matched[0].get("trainable"))
                    receipt = matched[0].get("receipt")
                    receipt_dict = dict(receipt) if isinstance(receipt, Mapping) else receipt
            return _pack(record, trainable=trainable, receipt_dict=receipt_dict, deduped=True)

        if kind_s != "QUALIFICATION":
            self.r006.append_attempt_result(
                result,
                epoch=int(getattr(result, "epoch", 1) or 1),
                point=self._point_from_candidate(result.candidate),
            )
        sealed = self.ledger.append(result.to_record(self.campaign_fingerprint))
        trainable = False
        receipt_dict: dict[str, Any] | None = None
        if kind_s != "QUALIFICATION":
            # Warm sidecar refresh in the sole-writer process (hash chain only;
            # cold subprocess already ran inside append).
            rows = self.r006.sidecar._verify_rows(cold_read=False)  # noqa: SLF001
            self.r006.sidecar._cached = rows  # noqa: SLF001
            matched = [
                row
                for row in rows
                if row.get("attempt_sequence") == seq
                and row.get("execution_id") == execution_id
            ]
            if len(matched) != 1:
                raise RuntimeError("r008 seal daemon: sealed attempt lacks one raw sidecar identity")
            receipt = matched[0].get("receipt")
            trainable = bool(matched[0].get("trainable"))
            as_dict = getattr(receipt, "as_dict", None) if receipt is not None else None
            if callable(as_dict):
                receipt_dict = as_dict()
            elif isinstance(receipt, Mapping):
                receipt_dict = dict(receipt)
            else:
                receipt_dict = receipt  # type: ignore[assignment]
        return _pack(sealed, trainable=trainable, receipt_dict=receipt_dict, deduped=False)


def _handle(owners: _SealOwners, request: Mapping[str, Any]) -> dict[str, Any]:
    if request.get("schema") != REQUEST_SCHEMA:
        raise RuntimeError(f"seal daemon unknown schema: {request.get('schema')!r}")
    op = str(request.get("op") or "seal")
    if op == "ping":
        return {"pong": True, "pid": os.getpid()}
    if op != "seal":
        raise RuntimeError(f"seal daemon unknown op: {op!r}")
    pickle_path = Path(str(request["pickle_path"]))
    if pickle_path.is_symlink() or not pickle_path.is_file():
        raise RuntimeError(f"seal inbox pickle missing/unsafe: {pickle_path}")
    return owners.seal_pickled_result(pickle_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="step5d_autotune_v4_r008.seal_daemon")
    parser.add_argument("--ledger-path", required=True)
    parser.add_argument("--r006-sidecar-path", required=True)
    parser.add_argument("--campaign-fingerprint", required=True)
    parser.add_argument("--eoat-sha256", required=True)
    args = parser.parse_args(argv)

    # Framing on a dedicated fd; redirect stdout prints to stderr.
    frame_fd = os.dup(sys.stdout.fileno())
    frame_out = os.fdopen(frame_fd, "wb", buffering=0)
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    try:
        os.nice(19)
    except OSError:
        pass

    owners = _SealOwners(
        ledger_path=Path(args.ledger_path),
        r006_sidecar_path=Path(args.r006_sidecar_path),
        campaign_fingerprint=str(args.campaign_fingerprint),
        eoat_sha256=str(args.eoat_sha256),
    )
    stdin = sys.stdin.buffer
    try:
        while True:
            encoded = _read_frame(stdin)
            if encoded is None or encoded == b"":
                return 0
            request_sha = hashlib.sha256(encoded).hexdigest()
            try:
                request = json.loads(encoded.decode("utf-8"))
                if not isinstance(request, Mapping):
                    raise TypeError("request must be an object")
                result = _handle(owners, request)
                body = {
                    "schema": RESPONSE_SCHEMA,
                    "ok": True,
                    "request_sha256": request_sha,
                    "result": result,
                }
            except BaseException as exc:  # noqa: BLE001 — surface to host
                body = {
                    "schema": RESPONSE_SCHEMA,
                    "ok": False,
                    "request_sha256": request_sha,
                    "detail": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
            _write_frame(frame_out, _canonical_bytes(body))
    finally:
        owners.close()


if __name__ == "__main__":
    raise SystemExit(main())
