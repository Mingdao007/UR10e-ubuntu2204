"""Phase 5: binary-native R006 seal (slim payload + R008RAW2 seal-block digest).

Monkeypatches ``R006ObjectiveBuilder.finalize`` and receipt version checks under
``r008_binary_seal_scope``. Frozen r006 bodies are not edited.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Iterator, Mapping

from step5d_autotune_v4_r006.contracts import (
    FORMAL_WINDOW_S,
    LEGACY_SHADOW_WINDOW_S,
    OBJECTIVE_SCHEMA,
    OBJECTIVE_SEMANTIC_FINGERPRINT,
    TARGET_FORCE_N,
    canonical_bytes,
    sha256_bytes,
)
from step5d_autotune_v4_r006.objective import (
    OBJECTIVE_RECEIPT_VERSION as STOCK_RECEIPT_VERSION,
    R006ObjectiveBuilder,
    R006ObjectiveError,
    R006ObjectiveReceipt,
    _formal_abs_error_stats,
    _mean_from_sums,
    _sample_identity,
    _sha,
    _signed_stats,
    _within_bin_abs_mae,
    cold_read_verify as stock_cold_read_verify,
)
from step5d_autotune_v4_r008.raw_force_binary_v2 import (
    SAMPLES_FORMAT_V2,
    detect_and_decode,
    seal_sha256_of,
    try_encode_samples_v2,
)
from step5d_force_objective import ForcePathSample

R008_OBJECTIVE_RECEIPT_VERSION = "r008-sealed-binary-raw-v1"
R008_RAW_BUNDLE_SCHEMA = "step5d.force-objective/r008-raw-path-bundle-v1"
R008_RAW_CODEC = "r008raw_v2"

# (attempt_sequence, execution_id) → full R008RAW2 file bytes pending disk write
_PENDING_RAW2: dict[tuple[int, str], bytes] = {}


def pop_pending_raw2(attempt_sequence: int, execution_id: str) -> bytes | None:
    return _PENDING_RAW2.pop((int(attempt_sequence), str(execution_id)), None)


def peek_pending_raw2(attempt_sequence: int, execution_id: str) -> bytes | None:
    return _PENDING_RAW2.get((int(attempt_sequence), str(execution_id)))


def slim_raw_bundle(
    *,
    attempt_sequence: int,
    execution_id: str,
    campaign_fingerprint: str,
    candidate_uid: str,
    sample_count: int,
    seal_block_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": R008_RAW_BUNDLE_SCHEMA,
        "attempt_sequence": attempt_sequence,
        "execution_id": execution_id,
        "campaign_fingerprint": campaign_fingerprint,
        "candidate_uid": candidate_uid,
        "semantic_fingerprint": OBJECTIVE_SEMANTIC_FINGERPRINT,
        "samples": [],
        "samples_format": SAMPLES_FORMAT_V2,
        "raw_codec": R008_RAW_CODEC,
        "sample_count": int(sample_count),
        "seal_block_sha256": seal_block_sha256,
    }


def finalize_binary_seal(
    builder: R006ObjectiveBuilder,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> R006ObjectiveReceipt:
    """Like ``R006ObjectiveBuilder.finalize`` but binary-native seal digest."""

    if not builder._samples:
        raise R006ObjectiveError("raw PATH bundle is empty")
    sample_dicts = [sample.as_dict() for sample in builder._samples]
    encoded = try_encode_samples_v2(sample_dicts)
    if encoded is None:
        # Fail closed to stock finalize for incompatible fixtures.
        return _stock_finalize(builder, metadata=metadata)
    full_bytes, _seal_region, seal_digest = encoded

    receipt_metadata = {} if metadata is None else dict(metadata)
    if builder.candidate_uid:
        receipt_metadata.setdefault("candidate_uid", builder.candidate_uid)
    receipt_metadata["raw_codec"] = R008_RAW_CODEC

    formal_signed_sums, formal_counts = _signed_stats(builder._samples, FORMAL_WINDOW_S)
    formal_abs_sums, _ = _formal_abs_error_stats(builder._samples)
    legacy_sums, legacy_counts = _signed_stats(builder._samples, LEGACY_SHADOW_WINDOW_S)
    stats_payload = {
        "formal_bin_sum_n": list(formal_signed_sums),
        "formal_bin_sum_abs_error_n": list(formal_abs_sums),
        "formal_bin_count": list(formal_counts),
        "r005_shadow_bin_sum_n": list(formal_signed_sums),
        "r005_shadow_bin_count": list(formal_counts),
        "legacy_r004_shadow_bin_sum_n": list(legacy_sums),
        "legacy_r004_shadow_bin_count": list(legacy_counts),
    }
    raw_bundle = slim_raw_bundle(
        attempt_sequence=builder.attempt_sequence,
        execution_id=builder.execution_id,
        campaign_fingerprint=builder.campaign_fingerprint,
        candidate_uid=builder.candidate_uid,
        sample_count=len(builder._samples),
        seal_block_sha256=seal_digest,
    )
    previous_post = R006ObjectiveReceipt.__post_init__
    R006ObjectiveReceipt.__post_init__ = _patched_post_init  # type: ignore[assignment]
    try:
        receipt = R006ObjectiveReceipt(
            schema=OBJECTIVE_SCHEMA,
            version=R008_OBJECTIVE_RECEIPT_VERSION,
            semantic_fingerprint=OBJECTIVE_SEMANTIC_FINGERPRINT,
            target_force_n=TARGET_FORCE_N,
            attempt_sequence=builder.attempt_sequence,
            execution_id=builder.execution_id,
            campaign_fingerprint=builder.campaign_fingerprint,
            raw_bundle=raw_bundle,
            raw_bundle_digest=seal_digest,
            source_sequence_time_identity_digest=_sample_identity(builder._samples),
            formal_bin_sum_n=formal_signed_sums,
            formal_bin_sum_abs_error_n=formal_abs_sums,
            formal_bin_count=formal_counts,
            r005_shadow_bin_sum_n=formal_signed_sums,
            r005_shadow_bin_count=formal_counts,
            legacy_r004_shadow_bin_sum_n=legacy_sums,
            legacy_r004_shadow_bin_count=legacy_counts,
            sufficient_statistics_digest=_sha(stats_payload),
            objective_mae_n=_within_bin_abs_mae(formal_abs_sums, formal_counts),
            r004_legacy_shadow_mae_n=_mean_from_sums(legacy_sums, legacy_counts),
            r005_abs_bin_mean_shadow_mae_n=_mean_from_sums(formal_signed_sums, formal_counts),
            sample_count=len(builder._samples),
            deduplicated_replays=builder._replays,
            builder_seal_sha256="0" * 64,
            metadata=receipt_metadata,
        )
        sealed = replace(receipt, builder_seal_sha256=_sha(receipt._seal_payload()))
    finally:
        R006ObjectiveReceipt.__post_init__ = previous_post  # type: ignore[assignment]
    _PENDING_RAW2[(int(builder.attempt_sequence), str(builder.execution_id))] = full_bytes
    return sealed


def _stock_finalize(
    builder: R006ObjectiveBuilder,
    *,
    metadata: Mapping[str, Any] | None,
) -> R006ObjectiveReceipt:
    return _ORIGINAL_FINALIZE(builder, metadata=metadata)


_ORIGINAL_FINALIZE = R006ObjectiveBuilder.finalize
_ORIGINAL_POST_INIT = R006ObjectiveReceipt.__post_init__


def cold_read_verify_binary(
    receipt: R006ObjectiveReceipt | Mapping[str, Any],
    *,
    expected_campaign_fingerprint: str | None = None,
    artifact_raw_bytes: bytes | None = None,
) -> R006ObjectiveReceipt:
    """Cold-verify Phase-5 receipts using R008RAW2 seal bytes + bin recompute."""

    original = (
        receipt
        if isinstance(receipt, R006ObjectiveReceipt)
        else R006ObjectiveReceipt.from_mapping(receipt)
    )
    if not is_binary_seal_receipt(original):
        return stock_cold_read_verify(
            original, expected_campaign_fingerprint=expected_campaign_fingerprint
        )
    original.validate_seal()
    if (
        expected_campaign_fingerprint is not None
        and original.campaign_fingerprint != expected_campaign_fingerprint
    ):
        raise R006ObjectiveError("campaign binding differs during cold read")

    raw = artifact_raw_bytes or peek_pending_raw2(
        original.attempt_sequence, original.execution_id
    )
    bundle = original.raw_bundle
    samples = bundle.get("samples") if isinstance(bundle, Mapping) else None
    if isinstance(samples, list) and samples:
        sample_maps = samples
    elif raw is not None:
        if seal_sha256_of(raw) != original.raw_bundle_digest:
            raise R006ObjectiveError("raw bundle digest differs during cold read")
        sample_maps = detect_and_decode(raw, include_audit=True)
    else:
        raise R006ObjectiveError("r008 binary seal cold-read lacks raw bytes")

    rebuilt = R006ObjectiveBuilder(
        attempt_sequence=original.attempt_sequence,
        execution_id=original.execution_id,
        campaign_fingerprint=original.campaign_fingerprint,
        candidate_uid=str(
            bundle.get("candidate_uid", original.metadata.get("candidate_uid", ""))
            if isinstance(bundle, Mapping)
            else original.metadata.get("candidate_uid", "")
        ),
    )
    # Temporarily use stock finalize to avoid nested pending overwrite while
    # comparing bin stats; digests compared against original binary digest.
    previous = R006ObjectiveBuilder.finalize
    R006ObjectiveBuilder.finalize = _ORIGINAL_FINALIZE  # type: ignore[assignment]
    try:
        for sample in sample_maps:
            rebuilt.add(ForcePathSample.from_mapping(sample) if isinstance(sample, Mapping) else sample)
        # Compute stats without stock finalize (would re-hash fat JSON).
        formal_signed_sums, formal_counts = _signed_stats(rebuilt._samples, FORMAL_WINDOW_S)
        formal_abs_sums, _ = _formal_abs_error_stats(rebuilt._samples)
        legacy_sums, legacy_counts = _signed_stats(rebuilt._samples, LEGACY_SHADOW_WINDOW_S)
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
            "source_sequence_time_identity_digest": _sample_identity(rebuilt._samples),
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
            "sample_count": len(rebuilt._samples),
            "deduplicated_replays": rebuilt._replays,
        }
    finally:
        R006ObjectiveBuilder.finalize = previous  # type: ignore[assignment]

    for name, value in recomputed.items():
        if getattr(original, name) != value:
            raise R006ObjectiveError(f"{name} differs during cold read")
    if raw is not None and seal_sha256_of(raw) != original.raw_bundle_digest:
        raise R006ObjectiveError("raw bundle digest differs during cold read")
    verified = replace(original, verification_state="verified_raw_artifact")
    verified.validate_seal()
    return verified


def _patched_finalize(
    self: R006ObjectiveBuilder,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> R006ObjectiveReceipt:
    return finalize_binary_seal(self, metadata=metadata)


def _patched_post_init(self: R006ObjectiveReceipt) -> None:
    if self.version == R008_OBJECTIVE_RECEIPT_VERSION:
        # Allow Phase-5 version; reuse stock checks with a temporary swap.
        object.__setattr__(self, "version", STOCK_RECEIPT_VERSION)
        try:
            _ORIGINAL_POST_INIT(self)
        finally:
            object.__setattr__(self, "version", R008_OBJECTIVE_RECEIPT_VERSION)
        bundle = self.raw_bundle
        if not isinstance(bundle, Mapping) or bundle.get("schema") != R008_RAW_BUNDLE_SCHEMA:
            raise R006ObjectiveError("r008 raw bundle schema differs")
        if bundle.get("samples_format") != SAMPLES_FORMAT_V2:
            raise R006ObjectiveError("r008 samples_format differs")
        if bundle.get("raw_codec") != R008_RAW_CODEC:
            raise R006ObjectiveError("r008 raw_codec differs")
        if list(bundle.get("samples") or []) != []:
            raise R006ObjectiveError("r008 seal raw_bundle must have empty samples")
        return
    _ORIGINAL_POST_INIT(self)


@contextmanager
def r008_binary_seal_scope() -> Iterator[None]:
    """Enable Phase-5 binary-native finalize + version acceptance."""

    from step5d_autotune_v4_r006 import objective as obj_mod

    previous_finalize = R006ObjectiveBuilder.finalize
    previous_post = R006ObjectiveReceipt.__post_init__
    previous_cold = obj_mod.cold_read_verify
    try:
        R006ObjectiveBuilder.finalize = _patched_finalize  # type: ignore[assignment]
        R006ObjectiveReceipt.__post_init__ = _patched_post_init  # type: ignore[assignment]
        obj_mod.cold_read_verify = cold_read_verify_binary  # type: ignore[assignment]
        yield
    finally:
        R006ObjectiveBuilder.finalize = previous_finalize  # type: ignore[assignment]
        R006ObjectiveReceipt.__post_init__ = previous_post  # type: ignore[assignment]
        obj_mod.cold_read_verify = previous_cold  # type: ignore[assignment]


def is_binary_seal_receipt(receipt: R006ObjectiveReceipt | Mapping[str, Any]) -> bool:
    if isinstance(receipt, R006ObjectiveReceipt):
        return receipt.version == R008_OBJECTIVE_RECEIPT_VERSION
    return (
        isinstance(receipt, Mapping)
        and receipt.get("version") == R008_OBJECTIVE_RECEIPT_VERSION
    )


__all__ = [
    "R008_OBJECTIVE_RECEIPT_VERSION",
    "R008_RAW_BUNDLE_SCHEMA",
    "R008_RAW_CODEC",
    "finalize_binary_seal",
    "is_binary_seal_receipt",
    "peek_pending_raw2",
    "pop_pending_raw2",
    "r008_binary_seal_scope",
    "slim_raw_bundle",
]
