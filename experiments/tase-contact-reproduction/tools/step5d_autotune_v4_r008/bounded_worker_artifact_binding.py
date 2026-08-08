"""r008 overlay: bound the worker-side ``_artifact_binding`` per-call cost.

Root cause (found live, 2026-08-03/04): the frozen r006
``optimizer_worker._artifact_binding`` unconditionally calls
``cold_read_verify`` -- a full recompute of the objective from ~28,000 raw
force samples per trial -- for **every** row in the sidecar file on **every**
single ``ask()``/``fit_group_once()`` call. The ``if identity in
requested_ids`` membership check only decides whether to *keep* a row in the
returned binding; it runs *after* the expensive verification already paid
its cost. This is independent of ``CANDIDATE_SOBOL`` and independent of
whether the qLogNEI scoring loop is batched -- it fires once per call,
scaling with total campaign length (row count), not with candidate count.

Measured directly against the live 1113_stage_d run (2026-08-03/04):
``_artifact_binding`` alone cost ~169s at 83 rows. Separately, cutting
``CANDIDATE_SOBOL`` 76->44 candidates and later batching the qLogNEI scoring
loop into one t-batched forward left the live seal->dispatch ask gap
unchanged across four back-to-back live measurements (356s, 526s, 381s,
378s) -- conclusive evidence neither change touched the real bottleneck,
which is this function.

The row-level hash chain inside ``_artifact_binding`` (``previous_sha256``/
``row_sha256``) already protects every row's own stored fields against
tampering, unconditionally, for every row regardless of request membership.
What the per-row ``cold_read_verify`` *additionally* catches is the raw
artifact *file* silently diverging from what the sidecar row claims --
a risk concentrated in the newest rows; older rows were already cold-read
verified once (when they were themselves the tail) and are protected against
later tampering by the hash chain.

This r008-owned replacement keeps the same validation shape (header check,
sidecar byte hash, per-row hash chain over the *entire* file, per-row
artifact byte hash for every requested row) but bounds the expensive
``cold_read_verify`` recompute to the newest ``tail_rows`` (default 5)
*positions in the sidecar body*; older requested rows fall back to a plain
``R006ObjectiveReceipt.from_mapping`` parse of the already-hash-verified
artifact bytes (no raw-sample recompute, no subprocess).

Hot-path follow-up (2026-08-04): even with cold_read bounded, every ask still
``json.loads``'d ~90 x ~18MB receipts (each embedding ``raw_bundle``),
costing ~65s. Keepalive workers now cache verified receipts by
``artifact_sha256`` so repeat asks skip the JSON parse after the artifact
digest still matches (mtime/size memo avoids re-reading unchanged files).
Uncached tail rows still run ``cold_read_verify``; once cached and the file
is unchanged, repeats reuse that verified receipt. It never touches
``tools/step5d_autotune_v4_r006/optimizer_worker.py``, so r006/r007 remain
byte-for-byte unaffected.

Memory bound (Track A2, 2026-08-04): the receipt cache was unbounded and held
full ``raw_bundle`` payloads (~18MB/row). Live keepalive RSS hit ~6.6GB at
~96 rows. After verification we now cache a slim receipt with ``raw_bundle``
replaced by ``{}`` (downstream ask/fit only needs ``.objective`` /
``.trainable`` / row ``point_key``; seal was already validated before slim),
and cap the cache with an LRU row-count limit.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import replace
from typing import Any, Mapping

from step5d_autotune_v4_r006 import objective as obj_mod
from step5d_autotune_v4_r006.optimizer_worker import (
    OptimizerWorkerError,
    _digest,
    _regular_file,
    _row_hash,
)
from step5d_autotune_v4_r008.binary_seal import (
    R008_RAW_BUNDLE_SCHEMA,
    R008_RAW_CODEC,
    is_binary_seal_receipt,
    r008_binary_seal_scope,
)
from step5d_autotune_v4_r008.raw_force_binary import load_receipt_mapping_from_bytes
from step5d_autotune_v4_r008.raw_force_binary_v2 import SAMPLES_FORMAT_V2

R006ObjectiveReceipt = obj_mod.R006ObjectiveReceipt

R008_ARTIFACT_BINDING_TAIL_ROWS = 5

# Safety cap on cached receipts (sha256 -> slim receipt). Primary memory
# win is dropping raw_bundle; this LRU stops unbounded growth if campaigns
# run far past the observed ~100-row regime.
R008_RECEIPT_CACHE_MAX_ENTRIES = 256

_SIDECAR_SCHEMA = "step5d.autotune-v4/r006-objective-sidecar-v1"

# Keepalive-process cache: artifact file digest -> already-verified receipt.
# Keyed only by content hash so a rewritten file with a new digest misses.
# Values are memory-slim (raw_bundle dropped after verification).
_RECEIPT_BY_ARTIFACT_SHA256: OrderedDict[str, R006ObjectiveReceipt] = OrderedDict()

# path -> (mtime_ns, size, sha256). Artifacts are write-once; unchanged
# mtime/size lets repeat asks skip re-reading ~18MB just to re-hash.
_ARTIFACT_SHA_BY_PATH: dict[str, tuple[int, int, str]] = {}


def clear_receipt_cache() -> None:
    """Drop cached receipts and path digests (tests / explicit process reset)."""

    _RECEIPT_BY_ARTIFACT_SHA256.clear()
    _ARTIFACT_SHA_BY_PATH.clear()


def receipt_cache_size() -> int:
    return len(_RECEIPT_BY_ARTIFACT_SHA256)


def _slim_cached_receipt(receipt: R006ObjectiveReceipt) -> R006ObjectiveReceipt:
    """Drop the ~18MB raw_bundle after verification; keep ask/fit fields.

    ``validate_seal`` embeds ``raw_bundle`` in the seal payload, so callers
    must only slim *after* seal / cold_read verification. Downstream
    ``.trainable`` / ``.objective`` depend only on verification_state,
    formal bin counts, and objective_mae_n -- not on the raw samples.

    Phase-5 binary receipts keep a schema-valid empty stub (not ``{}``):
    ``r008_binary_seal_scope`` post_init rejects a bare empty mapping.
    """

    if not receipt.raw_bundle:
        return receipt
    if is_binary_seal_receipt(receipt):
        bundle = (
            dict(receipt.raw_bundle) if isinstance(receipt.raw_bundle, Mapping) else {}
        )
        slim_bundle = {
            "schema": bundle.get("schema", R008_RAW_BUNDLE_SCHEMA),
            "attempt_sequence": int(receipt.attempt_sequence),
            "execution_id": str(receipt.execution_id),
            "campaign_fingerprint": str(receipt.campaign_fingerprint),
            "candidate_uid": str(bundle.get("candidate_uid", "")),
            "semantic_fingerprint": str(
                bundle.get("semantic_fingerprint", receipt.semantic_fingerprint)
            ),
            "samples": [],
            "samples_format": bundle.get("samples_format", SAMPLES_FORMAT_V2),
            "raw_codec": bundle.get("raw_codec", R008_RAW_CODEC),
            "sample_count": int(receipt.sample_count),
            "seal_block_sha256": str(
                bundle.get("seal_block_sha256", receipt.raw_bundle_digest)
            ),
        }
        return replace(receipt, raw_bundle=slim_bundle)
    return replace(receipt, raw_bundle={})


def _receipt_cache_get(artifact_sha: str) -> R006ObjectiveReceipt | None:
    cached = _RECEIPT_BY_ARTIFACT_SHA256.get(artifact_sha)
    if cached is not None:
        _RECEIPT_BY_ARTIFACT_SHA256.move_to_end(artifact_sha)
    return cached


def _receipt_cache_put(artifact_sha: str, receipt: R006ObjectiveReceipt) -> R006ObjectiveReceipt:
    slim = _slim_cached_receipt(receipt)
    if artifact_sha in _RECEIPT_BY_ARTIFACT_SHA256:
        _RECEIPT_BY_ARTIFACT_SHA256.move_to_end(artifact_sha)
    _RECEIPT_BY_ARTIFACT_SHA256[artifact_sha] = slim
    while len(_RECEIPT_BY_ARTIFACT_SHA256) > int(R008_RECEIPT_CACHE_MAX_ENTRIES):
        _RECEIPT_BY_ARTIFACT_SHA256.popitem(last=False)
    return slim


def _artifact_digest_and_bytes(path: Any) -> tuple[str, bytes | None]:
    """Return (sha256, bytes_or_None).

    When the on-disk mtime/size still match a prior digest, returns
    ``(digest, None)`` so callers can skip re-reading the file.
    """

    key = str(path)
    stat = path.stat()
    memo = _ARTIFACT_SHA_BY_PATH.get(key)
    if (
        memo is not None
        and memo[0] == stat.st_mtime_ns
        and memo[1] == stat.st_size
    ):
        return memo[2], None
    encoded = path.read_bytes()
    digest = hashlib.sha256(encoded).hexdigest()
    _ARTIFACT_SHA_BY_PATH[key] = (stat.st_mtime_ns, stat.st_size, digest)
    return digest, encoded


def bounded_artifact_binding(
    value: Any, *, tail_rows: int = R008_ARTIFACT_BINDING_TAIL_ROWS
) -> dict[str, Any]:
    # Phase-5 receipts need module-level patches (version + cold_read_binary).
    # Never bind ``from objective import cold_read_verify`` at import time —
    # that freezes the stock function and breaks empty-sample RAW2 stubs.
    with r008_binary_seal_scope():
        return _bounded_artifact_binding_impl(value, tail_rows=tail_rows)


def _bounded_artifact_binding_impl(
    value: Any, *, tail_rows: int
) -> dict[str, Any]:
    required = {
        "sidecar_path",
        "sidecar_sha256",
        "sidecar_prefix_bytes",
        "campaign_fingerprint",
        "rows",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise OptimizerWorkerError("r006 artifact binding fields differ")
    sidecar = _regular_file(value["sidecar_path"], "r006 artifact sidecar")
    sidecar_sha = _digest(value["sidecar_sha256"], "r006 artifact sidecar digest")
    campaign = _digest(value["campaign_fingerprint"], "r006 artifact campaign")
    prefix_bytes = value["sidecar_prefix_bytes"]
    if not isinstance(prefix_bytes, int) or isinstance(prefix_bytes, bool) or prefix_bytes < 0:
        raise OptimizerWorkerError("r006 artifact sidecar prefix is invalid")
    # 2026-08-07: hash/parse only the byte prefix the requested rows live in
    # (append-only file, so this prefix never changes once written) instead
    # of the whole file -- an unrelated concurrent append past this prefix
    # used to race the host's snapshot and fail-close the whole host.
    data = sidecar.read_bytes()
    prefix = data[:prefix_bytes]
    if hashlib.sha256(prefix).hexdigest() != sidecar_sha:
        raise OptimizerWorkerError("r006 artifact sidecar bytes differ")
    try:
        lines = [
            json.loads(line)
            for line in prefix.decode("utf-8").splitlines()
            if line.strip()
        ]
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OptimizerWorkerError("r006 artifact sidecar is not strict JSONL") from exc
    if (
        not lines
        or lines[0].get("schema") != _SIDECAR_SCHEMA
        or lines[0].get("record_type") != "header"
    ):
        raise OptimizerWorkerError("r006 artifact sidecar header differs")
    if lines[0].get("campaign_fingerprint") != campaign:
        raise OptimizerWorkerError("r006 artifact sidecar campaign differs")
    requested_rows = value["rows"]
    if not isinstance(requested_rows, list):
        raise OptimizerWorkerError("r006 artifact refs are not ordered")
    requested_ids = tuple(
        (int(item["attempt_sequence"]), str(item["execution_id"])) for item in requested_rows
    )
    if len(set(requested_ids)) != len(requested_ids):
        raise OptimizerWorkerError("r006 artifact refs contain duplicates")
    requested_id_set = set(requested_ids)

    body = lines[1:]
    tail_start = max(0, len(body) - int(tail_rows))
    previous = "0" * 64
    normalized: list[dict[str, Any]] = []
    for position, row in enumerate(body):
        if (
            row.get("record_type") != "objective_artifact"
            or row.get("previous_sha256") != previous
            or row.get("row_sha256") != _row_hash(row)
        ):
            raise OptimizerWorkerError("r006 artifact sidecar hash chain differs")
        identity = (int(row["attempt_sequence"]), str(row["execution_id"]))
        if identity in requested_id_set:
            artifact = _regular_file(
                str(sidecar.parent / "r006_raw_objectives" / str(row["artifact_name"])),
                "r006 raw artifact",
            )
            artifact_sha, encoded = _artifact_digest_and_bytes(artifact)
            if artifact_sha != row.get("artifact_sha256"):
                raise OptimizerWorkerError("r006 raw artifact bytes differ")
            in_tail = position >= tail_start
            try:
                cached = _receipt_cache_get(artifact_sha)
                if cached is not None:
                    # Digest still matches (mtime/size memo or fresh hash);
                    # reuse the verified receipt and skip the ~18MB JSON parse
                    # that dominated the ~65s ask tax. Unchanged tail files
                    # keep their prior cold_read_verify result.
                    receipt = cached
                    if receipt.verification_state != "verified_raw_artifact":
                        receipt = replace(
                            receipt, verification_state="verified_raw_artifact"
                        )
                        receipt = _receipt_cache_put(artifact_sha, receipt)
                else:
                    if encoded is None:
                        encoded = artifact.read_bytes()
                        # Refresh memo from the bytes we actually parse.
                        digest = hashlib.sha256(encoded).hexdigest()
                        if digest != artifact_sha:
                            raise OptimizerWorkerError("r006 raw artifact bytes differ")
                        artifact_sha = digest
                        stat = artifact.stat()
                        _ARTIFACT_SHA_BY_PATH[str(artifact)] = (
                            stat.st_mtime_ns,
                            stat.st_size,
                            artifact_sha,
                        )
                    # Hydrate R008RAW1 companion when JSON stub has empty samples.
                    # Phase-5 stubs stay empty; cold_read_binary binds .r008raw.
                    receipt_payload = load_receipt_mapping_from_bytes(
                        encoded, artifact_path=artifact
                    )
                    parsed = R006ObjectiveReceipt.from_mapping(receipt_payload)
                    if in_tail:
                        verify_kwargs: dict[str, Any] = {
                            "expected_campaign_fingerprint": campaign,
                        }
                        sibling = artifact.with_suffix(".r008raw")
                        if (
                            is_binary_seal_receipt(parsed)
                            and sibling.is_file()
                            and not sibling.is_symlink()
                        ):
                            verify_kwargs["artifact_raw_bytes"] = sibling.read_bytes()
                        receipt = obj_mod.cold_read_verify(parsed, **verify_kwargs)
                    else:
                        # Hash chain (above) already reconfirmed this row's own
                        # stored fields are unchanged since it was last verified
                        # (when it was itself within the tail); trust the sealed
                        # receipt instead of re-deriving it from raw samples.
                        #
                        # BUG FIXED 2026-08-04 (found live, after ~25 min deployed):
                        # the receipt stored on disk always carries
                        # verification_state="builder_sealed" (cold_read_verify is
                        # what flips it to "verified_raw_artifact"), and
                        # `.trainable` requires exactly "verified_raw_artifact".
                        # Skipping cold_read_verify without also setting this
                        # field silently made every non-tail row untrainable --
                        # live campaigns were fitting the GP on only the tail
                        # `tail_rows` points instead of the full history, with no
                        # error raised. `replace()` only touches
                        # verification_state (excluded from the seal payload, see
                        # R006ObjectiveReceipt._seal_payload); validate_seal()
                        # re-confirms the receipt's own internal hash still
                        # matches, which is cheap (no raw-sample I/O).
                        receipt = replace(
                            parsed,
                            verification_state="verified_raw_artifact",
                        )
                        receipt.validate_seal()
                    # Slim + LRU *after* seal/cold_read; returned rows also
                    # hold the slim receipt so ask() doesn't retain ~18MB/row.
                    receipt = _receipt_cache_put(artifact_sha, receipt)
            except Exception as exc:  # noqa: BLE001 -- re-raised as worker error
                raise OptimizerWorkerError("fresh r006 raw artifact verification failed") from exc
            if (
                receipt.attempt_sequence != row.get("attempt_sequence")
                or receipt.execution_id != row.get("execution_id")
            ):
                raise OptimizerWorkerError("r006 raw artifact attempt/execution binding differs")
            normalized.append({**dict(row), "receipt": receipt, "point_key": row.get("point_key")})
        previous = str(row["row_sha256"])
    actual_ids = tuple((int(item["attempt_sequence"]), str(item["execution_id"])) for item in normalized)
    if requested_ids != actual_ids:
        raise OptimizerWorkerError("r006 artifact refs differ from fresh sidecar")
    return {
        "sidecar_path": str(sidecar),
        "sidecar_sha256": sidecar_sha,
        "campaign_fingerprint": campaign,
        "rows": normalized,
    }


__all__ = [
    "R008_ARTIFACT_BINDING_TAIL_ROWS",
    "R008_RECEIPT_CACHE_MAX_ENTRIES",
    "bounded_artifact_binding",
    "clear_receipt_cache",
    "receipt_cache_size",
]
