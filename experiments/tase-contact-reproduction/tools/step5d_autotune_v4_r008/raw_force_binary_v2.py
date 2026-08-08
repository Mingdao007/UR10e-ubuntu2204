"""R008RAW2: seal-minimal columns + audit suffix (Phase 5).

SEAL block (hashed for ``raw_bundle_digest``):
  path_time_s, filtered_normal_n, timestamp_s, seq×4

AUDIT block (replay only; not in seal digest):
  commanded_qdot, actual_qd, age×4

Dual-read: ``detect_and_decode`` handles R008RAW1 (full columns) and R008RAW2.
"""

from __future__ import annotations

import hashlib
import math
import struct
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_autotune_v4_r008.raw_force_binary import (
    MAGIC as MAGIC_V1,
    SOURCE_KEYS,
    VERSION as VERSION_V1,
    RawForceBinaryError,
    decode_samples as decode_samples_v1,
)

MAGIC_V2 = b"R008RAW2"
VERSION_V2 = 1
SAMPLES_FORMAT_V2 = "r008raw_v2"
PATH_STAGE = 25

_HEADER = struct.Struct("<8sIQ")  # magic, version, n


def _seal_region_size(n: int) -> int:
    # header + time + force + timestamp + 4×seq
    return _HEADER.size + n * 8 * 3 + n * 8 * 4


def encode_samples_v2(samples: Sequence[Mapping[str, Any]]) -> tuple[bytes, bytes, str]:
    """Encode to R008RAW2.

    Returns ``(full_file_bytes, seal_region_bytes, seal_sha256_hex)``.
    ``seal_region`` = header + SEAL columns (digest authority).
    """

    n = len(samples)
    if n <= 0:
        raise RawForceBinaryError("samples must be non-empty")

    path_time: list[float] = []
    force: list[float] = []
    timestamp: list[float] = []
    qdot: list[float] = []
    actual: list[float] = []
    seqs: dict[str, list[int]] = {key: [] for key in SOURCE_KEYS}
    ages: dict[str, list[float]] = {key: [] for key in SOURCE_KEYS}

    for index, sample in enumerate(samples):
        path_time.append(float(sample["path_time_s"]))
        force.append(float(sample["filtered_normal_n"]))
        ts = sample.get("timestamp_s")
        timestamp.append(math.nan if ts is None else float(ts))
        for name in ("path_phase", "stage", "path_stage"):
            value = int(sample.get(name, PATH_STAGE))
            if value != PATH_STAGE:
                raise RawForceBinaryError(f"sample {index} {name} must be {PATH_STAGE}")
        cq = sample.get("commanded_qdot")
        aq = sample.get("actual_qd")
        if cq is None or aq is None or len(cq) != 6 or len(aq) != 6:
            raise RawForceBinaryError(f"sample {index} missing qdot vectors")
        qdot.extend(float(x) for x in cq)
        actual.extend(float(x) for x in aq)
        sequences = sample["source_sequences"]
        age_map = sample["source_ages_s"]
        if set(sequences) != set(SOURCE_KEYS) or set(age_map) != set(SOURCE_KEYS):
            raise RawForceBinaryError(f"sample {index} source keys must be {SOURCE_KEYS}")
        for key in SOURCE_KEYS:
            seqs[key].append(int(sequences[key]))
            ages[key].append(float(age_map[key]))

    seal_parts: list[bytes] = [_HEADER.pack(MAGIC_V2, VERSION_V2, n)]
    seal_parts.append(struct.pack(f"<{n}d", *path_time))
    seal_parts.append(struct.pack(f"<{n}d", *force))
    seal_parts.append(struct.pack(f"<{n}d", *timestamp))
    for key in SOURCE_KEYS:
        seal_parts.append(struct.pack(f"<{n}q", *seqs[key]))
    seal_region = b"".join(seal_parts)

    audit_parts: list[bytes] = [
        struct.pack(f"<{n * 6}d", *qdot),
        struct.pack(f"<{n * 6}d", *actual),
    ]
    for key in SOURCE_KEYS:
        audit_parts.append(struct.pack(f"<{n}d", *ages[key]))
    full = seal_region + b"".join(audit_parts)
    digest = hashlib.sha256(seal_region).hexdigest()
    return full, seal_region, digest


def try_encode_samples_v2(samples: Sequence[Mapping[str, Any]]) -> tuple[bytes, bytes, str] | None:
    if not samples:
        return None
    try:
        return encode_samples_v2(samples)
    except (RawForceBinaryError, KeyError, TypeError, ValueError):
        return None


