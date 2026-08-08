"""Builder-only r006 raw PATH objective receipt and cold-read verifier.

The receipt is intentionally lineage-neutral at the primitive level: it binds
raw samples, sufficient statistics, source sequence/time identities, the
attempt/campaign identity, semantic fingerprint, and a seal.  A caller scalar
or a caller mapping is never sufficient to obtain a trainable objective.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from step5d_force_objective import ForceObjectiveError, ForcePathSample

from .contracts import (
    BIN_WIDTH_S,
    FORMAL_WINDOW_S,
    LEGACY_SHADOW_WINDOW_S,
    OBJECTIVE_RECEIPT_VERSION,
    OBJECTIVE_SCHEMA,
    OBJECTIVE_SEMANTIC_FINGERPRINT,
    PATH_STAGE,
    REQUIRED_BINS,
    TARGET_FORCE_N,
    canonical_bytes,
    sha256_bytes,
)


class R006ObjectiveError(ValueError):
    """Raw evidence, binding, or sealed receipt validation failed."""


def _sha(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool):
        raise R006ObjectiveError(f"{role} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise R006ObjectiveError(f"{role} must be numeric") from exc
    if not math.isfinite(number):
        raise R006ObjectiveError(f"{role} must be finite")
    return number


def _positive_int(value: Any, role: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise R006ObjectiveError(f"{role} must be a positive integer")
    return value


def _sha_field(value: Any, role: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise R006ObjectiveError(f"{role} must be a SHA-256")
    return value


def _sample_from(value: ForcePathSample | Mapping[str, Any]) -> ForcePathSample:
    if isinstance(value, ForcePathSample):
        sample = value
    elif isinstance(value, Mapping):
        try:
            sample = ForcePathSample.from_mapping(value)
        except (ForceObjectiveError, KeyError, TypeError, ValueError) as exc:
            raise R006ObjectiveError("raw PATH sample is invalid") from exc
    else:
        raise R006ObjectiveError("raw PATH sample must be typed")
    if sample.path_stage != PATH_STAGE or sample.timestamp_s is None:
        raise R006ObjectiveError("r006 raw sample requires stage 25 and source time identity")
    return sample


def _bin_index(time_s: float, window: tuple[float, float]) -> int | None:
    start, end = window
    if not (start <= time_s < end):
        return None
    index = int(math.floor((time_s - start) / BIN_WIDTH_S + 1e-10))
    return index if 0 <= index < REQUIRED_BINS else None


def _signed_stats(samples: Sequence[ForcePathSample], window: tuple[float, float]) -> tuple[tuple[float, ...], tuple[int, ...]]:
    sums = [0.0] * REQUIRED_BINS
    counts = [0] * REQUIRED_BINS
    for sample in samples:
        index = _bin_index(sample.path_time_s, window)
        if index is None:
            continue
        sums[index] += sample.filtered_normal_n
        counts[index] += 1
    return tuple(sums), tuple(counts)


def _formal_abs_error_stats(samples: Sequence[ForcePathSample]) -> tuple[tuple[float, ...], tuple[int, ...]]:
    sums = [0.0] * REQUIRED_BINS
    counts = [0] * REQUIRED_BINS
    for sample in samples:
        index = _bin_index(sample.path_time_s, FORMAL_WINDOW_S)
        if index is None:
            continue
        sums[index] += abs(sample.filtered_normal_n - TARGET_FORCE_N)
        counts[index] += 1
    return tuple(sums), tuple(counts)


def _mean_from_sums(sums: Sequence[float], counts: Sequence[int]) -> float | None:
    if len(sums) != REQUIRED_BINS or len(counts) != REQUIRED_BINS or not all(counts):
        return None
    return math.fsum(abs((total / count) - TARGET_FORCE_N) for total, count in zip(sums, counts, strict=True)) / REQUIRED_BINS


def _within_bin_abs_mae(sum_abs_error: Sequence[float], counts: Sequence[int]) -> float | None:
    if len(sum_abs_error) != REQUIRED_BINS or len(counts) != REQUIRED_BINS or not all(counts):
        return None
    return math.fsum(total / count for total, count in zip(sum_abs_error, counts, strict=True)) / REQUIRED_BINS


def _sample_identity(samples: Sequence[ForcePathSample]) -> str:
    return _sha(
        [
            {
                "path_time_s": sample.path_time_s,
                "timestamp_s": sample.timestamp_s,
                "source_sequences": dict(sample.source_sequences),
            }
            for sample in samples
        ]
    )


@dataclass(frozen=True)
class R006ObjectiveReceipt:
    schema: str
    version: str
    semantic_fingerprint: str
    target_force_n: float
    attempt_sequence: int
    execution_id: str
    campaign_fingerprint: str
    raw_bundle: Mapping[str, Any]
    raw_bundle_digest: str
    source_sequence_time_identity_digest: str
    formal_bin_sum_n: tuple[float, ...]
    formal_bin_sum_abs_error_n: tuple[float, ...]
    formal_bin_count: tuple[int, ...]
    r005_shadow_bin_sum_n: tuple[float, ...]
    r005_shadow_bin_count: tuple[int, ...]
    legacy_r004_shadow_bin_sum_n: tuple[float, ...]
    legacy_r004_shadow_bin_count: tuple[int, ...]
    sufficient_statistics_digest: str
    objective_mae_n: float | None
    r004_legacy_shadow_mae_n: float | None
    r005_abs_bin_mean_shadow_mae_n: float | None
    sample_count: int
    deduplicated_replays: int
    builder_seal_sha256: str
    verification_state: str = "builder_sealed"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema != OBJECTIVE_SCHEMA or self.version != OBJECTIVE_RECEIPT_VERSION:
            raise R006ObjectiveError("r006 objective schema/version differs")
        if self.semantic_fingerprint != OBJECTIVE_SEMANTIC_FINGERPRINT:
            raise R006ObjectiveError("r006 objective semantic fingerprint differs")
        if float(self.target_force_n) != TARGET_FORCE_N:
            raise R006ObjectiveError("r006 target force is immutable at 5 N")
        _positive_int(self.attempt_sequence, "attempt sequence")
        if not isinstance(self.execution_id, str) or not self.execution_id:
            raise R006ObjectiveError("execution_id is required")
        if not isinstance(self.raw_bundle, Mapping):
            raise R006ObjectiveError("raw bundle must be a mapping owned by the builder")
        if not isinstance(self.metadata, Mapping):
            raise R006ObjectiveError("objective metadata must be a mapping")
        _sha_field(self.campaign_fingerprint, "campaign fingerprint")
        _sha_field(self.raw_bundle_digest, "raw bundle digest")
        _sha_field(self.source_sequence_time_identity_digest, "source identity digest")
        _sha_field(self.sufficient_statistics_digest, "sufficient statistics digest")
        _sha_field(self.builder_seal_sha256, "builder seal")
        if self.verification_state not in {"builder_sealed", "verified_raw_artifact"}:
            raise R006ObjectiveError("objective verification state differs")
        if (
            len(self.formal_bin_sum_n) != REQUIRED_BINS
            or len(self.formal_bin_sum_abs_error_n) != REQUIRED_BINS
            or len(self.formal_bin_count) != REQUIRED_BINS
            or len(self.r005_shadow_bin_sum_n) != REQUIRED_BINS
            or len(self.r005_shadow_bin_count) != REQUIRED_BINS
        ):
            raise R006ObjectiveError("formal sufficient statistics length differs")
        if len(self.legacy_r004_shadow_bin_sum_n) != REQUIRED_BINS or len(self.legacy_r004_shadow_bin_count) != REQUIRED_BINS:
            raise R006ObjectiveError("legacy sufficient statistics length differs")
        if any(
            isinstance(count, bool) or not isinstance(count, int) or count < 0
            for count in self.formal_bin_count
            + self.r005_shadow_bin_count
            + self.legacy_r004_shadow_bin_count
        ):
            raise R006ObjectiveError("bin count is invalid")
        if any(
            not math.isfinite(float(value))
            for value in self.formal_bin_sum_n
            + self.formal_bin_sum_abs_error_n
            + self.r005_shadow_bin_sum_n
            + self.legacy_r004_shadow_bin_sum_n
        ):
            raise R006ObjectiveError("bin sum is nonfinite")
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int) or self.sample_count <= 0:
            raise R006ObjectiveError("sample count is invalid")
        if isinstance(self.deduplicated_replays, bool) or not isinstance(self.deduplicated_replays, int) or self.deduplicated_replays < 0:
            raise R006ObjectiveError("deduplicated replay count is invalid")
        for value in (self.objective_mae_n, self.r004_legacy_shadow_mae_n, self.r005_abs_bin_mean_shadow_mae_n):
            if value is not None and (not math.isfinite(float(value)) or value < 0.0):
                raise R006ObjectiveError("objective metric is invalid")

    @property
    def formal_complete_bins(self) -> int:
        return sum(1 for count in self.formal_bin_count if count > 0)

    @property
    def trainable(self) -> bool:
        return (
            self.verification_state == "verified_raw_artifact"
            and self.formal_complete_bins == REQUIRED_BINS
            and self.objective_mae_n is not None
        )

    @property
    def objective(self) -> float | None:
        return self.objective_mae_n if self.trainable else None

    def _seal_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "semantic_fingerprint": self.semantic_fingerprint,
            "target_force_n": self.target_force_n,
            "attempt_sequence": self.attempt_sequence,
            "execution_id": self.execution_id,
            "campaign_fingerprint": self.campaign_fingerprint,
            "raw_bundle": self.raw_bundle,
            "raw_bundle_digest": self.raw_bundle_digest,
            "source_sequence_time_identity_digest": self.source_sequence_time_identity_digest,
            "formal_bin_sum_n": list(self.formal_bin_sum_n),
            "formal_bin_sum_abs_error_n": list(self.formal_bin_sum_abs_error_n),
            "formal_bin_count": list(self.formal_bin_count),
            "r005_shadow_bin_sum_n": list(self.r005_shadow_bin_sum_n),
            "r005_shadow_bin_count": list(self.r005_shadow_bin_count),
            "legacy_r004_shadow_bin_sum_n": list(self.legacy_r004_shadow_bin_sum_n),
            "legacy_r004_shadow_bin_count": list(self.legacy_r004_shadow_bin_count),
            "sufficient_statistics_digest": self.sufficient_statistics_digest,
            "objective_mae_n": self.objective_mae_n,
            "r004_legacy_shadow_mae_n": self.r004_legacy_shadow_mae_n,
            "r005_abs_bin_mean_shadow_mae_n": self.r005_abs_bin_mean_shadow_mae_n,
            "sample_count": self.sample_count,
            "deduplicated_replays": self.deduplicated_replays,
            "metadata": dict(self.metadata),
        }

    def validate_seal(self) -> None:
        expected = _sha(self._seal_payload())
        if expected != self.builder_seal_sha256:
            raise R006ObjectiveError("objective builder seal differs")

    def as_dict(self) -> dict[str, Any]:
        return {
            **self._seal_payload(),
            "builder_seal_sha256": self.builder_seal_sha256,
            "verification_state": self.verification_state,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "R006ObjectiveReceipt":
        if not isinstance(value, Mapping):
            raise R006ObjectiveError("objective receipt must be a mapping")
        required = {
            "schema", "version", "semantic_fingerprint", "target_force_n",
            "attempt_sequence", "execution_id", "campaign_fingerprint", "raw_bundle",
            "raw_bundle_digest", "source_sequence_time_identity_digest",
            "formal_bin_sum_n", "formal_bin_sum_abs_error_n", "formal_bin_count",
            "r005_shadow_bin_sum_n", "r005_shadow_bin_count", "legacy_r004_shadow_bin_sum_n",
            "legacy_r004_shadow_bin_count", "sufficient_statistics_digest", "objective_mae_n",
            "r004_legacy_shadow_mae_n", "r005_abs_bin_mean_shadow_mae_n", "sample_count",
            "deduplicated_replays", "builder_seal_sha256", "verification_state", "metadata",
        }
        if set(value) != required:
            raise R006ObjectiveError("receipt fields differ; caller scalar/mapping is not a receipt")
        try:
            receipt = cls(
                schema=str(value["schema"]),
                version=str(value["version"]),
                semantic_fingerprint=str(value["semantic_fingerprint"]),
                target_force_n=value["target_force_n"],
                attempt_sequence=value["attempt_sequence"],
                execution_id=str(value["execution_id"]),
                campaign_fingerprint=str(value["campaign_fingerprint"]),
                raw_bundle=value["raw_bundle"],
                raw_bundle_digest=str(value["raw_bundle_digest"]),
                source_sequence_time_identity_digest=str(value["source_sequence_time_identity_digest"]),
                formal_bin_sum_n=tuple(float(x) for x in value["formal_bin_sum_n"]),
                formal_bin_sum_abs_error_n=tuple(float(x) for x in value["formal_bin_sum_abs_error_n"]),
                formal_bin_count=tuple(int(x) for x in value["formal_bin_count"]),
                r005_shadow_bin_sum_n=tuple(float(x) for x in value["r005_shadow_bin_sum_n"]),
                r005_shadow_bin_count=tuple(int(x) for x in value["r005_shadow_bin_count"]),
                legacy_r004_shadow_bin_sum_n=tuple(float(x) for x in value["legacy_r004_shadow_bin_sum_n"]),
                legacy_r004_shadow_bin_count=tuple(int(x) for x in value["legacy_r004_shadow_bin_count"]),
                sufficient_statistics_digest=str(value["sufficient_statistics_digest"]),
                objective_mae_n=None if value["objective_mae_n"] is None else float(value["objective_mae_n"]),
                r004_legacy_shadow_mae_n=None if value["r004_legacy_shadow_mae_n"] is None else float(value["r004_legacy_shadow_mae_n"]),
                r005_abs_bin_mean_shadow_mae_n=None if value["r005_abs_bin_mean_shadow_mae_n"] is None else float(value["r005_abs_bin_mean_shadow_mae_n"]),
                sample_count=value["sample_count"],
                deduplicated_replays=value["deduplicated_replays"],
                builder_seal_sha256=str(value["builder_seal_sha256"]),
                verification_state=str(value["verification_state"]),
                metadata=value["metadata"],
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise R006ObjectiveError("objective receipt fields are invalid") from exc
        receipt.validate_seal()
        return receipt


class R006ObjectiveBuilder:
    """Only raw typed samples can enter this builder."""

    def __init__(
        self,
        *,
        attempt_sequence: int,
        execution_id: str,
        campaign_fingerprint: str,
        candidate_uid: str | None = None,
    ) -> None:
        _positive_int(attempt_sequence, "attempt sequence")
        if not isinstance(execution_id, str) or not execution_id:
            raise R006ObjectiveError("execution_id is required")
        _sha_field(campaign_fingerprint, "campaign fingerprint")
        self.attempt_sequence = attempt_sequence
        self.execution_id = execution_id
        self.campaign_fingerprint = campaign_fingerprint
        self.candidate_uid = "" if candidate_uid is None else str(candidate_uid)
        self._samples: list[ForcePathSample] = []
        # Fast identity key matches ForcePathSample.identity_payload contents.
        # Digests are computed only on collision (conflict vs replay).
        self._sample_by_identity_key: dict[tuple[tuple[str, int | str], ...], ForcePathSample] = {}
        self._last_time: float | None = None
        self._replays = 0

    def add(self, sample: ForcePathSample | Mapping[str, Any]) -> bool:
        typed = _sample_from(sample)
        if self._last_time is not None and typed.path_time_s < self._last_time:
            raise R006ObjectiveError("raw PATH time regressed")
        self._last_time = typed.path_time_s
        identity_key = tuple(sorted(typed.source_sequences.items()))
        prior = self._sample_by_identity_key.get(identity_key)
        if prior is not None:
            # Digests only on collision: identity confirms key; payload is conflict vs replay.
            prior_identity = prior.identity_digest
            typed_identity = typed.identity_digest
            if prior_identity != typed_identity or prior.payload_digest != typed.payload_digest:
                raise R006ObjectiveError("conflicting payload for source identity")
            self._replays += 1
            return False
        self._sample_by_identity_key[identity_key] = typed
        self._samples.append(typed)
        return True

    def _raw_bundle(self) -> dict[str, Any]:
        return {
            "schema": "step5d.force-objective/r006-raw-path-bundle-v1",
            "attempt_sequence": self.attempt_sequence,
            "execution_id": self.execution_id,
            "campaign_fingerprint": self.campaign_fingerprint,
            "candidate_uid": self.candidate_uid,
            "semantic_fingerprint": OBJECTIVE_SEMANTIC_FINGERPRINT,
            "samples": [sample.as_dict() for sample in self._samples],
        }

    def finalize(self, *, metadata: Mapping[str, Any] | None = None) -> R006ObjectiveReceipt:
        if not self._samples:
            raise R006ObjectiveError("raw PATH bundle is empty")
        raw_bundle = self._raw_bundle()
        receipt_metadata = {} if metadata is None else dict(metadata)
        if self.candidate_uid:
            receipt_metadata.setdefault("candidate_uid", self.candidate_uid)
        raw_digest = _sha(raw_bundle)
        formal_signed_sums, formal_counts = _signed_stats(self._samples, FORMAL_WINDOW_S)
        formal_abs_sums, _formal_abs_counts = _formal_abs_error_stats(self._samples)
        legacy_sums, legacy_counts = _signed_stats(self._samples, LEGACY_SHADOW_WINDOW_S)
        stats_payload = {
            "formal_bin_sum_n": list(formal_signed_sums),
            "formal_bin_sum_abs_error_n": list(formal_abs_sums),
            "formal_bin_count": list(formal_counts),
            "r005_shadow_bin_sum_n": list(formal_signed_sums),
            "r005_shadow_bin_count": list(formal_counts),
            "legacy_r004_shadow_bin_sum_n": list(legacy_sums),
            "legacy_r004_shadow_bin_count": list(legacy_counts),
        }
        receipt = R006ObjectiveReceipt(
            schema=OBJECTIVE_SCHEMA,
            version=OBJECTIVE_RECEIPT_VERSION,
            semantic_fingerprint=OBJECTIVE_SEMANTIC_FINGERPRINT,
            target_force_n=TARGET_FORCE_N,
            attempt_sequence=self.attempt_sequence,
            execution_id=self.execution_id,
            campaign_fingerprint=self.campaign_fingerprint,
            raw_bundle=raw_bundle,
            raw_bundle_digest=raw_digest,
            source_sequence_time_identity_digest=_sample_identity(self._samples),
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
            # This is intentionally explicit and non-authoritative.  It is
            # the current r005 bin-mean formula over the formal window.
            r005_abs_bin_mean_shadow_mae_n=_mean_from_sums(formal_signed_sums, formal_counts),
            sample_count=len(self._samples),
            deduplicated_replays=self._replays,
            builder_seal_sha256="0" * 64,
            metadata=receipt_metadata,
        )
        sealed = replace(receipt, builder_seal_sha256=_sha(receipt._seal_payload()))
        return sealed


def cold_read_verify(
    receipt: R006ObjectiveReceipt | Mapping[str, Any],
    *,
    expected_campaign_fingerprint: str | None = None,
) -> R006ObjectiveReceipt:
    """Rebuild all metrics from the persisted raw bundle in a fresh read."""

    original = receipt if isinstance(receipt, R006ObjectiveReceipt) else R006ObjectiveReceipt.from_mapping(receipt)
    original.validate_seal()
    if expected_campaign_fingerprint is not None and original.campaign_fingerprint != expected_campaign_fingerprint:
        raise R006ObjectiveError("campaign binding differs during cold read")
    bundle = original.raw_bundle
    if not isinstance(bundle, Mapping) or bundle.get("schema") != "step5d.force-objective/r006-raw-path-bundle-v1":
        raise R006ObjectiveError("raw artifact bundle schema differs")
    if bundle.get("attempt_sequence") != original.attempt_sequence or bundle.get("execution_id") != original.execution_id or bundle.get("campaign_fingerprint") != original.campaign_fingerprint:
        raise R006ObjectiveError("raw artifact attempt/campaign binding differs")
    if bundle.get("semantic_fingerprint") != OBJECTIVE_SEMANTIC_FINGERPRINT:
        raise R006ObjectiveError("raw artifact semantic fingerprint differs")
    samples = bundle.get("samples")
    if not isinstance(samples, list) or not samples:
        raise R006ObjectiveError("raw artifact samples are missing")
    rebuilt = R006ObjectiveBuilder(
        attempt_sequence=original.attempt_sequence,
        execution_id=original.execution_id,
        campaign_fingerprint=original.campaign_fingerprint,
        candidate_uid=str(bundle.get("candidate_uid", original.metadata.get("candidate_uid", ""))),
    )
    for sample in samples:
        rebuilt.add(sample)
    recomputed = rebuilt.finalize(metadata=original.metadata)
    if recomputed.raw_bundle_digest != original.raw_bundle_digest:
        raise R006ObjectiveError("raw bundle digest differs during cold read")
    for name in (
        "source_sequence_time_identity_digest",
        "formal_bin_sum_n",
        "formal_bin_sum_abs_error_n",
        "formal_bin_count",
        "r005_shadow_bin_sum_n",
        "r005_shadow_bin_count",
        "legacy_r004_shadow_bin_sum_n",
        "legacy_r004_shadow_bin_count",
        "sufficient_statistics_digest",
        "objective_mae_n",
        "r004_legacy_shadow_mae_n",
        "r005_abs_bin_mean_shadow_mae_n",
        "sample_count",
        "deduplicated_replays",
    ):
        if getattr(recomputed, name) != getattr(original, name):
            raise R006ObjectiveError(f"{name} differs during cold read")
    verified = replace(original, verification_state="verified_raw_artifact")
    verified.validate_seal()
    return verified


def cold_read_verify_subprocess(
    receipt: R006ObjectiveReceipt | Mapping[str, Any],
    *,
    expected_campaign_fingerprint: str | None = None,
) -> R006ObjectiveReceipt:
    """Perform the cold read in a fresh Python subprocess before returning it."""

    original = receipt if isinstance(receipt, R006ObjectiveReceipt) else R006ObjectiveReceipt.from_mapping(receipt)
    code = (
        "import json,sys; "
        "from step5d_autotune_v4_r006.objective import cold_read_verify; "
        "value=cold_read_verify(json.load(sys.stdin), expected_campaign_fingerprint=sys.argv[1] or None); "
        "print(json.dumps(value.as_dict(), sort_keys=True))"
    )
    expected = "" if expected_campaign_fingerprint is None else expected_campaign_fingerprint
    completed = subprocess.run(
        [sys.executable, "-c", code, expected],
        input=json.dumps(original.as_dict(), sort_keys=True),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise R006ObjectiveError(
            f"subprocess cold read failed: {completed.stderr.strip() or completed.returncode}"
        )
    try:
        result = R006ObjectiveReceipt.from_mapping(json.loads(completed.stdout))
    except (json.JSONDecodeError, R006ObjectiveError) as exc:
        raise R006ObjectiveError("subprocess cold read returned an invalid receipt") from exc
    if expected_campaign_fingerprint is not None and result.campaign_fingerprint != expected_campaign_fingerprint:
        raise R006ObjectiveError("subprocess cold read campaign binding differs")
    if not result.trainable:
        raise R006ObjectiveError("subprocess cold read did not produce a verified receipt")
    return result


def build_receipt_from_samples(
    samples: Iterable[ForcePathSample],
    *,
    attempt_sequence: int,
    execution_id: str,
    campaign_fingerprint: str,
    candidate_uid: str | None = None,
) -> R006ObjectiveReceipt:
    builder = R006ObjectiveBuilder(
        attempt_sequence=attempt_sequence,
        execution_id=execution_id,
        campaign_fingerprint=campaign_fingerprint,
        candidate_uid=candidate_uid,
    )
    for sample in samples:
        builder.add(sample)
    return cold_read_verify(builder.finalize(), expected_campaign_fingerprint=campaign_fingerprint)


__all__ = [
    "R006ObjectiveBuilder",
    "R006ObjectiveError",
    "R006ObjectiveReceipt",
    "build_receipt_from_samples",
    "cold_read_verify",
    "cold_read_verify_subprocess",
]
