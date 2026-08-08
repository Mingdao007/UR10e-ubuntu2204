"""r005 ledger raw-artifact R008RAW2 overlay (seal Phase 5).

Slim JSON ``samples: []`` + sibling ``.r008raw`` (R008RAW2 seal+audit);
dual-read legacy full JSON / R008RAW1. Fresh verify hydrates + ForceObjective
columnar; RAW2 binds ``raw_evidence_digest`` to the seal-block SHA.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_autotune_v4_r005.contracts import canonical_bytes, sha256_bytes
from step5d_autotune_v4_r005.observations import (
    RAW_ARTIFACT_RECEIPT_VERSION,
    RAW_ARTIFACT_SCHEMA,
    ObservationError,
    ObservationRecord,
    _canonical,
    _expected_receipt,
    _sha256,
)
from step5d_autotune_v4_r008.force_objective_columnar import (
    build_force_objective_from_sample_maps,
)
from step5d_autotune_v4_r008.raw_force_binary import (
    RawForceBinaryError,
    write_raw_sidecar_bytes,
)
from step5d_autotune_v4_r008.raw_force_binary_v2 import (
    MAGIC_V2,
    detect_and_decode,
    detect_magic,
    seal_sha256_of,
    try_encode_samples_v2,
)
from step5d_force_objective import (
    FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
    ForceObjectiveBuilder,
    ForceObjectiveError,
    ForcePathSample,
)

# Hot append: typed samples just written, keyed by resolved artifact path.
# Cold resume / foreign verify leaves this empty and hydrates from disk.
_PENDING_TYPED_SAMPLES: dict[str, tuple[Any, ...]] = {}


def hydrate_ledger_artifact(
    artifact: Mapping[str, Any],
    *,
    artifact_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (artifact_dict, samples) hydrating ``.r008raw`` when samples empty."""

    out = dict(artifact)
    samples = out.get("samples")
    if isinstance(samples, list) and samples:
        return out, [dict(item) for item in samples]
    if not isinstance(samples, list):
        raise ObservationError("raw artifact samples are not a list")

    sibling = Path(artifact_path).with_suffix(".r008raw")
    if samples == [] and sibling.is_file() and not sibling.is_symlink():
        try:
            decoded = detect_and_decode(sibling.read_bytes(), include_audit=True)
        except RawForceBinaryError as exc:
            raise ObservationError(f"r008raw hydrate failed: {exc}") from exc
        out["samples"] = decoded
        return out, decoded
    return out, [dict(item) for item in samples]


def write_ledger_artifact_bytes(
    ledger: Any,
    record: ObservationRecord,
    execution_id: str,
) -> tuple[str, bytes, str, int]:
    """Write slim+``.r008raw`` when R008RAW2-compatible; else full JSON (legacy)."""

    identity = {
        "schema": RAW_ARTIFACT_SCHEMA,
        "campaign_fingerprint": record.campaign_fingerprint,
        "epoch": record.epoch,
        "attempt_sequence": record.attempt_sequence,
        "execution_id": execution_id,
        "candidate_uid": record.candidate_uid,
    }
    filename = _sha256(_canonical(identity, "raw artifact identity")) + ".json"
    relative_path = f"{ledger._artifact_prefix}/{filename}"
    target = ledger._artifact_path(relative_path)
    sample_dicts = [sample.as_dict() for sample in record.raw_path_samples]
    encoded_v2 = try_encode_samples_v2(sample_dicts)
    raw_payload = encoded_v2[0] if encoded_v2 is not None else None
    use_binary = raw_payload is not None
    artifact = {
        "schema": RAW_ARTIFACT_SCHEMA,
        "receipt_version": RAW_ARTIFACT_RECEIPT_VERSION,
        "semantic_fingerprint": FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
        "campaign_fingerprint": record.campaign_fingerprint,
        "epoch": record.epoch,
        "attempt_sequence": record.attempt_sequence,
        "kind": record.kind,
        "execution_id": execution_id,
        "candidate_uid": record.candidate_uid,
        "samples": [] if use_binary else sample_dicts,
    }
    encoded = _canonical(artifact, "raw artifact") + b"\n"
    byte_sha = _sha256(encoded)
    sibling = target.with_suffix(".r008raw")

    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file():
            raise ObservationError("raw artifact target is not a regular immutable file")
        if target.read_bytes() != encoded:
            raise ObservationError("raw artifact identity already has different bytes")
        if use_binary:
            if (
                sibling.is_symlink()
                or not sibling.is_file()
                or sibling.read_bytes() != raw_payload
            ):
                raise ObservationError("raw artifact r008raw sibling differs")
        _PENDING_TYPED_SAMPLES[str(target.resolve())] = tuple(record.raw_path_samples)
        return relative_path, encoded, byte_sha, len(encoded)

    if use_binary and raw_payload is not None:
        write_raw_sidecar_bytes(sibling, raw_payload)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".raw-", suffix=".tmp", dir=ledger.artifact_root
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        ledger._fsync_dir(ledger.artifact_root)
        ledger._fsync_dir(ledger.path.parent)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        if use_binary:
            try:
                sibling.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    _PENDING_TYPED_SAMPLES[str(target.resolve())] = tuple(record.raw_path_samples)
    return relative_path, encoded, byte_sha, len(encoded)


