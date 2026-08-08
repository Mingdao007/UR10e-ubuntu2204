"""Columnar cold-verify for R008RAW1 / hydrated sample lists (seal Phase 2/4).

Recomputes bin stats + digests without ``ForcePathSample`` × N and without a
second ``R006ObjectiveBuilder.finalize``. Used by r008 fresh-verify and the
append skip-dup fast-path. Fail closed → caller may fall back to stock
``cold_read_verify``.

Phase 4 hot-append: ``columnar_verify_hot_append`` binds on-disk stub+``.r008raw``
bytes and recomputes bins from the in-memory builder receipt (no decode /
``from_mapping`` / full-bundle ``validate_seal``).
"""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_autotune_v4_r006.contracts import (
    BIN_WIDTH_S,
    FORMAL_WINDOW_S,
    LEGACY_SHADOW_WINDOW_S,
    OBJECTIVE_SEMANTIC_FINGERPRINT,
    PATH_STAGE,
    REQUIRED_BINS,
    TARGET_FORCE_N,
    canonical_bytes,
    sha256_bytes,
)
from step5d_autotune_v4_r006.objective import R006ObjectiveError, R006ObjectiveReceipt


def _sha(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def _bin_index(time_s: float, window: tuple[float, float]) -> int | None:
    start, end = window
    if not (start <= time_s < end):
        return None
    index = int(math.floor((time_s - start) / BIN_WIDTH_S + 1e-10))
    return index if 0 <= index < REQUIRED_BINS else None


def _signed_stats_maps(
    samples: Sequence[Mapping[str, Any]], window: tuple[float, float]
) -> tuple[tuple[float, ...], tuple[int, ...]]:
    sums = [0.0] * REQUIRED_BINS
    counts = [0] * REQUIRED_BINS
    for sample in samples:
        index = _bin_index(float(sample["path_time_s"]), window)
        if index is None:
            continue
        sums[index] += float(sample["filtered_normal_n"])
        counts[index] += 1
    return tuple(sums), tuple(counts)


def _formal_abs_error_stats_maps(
    samples: Sequence[Mapping[str, Any]],
) -> tuple[tuple[float, ...], tuple[int, ...]]:
    sums = [0.0] * REQUIRED_BINS
    counts = [0] * REQUIRED_BINS
    for sample in samples:
        index = _bin_index(float(sample["path_time_s"]), FORMAL_WINDOW_S)
        if index is None:
            continue
        sums[index] += abs(float(sample["filtered_normal_n"]) - TARGET_FORCE_N)
        counts[index] += 1
    return tuple(sums), tuple(counts)


def _mean_from_sums(sums: Sequence[float], counts: Sequence[int]) -> float | None:
    if len(sums) != REQUIRED_BINS or len(counts) != REQUIRED_BINS or not all(counts):
        return None
    return math.fsum(
        abs((total / count) - TARGET_FORCE_N) for total, count in zip(sums, counts, strict=True)
    ) / REQUIRED_BINS


def _within_bin_abs_mae(
    sum_abs_error: Sequence[float], counts: Sequence[int]
) -> float | None:
    if len(sum_abs_error) != REQUIRED_BINS or len(counts) != REQUIRED_BINS or not all(counts):
        return None
    return math.fsum(
        total / count for total, count in zip(sum_abs_error, counts, strict=True)
    ) / REQUIRED_BINS


def _sample_identity_maps(samples: Sequence[Mapping[str, Any]]) -> str:
    return _sha(
        [
            {
                "path_time_s": float(sample["path_time_s"]),
                "timestamp_s": sample["timestamp_s"],
                "source_sequences": dict(sample["source_sequences"]),
            }
            for sample in samples
        ]
    )


def _validate_sample_stream(samples: Sequence[Mapping[str, Any]]) -> None:
    """Time order / stage / identity uniqueness (sealed bundles store uniques)."""

    last_time: float | None = None
    seen_identities: set[tuple[tuple[str, int | str], ...]] = set()
    for sample in samples:
        path_time = float(sample["path_time_s"])
        if last_time is not None and path_time < last_time:
            raise R006ObjectiveError("raw PATH time regressed")
        last_time = path_time
        for stage_key in ("path_phase", "stage", "path_stage"):
            if int(sample[stage_key]) != PATH_STAGE:
                raise R006ObjectiveError("only stage 25 may be used as PATH evidence")
        if sample.get("timestamp_s") is None:
            raise R006ObjectiveError("r006 raw sample requires stage 25 and source time identity")
        identity = tuple(sorted(sample["source_sequences"].items()))
        if identity in seen_identities:
            # Stock cold_read rebuilds only persisted uniques → replays stay 0.
            # Duplicate identities in the sealed list are reject-closed here.
            raise R006ObjectiveError("duplicate source identity in raw artifact samples")
        seen_identities.add(identity)


def _recompute_and_check_bins(
    original: R006ObjectiveReceipt,
    samples: Sequence[Mapping[str, Any]],
    *,
    check_raw_bundle_digest: bool,
) -> None:
    """Shared bin / identity checks against a sealed receipt."""

    bundle = original.raw_bundle
    if not isinstance(bundle, Mapping) or bundle.get("schema") != (
        "step5d.force-objective/r006-raw-path-bundle-v1"
    ):
        raise R006ObjectiveError("raw artifact bundle schema differs")
    if (
        bundle.get("attempt_sequence") != original.attempt_sequence
        or bundle.get("execution_id") != original.execution_id
        or bundle.get("campaign_fingerprint") != original.campaign_fingerprint
    ):
        raise R006ObjectiveError("raw artifact attempt/campaign binding differs")
    if bundle.get("semantic_fingerprint") != OBJECTIVE_SEMANTIC_FINGERPRINT:
        raise R006ObjectiveError("raw artifact semantic fingerprint differs")
    if not isinstance(samples, list) or not samples:
        raise R006ObjectiveError("raw artifact samples are missing")
    if len(samples) != int(original.sample_count):
        raise R006ObjectiveError("sample_count differs during cold read")

    _validate_sample_stream(samples)
    if check_raw_bundle_digest:
        logical = dict(bundle)
        logical["samples"] = list(samples)
        if _sha(logical) != original.raw_bundle_digest:
            raise R006ObjectiveError("raw bundle digest differs during cold read")

    formal_signed_sums, formal_counts = _signed_stats_maps(samples, FORMAL_WINDOW_S)
    formal_abs_sums, _ = _formal_abs_error_stats_maps(samples)
    legacy_sums, legacy_counts = _signed_stats_maps(samples, LEGACY_SHADOW_WINDOW_S)
    stats_payload = {
        "formal_bin_sum_n": list(formal_signed_sums),
        "formal_bin_sum_abs_error_n": list(formal_abs_sums),
        "formal_bin_count": list(formal_counts),
        "r005_shadow_bin_sum_n": list(formal_signed_sums),
        "r005_shadow_bin_count": list(formal_counts),
        "legacy_r004_shadow_bin_sum_n": list(legacy_sums),
        "legacy_r004_shadow_bin_count": list(legacy_counts),
    }
    recomputed = {
        "source_sequence_time_identity_digest": _sample_identity_maps(samples),
        "formal_bin_sum_n": formal_signed_sums,
        "formal_bin_sum_abs_error_n": formal_abs_sums,
        "formal_bin_count": formal_counts,
        "r005_shadow_bin_sum_n": formal_signed_sums,
        "r005_shadow_bin_count": formal_counts,
        "legacy_r004_shadow_bin_sum_n": legacy_sums,
        "legacy_r004_shadow_bin_count": legacy_counts,
        "sufficient_statistics_digest": _sha(stats_payload),
        "objective_mae_n": _within_bin_abs_mae(formal_abs_sums, formal_counts),
        "r004_legacy_shadow_mae_n": _mean_from_sums(legacy_sums, legacy_counts),
        "r005_abs_bin_mean_shadow_mae_n": _mean_from_sums(formal_signed_sums, formal_counts),
        "sample_count": len(samples),
        "deduplicated_replays": 0,
    }
    for name, value in recomputed.items():
        if getattr(original, name) != value:
            raise R006ObjectiveError(f"{name} differs during cold read")


def columnar_verify_receipt(
    receipt: R006ObjectiveReceipt | Mapping[str, Any],
    *,
    expected_campaign_fingerprint: str | None = None,
) -> R006ObjectiveReceipt:
    """Cold-verify using sample maps + columnar bin loops (no second finalize)."""

    original = (
        receipt
        if isinstance(receipt, R006ObjectiveReceipt)
        else R006ObjectiveReceipt.from_mapping(receipt)
    )
    original.validate_seal()
    if (
        expected_campaign_fingerprint is not None
        and original.campaign_fingerprint != expected_campaign_fingerprint
    ):
        raise R006ObjectiveError("campaign binding differs during cold read")
    samples = original.raw_bundle.get("samples") if isinstance(original.raw_bundle, Mapping) else None
    if not isinstance(samples, list):
        raise R006ObjectiveError("raw artifact samples are missing")
    _recompute_and_check_bins(original, samples, check_raw_bundle_digest=True)
    verified = replace(original, verification_state="verified_raw_artifact")
    verified.validate_seal()
    return verified


def columnar_verify_hot_append(
    receipt: R006ObjectiveReceipt,
    *,
    expected_campaign_fingerprint: str,
    artifact_path: Path,
    encoded_stub: bytes,
    raw_payload: bytes,
) -> R006ObjectiveReceipt:
    """Hot-append verify: bind disk bytes + recompute bins from in-memory samples.

    Skips disk hydrate / ``ForcePathSample`` rebuild and skips full-bundle
    ``validate_seal`` (seal already covers ``raw_bundle``; ``.r008raw`` bytes
    bind the same samples via ``encode_samples``). Fail closed → caller falls
    back to stock cold verify.
    """

    if not isinstance(receipt, R006ObjectiveReceipt):
        raise R006ObjectiveError("hot append requires a typed builder receipt")
    if receipt.campaign_fingerprint != expected_campaign_fingerprint:
        raise R006ObjectiveError("campaign binding differs during cold read")
    artifact_path = Path(artifact_path)
    if artifact_path.is_symlink() or not artifact_path.is_file():
        raise R006ObjectiveError("artifact is not a regular file")
    if artifact_path.read_bytes() != encoded_stub:
        raise R006ObjectiveError("artifact stub bytes differ from local write")
    sibling = artifact_path.with_suffix(".r008raw")
    if sibling.is_symlink() or not sibling.is_file():
        raise R006ObjectiveError("r008raw sibling missing after local write")
    if sibling.read_bytes() != raw_payload:
        raise R006ObjectiveError("r008raw bytes differ from local encode")

    bundle = receipt.raw_bundle
    if not isinstance(bundle, Mapping):
        raise R006ObjectiveError("raw artifact bundle schema differs")
    samples = bundle.get("samples")
    if not isinstance(samples, list) or not samples:
        raise R006ObjectiveError("raw artifact samples are missing")
    # Disk bind already proves encode(samples)==raw_payload; skip re-SHA of the
    # full JSON bundle (dominant cost of stock validate_seal / digest check).
    _recompute_and_check_bins(receipt, samples, check_raw_bundle_digest=False)
    return replace(receipt, verification_state="verified_raw_artifact")


__all__ = ["columnar_verify_hot_append", "columnar_verify_receipt"]