def decode_samples_v2(payload: bytes, *, include_audit: bool = True) -> list[dict[str, Any]]:
    """Decode R008RAW2; seal-only maps when ``include_audit`` is False."""

    if len(payload) < _HEADER.size:
        raise RawForceBinaryError("payload too short for header")
    magic, version, n = _HEADER.unpack_from(payload, 0)
    if magic != MAGIC_V2:
        raise RawForceBinaryError(f"bad magic {magic!r}")
    if version != VERSION_V2:
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
    seqs = {key: take(f"<{n}q") for key in SOURCE_KEYS}
    seal_end = offset
    if seal_end != _seal_region_size(n):
        raise RawForceBinaryError("seal region size mismatch")

    if include_audit:
        qdot = take(f"<{n * 6}d")
        actual = take(f"<{n * 6}d")
        ages = {key: take(f"<{n}d") for key in SOURCE_KEYS}
        if offset != len(payload):
            raise RawForceBinaryError(f"trailing bytes: {len(payload) - offset}")
    else:
        qdot = actual = None
        ages = None

    samples: list[dict[str, Any]] = []
    for i in range(n):
        ts = timestamp[i]
        row: dict[str, Any] = {
            "path_time_s": path_time[i],
            "filtered_normal_n": force[i],
            "timestamp_s": None if isinstance(ts, float) and math.isnan(ts) else float(ts),
            "path_phase": PATH_STAGE,
            "stage": PATH_STAGE,
            "path_stage": PATH_STAGE,
            "source_sequences": {key: int(seqs[key][i]) for key in SOURCE_KEYS},
        }
        if include_audit and qdot is not None and actual is not None and ages is not None:
            row["commanded_qdot"] = [qdot[i * 6 + j] for j in range(6)]
            row["actual_qd"] = [actual[i * 6 + j] for j in range(6)]
            row["source_ages_s"] = {key: float(ages[key][i]) for key in SOURCE_KEYS}
        else:
            # Placeholders so ForcePathSample.from_mapping can still run on cold FO path.
            row["commanded_qdot"] = [0.0] * 6
            row["actual_qd"] = [0.0] * 6
            row["source_ages_s"] = {key: 0.001 for key in SOURCE_KEYS}
        samples.append(row)
    return samples


def seal_region_of(payload: bytes) -> bytes:
    """Return the digest-authority prefix of a R008RAW2 file."""

    if len(payload) < _HEADER.size:
        raise RawForceBinaryError("payload too short")
    magic, version, n = _HEADER.unpack_from(payload, 0)
    if magic != MAGIC_V2 or version != VERSION_V2 or n <= 0:
        raise RawForceBinaryError("not a R008RAW2 payload")
    size = _seal_region_size(n)
    if len(payload) < size:
        raise RawForceBinaryError("payload shorter than seal region")
    return payload[:size]


def seal_sha256_of(payload: bytes) -> str:
    return hashlib.sha256(seal_region_of(payload)).hexdigest()


def detect_magic(payload: bytes) -> bytes | None:
    if len(payload) < 8:
        return None
    return payload[:8]


def detect_and_decode(payload: bytes, *, include_audit: bool = True) -> list[dict[str, Any]]:
    """Decode R008RAW2 or legacy R008RAW1."""

    magic = detect_magic(payload)
    if magic == MAGIC_V2:
        return decode_samples_v2(payload, include_audit=include_audit)
    if magic == MAGIC_V1:
        # RAW1 stores version as u32 after magic; reuse v1 decoder.
        return decode_samples_v1(payload)
    raise RawForceBinaryError(f"unknown r008raw magic {magic!r}")


def write_r008raw_v2(path: Path, samples: Sequence[Mapping[str, Any]]) -> tuple[str, bytes]:
    """Write R008RAW2; return (seal_sha256, full_bytes)."""

    full, _seal, digest = encode_samples_v2(samples)
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(full)
    tmp.replace(path)
    return digest, full


__all__ = [
    "MAGIC_V1",
    "MAGIC_V2",
    "PATH_STAGE",
    "SAMPLES_FORMAT_V2",
    "SOURCE_KEYS",
    "VERSION_V1",
    "VERSION_V2",
    "decode_samples_v2",
    "detect_and_decode",
    "detect_magic",
    "encode_samples_v2",
    "seal_region_of",
    "seal_sha256_of",
    "try_encode_samples_v2",
    "write_r008raw_v2",
]