def _rebuild_objective(
    samples: Sequence[Mapping[str, Any]],
    *,
    seal_block_sha256: str | None = None,
) -> Any:
    try:
        return build_force_objective_from_sample_maps(
            samples, seal_block_sha256=seal_block_sha256
        )
    except Exception:
        if seal_block_sha256 is not None:
            raise
        builder = ForceObjectiveBuilder()
        for payload in samples:
            builder.add(ForcePathSample.from_mapping(payload))
        return builder.finalize(provenance="raw_path_evidence")


def _rebuild_objective_typed(
    samples: Sequence[Any],
    *,
    seal_block_sha256: str | None = None,
) -> Any:
    if seal_block_sha256 is not None:
        return build_force_objective_from_sample_maps(
            [sample.as_dict() for sample in samples],
            seal_block_sha256=seal_block_sha256,
        )
    builder = ForceObjectiveBuilder()
    builder.extend(samples)
    return builder.finalize(provenance="raw_path_evidence")


def _sibling_seal_digest(artifact_path: Path) -> str | None:
    sibling = Path(artifact_path).with_suffix(".r008raw")
    if not sibling.is_file() or sibling.is_symlink():
        return None
    payload = sibling.read_bytes()
    if detect_magic(payload) != MAGIC_V2:
        return None
    return seal_sha256_of(payload)


def verify_ledger_artifact_fresh(path: Path) -> dict[str, Any]:
    """In-process r005 artifact fresh verify (hydrate + columnar / stock)."""

    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ObservationError("artifact is not a regular file")
    raw_bytes = path.read_bytes()
    byte_sha = sha256_bytes(raw_bytes)
    byte_size = len(raw_bytes)
    try:
        artifact = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ObservationError("raw artifact is not valid JSON") from exc
    if not isinstance(artifact, dict):
        raise ObservationError("raw artifact is not an object")
    required = {
        "schema",
        "receipt_version",
        "semantic_fingerprint",
        "campaign_fingerprint",
        "epoch",
        "attempt_sequence",
        "kind",
        "execution_id",
        "candidate_uid",
        "samples",
    }
    if set(artifact) != required or artifact["schema"] != RAW_ARTIFACT_SCHEMA:
        raise ObservationError("raw artifact fields differ")
    if (
        artifact["receipt_version"] != RAW_ARTIFACT_RECEIPT_VERSION
        or artifact["semantic_fingerprint"] != FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT
    ):
        raise ObservationError("raw artifact semantic/version differs")
    if not isinstance(artifact["campaign_fingerprint"], str) or not artifact["campaign_fingerprint"]:
        raise ObservationError("raw artifact campaign is invalid")
    if (
        not isinstance(artifact["epoch"], int)
        or isinstance(artifact["epoch"], bool)
        or artifact["epoch"] <= 0
    ):
        raise ObservationError("raw artifact epoch is invalid")
    if (
        not isinstance(artifact["attempt_sequence"], int)
        or isinstance(artifact["attempt_sequence"], bool)
        or artifact["attempt_sequence"] <= 0
    ):
        raise ObservationError("raw artifact sequence is invalid")
    for key in ("kind", "execution_id", "candidate_uid"):
        if not isinstance(artifact[key], str) or not artifact[key]:
            raise ObservationError("raw artifact identity is invalid")

    pending_key = str(path.resolve())
    typed = _PENDING_TYPED_SAMPLES.pop(pending_key, None)
    seal_digest = _sibling_seal_digest(path)
    try:
        if typed is not None:
            recomputed = _rebuild_objective_typed(typed, seal_block_sha256=seal_digest)
            sample_count = len(typed)
        else:
            _, samples = hydrate_ledger_artifact(artifact, artifact_path=path)
            recomputed = _rebuild_objective(samples, seal_block_sha256=seal_digest)
            sample_count = len(samples)
        objective_payload = recomputed.as_dict()
    except (ForceObjectiveError, TypeError, ValueError, KeyError) as exc:
        raise ObservationError(f"fresh artifact objective rebuild failed: {exc}") from exc

    objective_sha = sha256_bytes(canonical_bytes(objective_payload))
    receipt = _expected_receipt(
        byte_sha256=byte_sha,
        byte_size=byte_size,
        objective_sha256=objective_sha,
        campaign_fingerprint=str(artifact["campaign_fingerprint"]),
        epoch=int(artifact["epoch"]),
        attempt_sequence=int(artifact["attempt_sequence"]),
        execution_id=str(artifact["execution_id"]),
        candidate_uid=str(artifact["candidate_uid"]),
        sample_count=sample_count,
    )
    return {
        "objective": objective_payload,
        "objective_sha256": objective_sha,
        "receipt": receipt,
        "receipt_sha256": sha256_bytes(canonical_bytes(receipt)),
        "byte_sha256": byte_sha,
        "byte_size": byte_size,
    }


__all__ = [
    "hydrate_ledger_artifact",
    "verify_ledger_artifact_fresh",
    "write_ledger_artifact_bytes",
]
