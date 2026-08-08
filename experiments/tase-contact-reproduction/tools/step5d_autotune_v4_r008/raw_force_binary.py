"""R008RAW1 binary sidecar for PATH force samples (seal-speed Phase 1).

Replaces the ~18MB JSON ``samples: [...]`` blob with column-major float64/int
arrays. Metadata (campaign / digests / bins) stays in a slim JSON receipt;
``raw_bundle_digest`` / ``builder_seal_sha256`` remain computed over the full
logical bundle (samples hydrated before ``from_mapping`` / cold verify).

Format (little-endian):
  magic[8]=b"R008RAW1"
  version:u32 = 1
  n:u64
  path_time_s:f64[n]
  filtered_normal_n:f64[n]
  timestamp_s:f64[n]          # NaN encodes JSON null
  path_phase:i32[n]
  stage:i32[n]
  path_stage:i32[n]
  commanded_qdot:f64[n*6]
  actual_qd:f64[n*6]
  seq_{kunwei,rtde,tp,writer}:i64[n] each
  age_{kunwei,rtde,tp,writer}:f64[n] each
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence

MAGIC = b"R008RAW1"
VERSION = 1
SOURCE_KEYS: tuple[str, ...] = ("kunwei", "rtde", "tp", "writer")
SAMPLES_FORMAT = "r008raw_v1"
PATH_STAGE = 25

_HEADER = struct.Struct("<8sIQ")  # magic, version, n


class RawForceBinaryError(ValueError):
    """R008RAW1 encode/decode failed."""


def encode_samples(samples: Sequence[Mapping[str, Any]]) -> bytes:
    """Encode PATH sample mappings to R008RAW1 bytes."""

    n = len(samples)
    if n <= 0:
        raise RawForceBinaryError("samples must be non-empty")

    path_time: list[float] = []
    force: list[float] = []
    timestamp: list[float] = []
    phase: list[int] = []
    stage: list[int] = []
    path_stage: list[int] = []
    qdot: list[float] = []
    actual: list[float] = []
    seqs: dict[str, list[int]] = {key: [] for key in SOURCE_KEYS}
    ages: dict[str, list[float]] = {key: [] for key in SOURCE_KEYS}

    for index, sample in enumerate(samples):
        path_time.append(float(sample["path_time_s"]))
        force.append(float(sample["filtered_normal_n"]))
        ts = sample.get("timestamp_s")
        timestamp.append(math.nan if ts is None else float(ts))
        for name, bucket in (
            ("path_phase", phase),
            ("stage", stage),
            ("path_stage", path_stage),
        ):
            value = int(sample.get(name, PATH_STAGE))
            if value != PATH_STAGE:
                raise RawForceBinaryError(f"sample {index} {name} must be {PATH_STAGE}")
            bucket.append(value)
        cq = sample.get("commanded_qdot")
        aq = sample.get("actual_qd")
        if cq is None or aq is None:
            raise RawForceBinaryError(f"sample {index} missing qdot vectors")
        if len(cq) != 6 or len(aq) != 6:
            raise RawForceBinaryError(f"sample {index} qdot length must be 6")
        qdot.extend(float(x) for x in cq)
        actual.extend(float(x) for x in aq)
        sequences = sample["source_sequences"]
        age_map = sample["source_ages_s"]
        if set(sequences) != set(SOURCE_KEYS) or set(age_map) != set(SOURCE_KEYS):
            raise RawForceBinaryError(f"sample {index} source keys must be {SOURCE_KEYS}")
        for key in SOURCE_KEYS:
            seqs[key].append(int(sequences[key]))
            ages[key].append(float(age_map[key]))

    parts: list[bytes] = [_HEADER.pack(MAGIC, VERSION, n)]
    parts.append(struct.pack(f"<{n}d", *path_time))
    parts.append(struct.pack(f"<{n}d", *force))
    parts.append(struct.pack(f"<{n}d", *timestamp))
    parts.append(struct.pack(f"<{n}i", *phase))
    parts.append(struct.pack(f"<{n}i", *stage))
    parts.append(struct.pack(f"<{n}i", *path_stage))
    parts.append(struct.pack(f"<{n * 6}d", *qdot))
    parts.append(struct.pack(f"<{n * 6}d", *actual))
    for key in SOURCE_KEYS:
        parts.append(struct.pack(f"<{n}q", *seqs[key]))
    for key in SOURCE_KEYS:
        parts.append(struct.pack(f"<{n}d", *ages[key]))
    return b"".join(parts)


def decode_samples(payload: bytes) -> list[dict[str, Any]]:
    """Decode R008RAW1 bytes to sample dicts compatible with ForcePathSample."""

    if len(payload) < _HEADER.size:
        raise RawForceBinaryError("payload too short for header")
    magic, version, n = _HEADER.unpack_from(payload, 0)
    if magic != MAGIC:
        raise RawForceBinaryError(f"bad magic {magic!r}")
    if version != VERSION:
        raise RawForceBinaryError(f"unsupported version {version}")
    if n <= 0:
        raise RawForceBinaryError("n_samples must be positive")

    offset = _HEADER.size

    def take(fmt: str) -> tuple[Any, ...]:
        nonlocal offset
        size = struct.calcsize(fmt)
        chunk = payload[offset : offset + size]
        if len(chunk) != size:
            raise RawForceBinaryError("payload truncated")
        offset += size
        return struct.unpack(fmt, chunk)

    path_time = take(f"<{n}d")
    force = take(f"<{n}d")
    timestamp = take(f"<{n}d")
    phase = take(f"<{n}i")
    stage = take(f"<{n}i")
    path_stage = take(f"<{n}i")
    qdot = take(f"<{n * 6}d")
    actual = take(f"<{n * 6}d")
    seqs = {key: take(f"<{n}q") for key in SOURCE_KEYS}
    ages = {key: take(f"<{n}d") for key in SOURCE_KEYS}
    if offset != len(payload):
        raise RawForceBinaryError(f"trailing bytes: {len(payload) - offset}")

    samples: list[dict[str, Any]] = []
    for i in range(n):
        ts = timestamp[i]
        samples.append(
            {
                "path_time_s": path_time[i],
                "filtered_normal_n": force[i],
                "timestamp_s": None if isinstance(ts, float) and math.isnan(ts) else float(ts),
                "path_phase": int(phase[i]),
                "stage": int(stage[i]),
                "path_stage": int(path_stage[i]),
                "commanded_qdot": [qdot[i * 6 + j] for j in range(6)],
                "actual_qd": [actual[i * 6 + j] for j in range(6)],
                "source_sequences": {key: int(seqs[key][i]) for key in SOURCE_KEYS},
                "source_ages_s": {key: float(ages[key][i]) for key in SOURCE_KEYS},
            }
        )
    return samples


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_r008raw(path: Path, samples: Sequence[Mapping[str, Any]]) -> str:
    """Write samples to ``path``; return SHA-256 hex of file bytes."""

    payload = encode_samples(samples)
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)
    return sha256_bytes(payload)


def read_r008raw(path: Path) -> list[dict[str, Any]]:
    return decode_samples(Path(path).read_bytes())


def try_encode_samples(samples: Sequence[Mapping[str, Any]]) -> bytes | None:
    """Return ``encode_samples`` result, or ``None`` when samples are incompatible."""

    if not samples:
        return None
    try:
        return encode_samples(samples)
    except (RawForceBinaryError, KeyError, TypeError, ValueError):
        return None


def samples_compatible(samples: Sequence[Mapping[str, Any]]) -> bool:
    """True when samples match R008RAW1 v1 column requirements."""

    return try_encode_samples(samples) is not None


def prepare_slim_receipt(
    receipt_dict: MutableMapping[str, Any],
    *,
    artifact_json_path: Path,
) -> bytes | None:
    """Slim ``receipt_dict`` samples in-memory; return R008RAW1 bytes to write.

    Returns ``None`` when samples are not R008RAW1-compatible (caller keeps
    full JSON). Does not touch the filesystem. Pointer is the sibling
    ``.r008raw`` only — metadata stays seal-identical.
    """

    del artifact_json_path  # naming convention only; no receipt mutation beyond samples
    raw_bundle = receipt_dict.get("raw_bundle")
    if not isinstance(raw_bundle, Mapping):
        return None
    samples = raw_bundle.get("samples")
    if not isinstance(samples, list):
        return None
    payload = try_encode_samples(samples)
    if payload is None:
        return None

    slim_bundle = dict(raw_bundle)
    slim_bundle["samples"] = []
    receipt_dict["raw_bundle"] = slim_bundle
    return payload


def write_raw_sidecar_bytes(path: Path, payload: bytes) -> None:
    """Atomically write pre-encoded R008RAW1 bytes."""

    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(payload)
    tmp.replace(path)


def split_receipt_for_disk(
    receipt_dict: MutableMapping[str, Any],
    *,
    artifact_json_path: Path,
) -> bool:
    """Write companion ``.r008raw`` and slim ``samples`` in ``receipt_dict``."""

    artifact_json_path = Path(artifact_json_path)
    payload = prepare_slim_receipt(receipt_dict, artifact_json_path=artifact_json_path)
    if payload is None:
        return False
    write_raw_sidecar_bytes(artifact_json_path.with_suffix(".r008raw"), payload)
    return True


def hydrate_receipt_payload(
    payload: Mapping[str, Any],
    *,
    artifact_path: Path,
) -> dict[str, Any]:
    """Reload samples from companion ``.r008raw`` when the JSON stub is slim.

    Phase-5 binary-native seals keep ``raw_bundle.samples == []`` in the seal
    payload; cold-read binds ``.r008raw`` separately and must not inject sample
    bodies back into the sealed stub.
    """

    from step5d_autotune_v4_r008.binary_seal import (
        R008_OBJECTIVE_RECEIPT_VERSION,
        R008_RAW_CODEC,
    )
    from step5d_autotune_v4_r008.raw_force_binary_v2 import (
        SAMPLES_FORMAT_V2,
        detect_and_decode,
    )

    out = dict(payload)
    raw_bundle = out.get("raw_bundle")
    if not isinstance(raw_bundle, Mapping):
        return out
    bundle = dict(raw_bundle)
    samples = bundle.get("samples")
    if isinstance(samples, list) and samples:
        out["raw_bundle"] = bundle
        return out

    # Phase 5: leave seal stub empty; verify path reads sibling bytes.
    if (
        out.get("version") == R008_OBJECTIVE_RECEIPT_VERSION
        or bundle.get("samples_format") == SAMPLES_FORMAT_V2
        or bundle.get("raw_codec") == R008_RAW_CODEC
    ):
        out["raw_bundle"] = bundle
        return out

    artifact_path = Path(artifact_path)
    sibling = artifact_path.with_suffix(".r008raw")
    sample_count = out.get("sample_count")
    if sample_count is None and isinstance(bundle.get("sample_count"), int):
        sample_count = bundle.get("sample_count")
    if (
        isinstance(samples, list)
        and not samples
        and isinstance(sample_count, int)
        and sample_count > 0
        and sibling.is_file()
        and not sibling.is_symlink()
    ):
        decoded = detect_and_decode(sibling.read_bytes(), include_audit=True)
        if len(decoded) != sample_count:
            raise RawForceBinaryError(
                f"r008raw n_samples {len(decoded)} != receipt sample_count {sample_count}"
            )
        bundle["samples"] = decoded
    out["raw_bundle"] = bundle
    return out


def load_receipt_mapping(artifact_path: Path) -> dict[str, Any]:
    """``json.loads`` artifact then hydrate binary samples when present."""

    artifact_path = Path(artifact_path)
    if artifact_path.is_symlink() or not artifact_path.is_file():
        raise RawForceBinaryError("artifact is not a regular file")
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RawForceBinaryError("artifact is not a JSON object")
    return hydrate_receipt_payload(payload, artifact_path=artifact_path)


def load_receipt_mapping_from_bytes(
    encoded: bytes,
    *,
    artifact_path: Path,
) -> dict[str, Any]:
    payload = json.loads(encoded.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RawForceBinaryError("artifact is not a JSON object")
    return hydrate_receipt_payload(payload, artifact_path=Path(artifact_path))


__all__ = [
    "MAGIC",
    "PATH_STAGE",
    "SAMPLES_FORMAT",
    "SOURCE_KEYS",
    "VERSION",
    "RawForceBinaryError",
    "decode_samples",
    "encode_samples",
    "hydrate_receipt_payload",
    "load_receipt_mapping",
    "load_receipt_mapping_from_bytes",
    "prepare_slim_receipt",
    "read_r008raw",
    "samples_compatible",
    "sha256_bytes",
    "split_receipt_for_disk",
    "try_encode_samples",
    "write_r008raw",
    "write_raw_sidecar_bytes",
]
