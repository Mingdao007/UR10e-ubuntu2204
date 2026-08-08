"""Columnar ForceObjective rebuild from sample maps (seal Phase 3 ledger path).

Recomputes the same bins / digests ``ForceObjectiveBuilder.finalize`` commits,
without ``ForcePathSample.from_mapping`` × N when the stream is well-formed.
Fail closed: callers may fall back to stock ``ForceObjectiveBuilder``.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from typing import Any, Mapping, Sequence

from step5d_force_objective import (
    BIN_WIDTH_S,
    FORCE_OBJECTIVE_RECEIPT_VERSION,
    FORCE_OBJECTIVE_SCHEMA,
    FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
    FORCE_OBJECTIVE_VERSION,
    FORMAL_END_S,
    FORMAL_START_S,
    LEGACY_END_S,
    LEGACY_START_S,
    MAX_SOURCE_AGE_S,
    PATH_STAGE,
    REQUIRED_BINS,
    TARGET_FORCE_N,
    ForceObjective,
    ForceObjectiveError,
)


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bin(path_time_s: float, start_s: float, end_s: float) -> int | None:
    if path_time_s < start_s or path_time_s >= end_s:
        return None
    index = math.floor((path_time_s - start_s) / BIN_WIDTH_S + 1e-9)
    return index if 0 <= index < REQUIRED_BINS else None


def _identity_digest(sequences: Mapping[str, Any]) -> str:
    return _sha256(_canonical({"source_sequences": dict(sequences)}))


def _payload_digest(sample: Mapping[str, Any]) -> str:
    return _sha256(
        _canonical(
            {
                "path_time_s": float(sample["path_time_s"]),
                "path_phase": int(sample["path_phase"]),
                "filtered_normal_n": float(sample["filtered_normal_n"]),
                "source_sequences": dict(sample["source_sequences"]),
                "source_ages_s": dict(sample["source_ages_s"]),
                "commanded_qdot": sample["commanded_qdot"],
                "actual_qd": sample["actual_qd"],
                "timestamp_s": sample["timestamp_s"],
            }
        )
    )


def _validate_sample(sample: Mapping[str, Any], index: int) -> dict[str, Any]:
    required = {
        "path_time_s",
        "path_phase",
        "stage",
        "path_stage",
        "filtered_normal_n",
        "source_sequences",
        "source_ages_s",
        "commanded_qdot",
        "actual_qd",
        "timestamp_s",
    }
    if set(sample) != required:
        raise ForceObjectiveError(f"sample {index} fields differ")
    path_time = float(sample["path_time_s"])
    if not math.isfinite(path_time) or path_time < 0.0:
        raise ForceObjectiveError(f"sample {index} PATH time invalid")
    for name in ("path_phase", "stage", "path_stage"):
        if int(sample[name]) != PATH_STAGE:
            raise ForceObjectiveError(f"sample {index} {name} must be {PATH_STAGE}")
    force = float(sample["filtered_normal_n"])
    if not math.isfinite(force):
        raise ForceObjectiveError(f"sample {index} force nonfinite")
    sequences = sample["source_sequences"]
    ages = sample["source_ages_s"]
    if not isinstance(sequences, Mapping) or not sequences:
        raise ForceObjectiveError(f"sample {index} source_sequences invalid")
    if not isinstance(ages, Mapping) or set(sequences) != set(ages):
        raise ForceObjectiveError(f"sample {index} source bindings differ")
    for key, age in ages.items():
        age_f = float(age)
        if age_f < 0.0 or age_f > MAX_SOURCE_AGE_S:
            raise ForceObjectiveError(f"sample {index} source age {key} is stale")
    cq = sample["commanded_qdot"]
    aq = sample["actual_qd"]
    if cq is None or aq is None:
        raise ForceObjectiveError(f"sample {index} missing qdot vectors")
    if len(cq) != 6 or len(aq) != 6:
        raise ForceObjectiveError(f"sample {index} qdot length must be 6")
    ts = sample["timestamp_s"]
    if ts is not None and not math.isfinite(float(ts)):
        raise ForceObjectiveError(f"sample {index} timestamp invalid")
    return {
        "path_time_s": path_time,
        "path_phase": PATH_STAGE,
        "stage": PATH_STAGE,
        "path_stage": PATH_STAGE,
        "filtered_normal_n": force,
        "source_sequences": dict(sequences),
        "source_ages_s": {key: float(ages[key]) for key in sequences},
        "commanded_qdot": [float(x) for x in cq],
        "actual_qd": [float(x) for x in aq],
        "timestamp_s": None if ts is None else float(ts),
    }


def build_force_objective_from_sample_maps(
    samples: Sequence[Mapping[str, Any]],
    *,
    provenance: str = "raw_path_evidence",
    seal_block_sha256: str | None = None,
) -> ForceObjective:
    """Rebuild ForceObjective from PATH sample mappings (ledger artifact path).

    When ``seal_block_sha256`` is set (R008RAW2), ``raw_evidence_digest`` binds
    that seal-block digest instead of the per-sample JSON ``payload_digest`` chain.
    """

    formal: dict[int, list[float]] = {}
    legacy: dict[int, list[float]] = {}
    retained: dict[tuple[tuple[str, Any], ...], dict[str, Any]] = {}
    last_path_time: float | None = None
    replays = 0

    for index, raw in enumerate(samples):
        sample = _validate_sample(raw, index)
        identity_key = tuple(sorted(sample["source_sequences"].items()))
        prior = retained.get(identity_key)
        if prior is not None:
            if _payload_digest(prior) != _payload_digest(sample):
                raise ForceObjectiveError("sample identity was reused with a conflicting payload")
            replays += 1
            continue
        if last_path_time is not None and sample["path_time_s"] < last_path_time:
            raise ForceObjectiveError("PATH-relative time regressed")
        last_path_time = sample["path_time_s"]
        retained[identity_key] = sample
        formal_index = _bin(sample["path_time_s"], FORMAL_START_S, FORMAL_END_S)
        if formal_index is not None:
            formal.setdefault(formal_index, []).append(sample["filtered_normal_n"])
        legacy_index = _bin(sample["path_time_s"], LEGACY_START_S, LEGACY_END_S)
        if legacy_index is not None:
            legacy.setdefault(legacy_index, []).append(sample["filtered_normal_n"])

    identities = {
        _identity_digest(sample["source_sequences"]): _payload_digest(sample)
        for sample in retained.values()
    }
    identity_ids = tuple(sorted(identities))
    identity_digest = _sha256(_canonical(identity_ids))
    bitmap = "".join("1" if index in formal else "0" for index in range(REQUIRED_BINS))

    def _mae(bins: Mapping[int, list[float]]) -> float | None:
        if len(bins) != REQUIRED_BINS or any(not values for values in bins.values()):
            return None
        return statistics.fmean(
            abs(statistics.fmean(values) - TARGET_FORCE_N)
            for index in range(REQUIRED_BINS)
            for values in (bins.get(index, []),)
        )

    v2 = _mae(formal)
    legacy_mae = _mae(legacy)
    formal_sums = tuple(math.fsum(formal.get(index, ())) for index in range(REQUIRED_BINS))
    formal_counts = tuple(len(formal.get(index, ())) for index in range(REQUIRED_BINS))
    legacy_sums = tuple(math.fsum(legacy.get(index, ())) for index in range(REQUIRED_BINS))
    legacy_counts = tuple(len(legacy.get(index, ())) for index in range(REQUIRED_BINS))
    stats_payload = {
        "receipt_version": FORCE_OBJECTIVE_RECEIPT_VERSION,
        "semantic_fingerprint": FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
        "formal_bin_sum_n": list(formal_sums),
        "formal_bin_count": list(formal_counts),
        "legacy_bin_sum_n": list(legacy_sums),
        "legacy_bin_count": list(legacy_counts),
    }
    stats_digest = _sha256(_canonical(stats_payload))
    if seal_block_sha256 is not None:
        if (
            not isinstance(seal_block_sha256, str)
            or len(seal_block_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in seal_block_sha256)
        ):
            raise ForceObjectiveError("seal_block_sha256 is not a sha256 hex digest")
        raw_evidence_digest = seal_block_sha256
    else:
        raw_evidence_digest = _sha256(
            _canonical(
                [
                    {"identity": identity, "payload_digest": identities[identity]}
                    for identity in identity_ids
                ]
            )
        )
    receipt = {
        "receipt_version": FORCE_OBJECTIVE_RECEIPT_VERSION,
        "semantic_fingerprint": FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
        "raw_evidence_digest": raw_evidence_digest,
        "sufficient_statistics_digest": stats_digest,
        "sample_identity_digest": identity_digest,
        "sample_identity_count": len(identity_ids),
        "sample_count": len(retained),
        "deduplicated_replays": replays,
        "formal_bin_sum_n": list(formal_sums),
        "formal_bin_count": list(formal_counts),
        "legacy_bin_sum_n": list(legacy_sums),
        "legacy_bin_count": list(legacy_counts),
        "provenance": provenance,
    }
    builder_seal = _sha256(_canonical(receipt))
    formal_ids = tuple(sorted(formal))
    legacy_ids = tuple(sorted(legacy))
    return ForceObjective(
        schema=FORCE_OBJECTIVE_SCHEMA,
        version=FORCE_OBJECTIVE_VERSION,
        target_force_n=TARGET_FORCE_N,
        formal_window_s=(FORMAL_START_S, FORMAL_END_S),
        legacy_window_s=(LEGACY_START_S, LEGACY_END_S),
        bin_width_s=BIN_WIDTH_S,
        required_bins=REQUIRED_BINS,
        complete_bins=len(formal_ids),
        coverage_bitmap=bitmap,
        formal_bin_ids=formal_ids,
        legacy_bin_ids=legacy_ids,
        sample_identity_digest=identity_digest,
        sample_identity_ids=(),
        sample_count=len(retained),
        deduplicated_replays=replays,
        v2_mae_n=v2,
        legacy_mae_n=legacy_mae,
        delta_n=None if v2 is None or legacy_mae is None else legacy_mae - v2,
        provenance=provenance,
        legacy_complete_bins=len(legacy_ids),
        semantic_fingerprint=FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
        raw_evidence_digest=raw_evidence_digest,
        sufficient_statistics_digest=stats_digest,
        builder_seal_sha256=builder_seal,
        formal_bin_sum_n=formal_sums,
        formal_bin_count=formal_counts,
        legacy_bin_sum_n=legacy_sums,
        legacy_bin_count=legacy_counts,
        sample_identity_count=len(identity_ids),
    )


__all__ = ["build_force_objective_from_sample_maps"]
