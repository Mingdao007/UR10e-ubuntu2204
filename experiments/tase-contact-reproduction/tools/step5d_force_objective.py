"""Lineage-neutral force objective over immutable aligned PATH evidence.

The optimizer-facing value is deliberately produced here, rather than read
from a result scalar.  The primitive is independent of the V4 campaign so a
future V5 can bind the same evidence contract without importing a V4 identity.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Mapping


FORCE_OBJECTIVE_SCHEMA = "step5d.force-objective/v2"
FORCE_OBJECTIVE_VERSION = "force_mae_v2"
FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT = (
    "r005.force-mae-v2|stage=25|formal=[5,60)|legacy-shadow=[0,55)|"
    "bin=0.1s|bins=550|stat=mean(abs(mean(force_in_bin)-5N))|"
    "legacy=shadow-only"
)
FORCE_OBJECTIVE_RECEIPT_VERSION = "r005-sealed-sufficient-statistics-v1"
LEGACY_SHADOW_VERSION = "r004_legacy_shadow_v1"
TARGET_FORCE_N = 5.0
PATH_STAGE = 25
BIN_WIDTH_S = 0.1
FORMAL_START_S = 5.0
FORMAL_END_S = 60.0
LEGACY_START_S = 0.0
LEGACY_END_S = 55.0
REQUIRED_BINS = 550
MAX_SOURCE_AGE_S = 0.080


class ForceObjectiveError(ValueError):
    """Raw force evidence cannot be used for the typed objective."""


def _require_digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ForceObjectiveError(f"{role} must be a lowercase SHA-256")
    return value


def _stats_payload(
    formal_sums: tuple[float, ...],
    formal_counts: tuple[int, ...],
    legacy_sums: tuple[float, ...],
    legacy_counts: tuple[int, ...],
) -> dict[str, Any]:
    return {
        "receipt_version": FORCE_OBJECTIVE_RECEIPT_VERSION,
        "semantic_fingerprint": FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
        "formal_bin_sum_n": list(formal_sums),
        "formal_bin_count": list(formal_counts),
        "legacy_bin_sum_n": list(legacy_sums),
        "legacy_bin_count": list(legacy_counts),
    }


def _stats_digest(
    formal_sums: tuple[float, ...],
    formal_counts: tuple[int, ...],
    legacy_sums: tuple[float, ...],
    legacy_counts: tuple[int, ...],
) -> str:
    return _sha256(_canonical(_stats_payload(formal_sums, formal_counts, legacy_sums, legacy_counts), "sufficient statistics"))


def _mae_from_stats(
    sums: tuple[float, ...], counts: tuple[int, ...], *, role: str
) -> float | None:
    if len(sums) != REQUIRED_BINS or len(counts) != REQUIRED_BINS:
        raise ForceObjectiveError(f"{role} sufficient statistics must contain 550 bins")
    if any(count < 0 for count in counts):
        raise ForceObjectiveError(f"{role} sufficient-statistic count is negative")
    if not all(math.isfinite(value) for value in sums):
        raise ForceObjectiveError(f"{role} sufficient-statistic sum is nonfinite")
    if not all((count == 0 and math.isclose(total, 0.0, abs_tol=1e-12)) or count > 0 for total, count in zip(sums, counts, strict=True)):
        raise ForceObjectiveError(f"{role} empty-bin sufficient statistic is nonzero")
    if not all(count > 0 for count in counts):
        return None
    return math.fsum(
        abs((total / count) - TARGET_FORCE_N)
        for total, count in zip(sums, counts, strict=True)
    ) / REQUIRED_BINS


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool):
        raise ForceObjectiveError(f"{role} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ForceObjectiveError(f"{role} must be numeric") from exc
    if not math.isfinite(number):
        raise ForceObjectiveError(f"{role} must be finite")
    return number


def _canonical(value: Any, role: str) -> bytes:
    try:
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
    except (TypeError, ValueError) as exc:
        raise ForceObjectiveError(f"{role} is not canonical") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sequence_map(value: Any) -> dict[str, int | str]:
    if not isinstance(value, Mapping) or not value:
        raise ForceObjectiveError("source_sequences must be a non-empty mapping")
    result: dict[str, int | str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ForceObjectiveError("source sequence keys must be non-empty strings")
        if isinstance(item, bool) or item is None or not isinstance(item, (int, str)):
            raise ForceObjectiveError(f"source sequence {key} is not a stable identity")
        if isinstance(item, int) and item <= 0:
            raise ForceObjectiveError(f"source sequence {key} must be positive")
        if isinstance(item, str) and not item:
            raise ForceObjectiveError(f"source sequence {key} is empty")
        result[key] = item
    return result


def _ages(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping) or not value:
        raise ForceObjectiveError("source_ages_s must be a non-empty mapping")
    result: dict[str, float] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ForceObjectiveError("source age keys must be non-empty strings")
        age = _finite(item, f"source age {key}")
        if age < 0.0 or age > MAX_SOURCE_AGE_S:
            raise ForceObjectiveError(f"source age {key} is stale")
        result[key] = age
    return result


@dataclass(frozen=True)
class ForcePathSample:
    """One fresh PATH force sample with a stable source/sequence identity."""

    path_time_s: float
    filtered_normal_n: float
    source_sequences: Mapping[str, int | str]
    source_ages_s: Mapping[str, float]
    path_phase: int | None = None
    stage: int | None = None
    path_stage: int | None = None
    commanded_qdot: tuple[float, ...] | None = None
    actual_qd: tuple[float, ...] | None = None
    timestamp_s: float | None = None

    def __post_init__(self) -> None:
        path_time = _finite(self.path_time_s, "PATH-relative time")
        if path_time < 0.0:
            raise ForceObjectiveError("PATH-relative time is negative")
        stage_values = [value for value in (self.path_phase, self.stage, self.path_stage) if value is not None]
        if not stage_values or any(isinstance(value, bool) or not isinstance(value, int) for value in stage_values):
            raise ForceObjectiveError("PATH stage is not an integer")
        if any(value != PATH_STAGE for value in stage_values) or len(set(stage_values)) != 1:
            raise ForceObjectiveError("only stage 25 may be used as PATH evidence")
        force = _finite(self.filtered_normal_n, "filtered normal force")
        sequences = _sequence_map(self.source_sequences)
        ages = _ages(self.source_ages_s)
        if set(sequences) != set(ages):
            raise ForceObjectiveError("source sequence and freshness bindings differ")

        normalized_qdot = self._vector(self.commanded_qdot, "commanded qdot")
        normalized_actual = self._vector(self.actual_qd, "actual qd")
        if (normalized_qdot is None) != (normalized_actual is None):
            raise ForceObjectiveError("qdot and actual_qd must be bound together")
        if normalized_qdot is not None and len(normalized_qdot) != len(normalized_actual or ()):
            raise ForceObjectiveError("qdot and actual_qd lengths differ")
        timestamp = None if self.timestamp_s is None else _finite(self.timestamp_s, "evidence timestamp")
        object.__setattr__(self, "path_time_s", path_time)
        object.__setattr__(self, "filtered_normal_n", force)
        object.__setattr__(self, "path_phase", PATH_STAGE)
        object.__setattr__(self, "stage", PATH_STAGE)
        object.__setattr__(self, "path_stage", PATH_STAGE)
        object.__setattr__(self, "source_sequences", sequences)
        object.__setattr__(self, "source_ages_s", ages)
        object.__setattr__(self, "commanded_qdot", normalized_qdot)
        object.__setattr__(self, "actual_qd", normalized_actual)
        object.__setattr__(self, "timestamp_s", timestamp)

    @staticmethod
    def _vector(value: Any, role: str) -> tuple[float, ...] | None:
        if value is None:
            return None
        if isinstance(value, (str, bytes)):
            raise ForceObjectiveError(f"{role} is not a vector")
        try:
            values = tuple(_finite(item, role) for item in value)
        except TypeError as exc:
            raise ForceObjectiveError(f"{role} is not a vector") from exc
        if not values:
            raise ForceObjectiveError(f"{role} is empty")
        return values

    @property
    def identity_payload(self) -> dict[str, Any]:
        return {"source_sequences": dict(self.source_sequences)}

    def as_dict(self) -> dict[str, Any]:
        """Return the complete canonical raw-sample payload.

        This is intentionally separate from the bounded objective mapping.
        The raw artifact store persists this payload, while the observation
        ledger stores only its content binding and the freshly recomputed
        sufficient statistics.
        """

        return {
            "path_time_s": self.path_time_s,
            "path_phase": self.path_phase,
            "stage": self.stage,
            "path_stage": self.path_stage,
            "filtered_normal_n": self.filtered_normal_n,
            "source_sequences": dict(self.source_sequences),
            "source_ages_s": dict(self.source_ages_s),
            "commanded_qdot": None
            if self.commanded_qdot is None
            else list(self.commanded_qdot),
            "actual_qd": None if self.actual_qd is None else list(self.actual_qd),
            "timestamp_s": self.timestamp_s,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ForcePathSample":
        """Reconstruct one sample from the strict durable raw schema."""

        if not isinstance(value, Mapping):
            raise ForceObjectiveError("raw PATH sample is not an object")
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
        if set(value) != required:
            raise ForceObjectiveError("raw PATH sample fields differ")
        try:
            return cls(
                path_time_s=value["path_time_s"],
                path_phase=value["path_phase"],
                stage=value["stage"],
                path_stage=value["path_stage"],
                filtered_normal_n=value["filtered_normal_n"],
                source_sequences=value["source_sequences"],
                source_ages_s=value["source_ages_s"],
                commanded_qdot=value["commanded_qdot"],
                actual_qd=value["actual_qd"],
                timestamp_s=value["timestamp_s"],
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ForceObjectiveError("raw PATH sample payload is invalid") from exc

    @property
    def identity_digest(self) -> str:
        return _sha256(_canonical(self.identity_payload, "sample identity"))

    @property
    def payload_digest(self) -> str:
        return _sha256(
            _canonical(
                {
                    "path_time_s": self.path_time_s,
                    "path_phase": self.path_phase,
                    "filtered_normal_n": self.filtered_normal_n,
                    "source_sequences": dict(self.source_sequences),
                    "source_ages_s": dict(self.source_ages_s),
                    "commanded_qdot": self.commanded_qdot,
                    "actual_qd": self.actual_qd,
                    "timestamp_s": self.timestamp_s,
                },
                "sample payload",
            )
        )


@dataclass(frozen=True)
class ForceObjective:
    """Immutable v2 objective and audit-only r004 shadow metadata."""

    schema: str
    version: str
    target_force_n: float
    formal_window_s: tuple[float, float]
    legacy_window_s: tuple[float, float]
    bin_width_s: float
    required_bins: int
    complete_bins: int
    coverage_bitmap: str
    formal_bin_ids: tuple[int, ...]
    legacy_bin_ids: tuple[int, ...]
    sample_identity_digest: str
    sample_identity_ids: tuple[str, ...]
    sample_count: int
    deduplicated_replays: int
    v2_mae_n: float | None
    legacy_mae_n: float | None
    delta_n: float | None
    provenance: str = "raw_path_evidence"
    legacy_complete_bins: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    semantic_fingerprint: str = ""
    raw_evidence_digest: str = ""
    sufficient_statistics_digest: str = ""
    builder_seal_sha256: str = ""
    formal_bin_sum_n: tuple[float, ...] = ()
    formal_bin_count: tuple[int, ...] = ()
    legacy_bin_sum_n: tuple[float, ...] = ()
    legacy_bin_count: tuple[int, ...] = ()
    sample_identity_count: int | None = None

    def __post_init__(self) -> None:
        if self.schema != FORCE_OBJECTIVE_SCHEMA or self.version != FORCE_OBJECTIVE_VERSION:
            raise ForceObjectiveError("force objective schema/version differs")
        if self.target_force_n != TARGET_FORCE_N:
            raise ForceObjectiveError("target force is immutable and must be exactly 5.0 N")
        if self.formal_window_s != (FORMAL_START_S, FORMAL_END_S):
            raise ForceObjectiveError("formal force window differs")
        if self.legacy_window_s != (LEGACY_START_S, LEGACY_END_S):
            raise ForceObjectiveError("legacy shadow window differs")
        if self.bin_width_s != BIN_WIDTH_S or self.required_bins != REQUIRED_BINS:
            raise ForceObjectiveError("force bin contract differs")
        if self.complete_bins < 0 or self.complete_bins > REQUIRED_BINS:
            raise ForceObjectiveError("formal complete-bin count is invalid")
        if self.legacy_complete_bins < 0 or self.legacy_complete_bins > REQUIRED_BINS:
            raise ForceObjectiveError("legacy complete-bin count is invalid")
        if len(self.coverage_bitmap) != REQUIRED_BINS or any(bit not in "01" for bit in self.coverage_bitmap):
            raise ForceObjectiveError("formal coverage bitmap is invalid")
        expected_formal_ids = tuple(sorted(set(self.formal_bin_ids)))
        expected_legacy_ids = tuple(sorted(set(self.legacy_bin_ids)))
        if (
            expected_formal_ids != self.formal_bin_ids
            or any(index < 0 or index >= REQUIRED_BINS for index in expected_formal_ids)
        ):
            raise ForceObjectiveError("formal bin identities are invalid")
        if (
            expected_legacy_ids != self.legacy_bin_ids
            or any(index < 0 or index >= REQUIRED_BINS for index in expected_legacy_ids)
        ):
            raise ForceObjectiveError("legacy bin identities are invalid")
        if self.complete_bins != len(expected_formal_ids):
            raise ForceObjectiveError("formal complete-bin count differs from bin identities")
        if self.legacy_complete_bins != len(expected_legacy_ids):
            raise ForceObjectiveError("legacy complete-bin count differs from bin identities")
        expected_bitmap = "".join(
            "1" if index in set(expected_formal_ids) else "0"
            for index in range(REQUIRED_BINS)
        )
        if self.coverage_bitmap != expected_bitmap:
            raise ForceObjectiveError("formal coverage bitmap differs from bin identities")
        if self.v2_mae_n is not None:
            value = _finite(self.v2_mae_n, "v2 MAE")
            if value < 0.0:
                raise ForceObjectiveError("v2 MAE is negative")
            if self.complete_bins != REQUIRED_BINS:
                raise ForceObjectiveError("v2 MAE requires exactly 550 complete bins")
        elif self.complete_bins == REQUIRED_BINS:
            raise ForceObjectiveError("complete formal coverage requires a v2 MAE")
        if self.legacy_mae_n is not None:
            value = _finite(self.legacy_mae_n, "legacy MAE")
            if value < 0.0:
                raise ForceObjectiveError("legacy MAE is negative")
            if self.legacy_complete_bins != REQUIRED_BINS:
                raise ForceObjectiveError("legacy MAE requires exactly 550 complete bins")
        elif self.legacy_complete_bins == REQUIRED_BINS:
            raise ForceObjectiveError("complete legacy coverage requires a legacy MAE")
        if self.delta_n is not None and (self.v2_mae_n is None or self.legacy_mae_n is None):
            raise ForceObjectiveError("objective delta requires both values")
        if self.delta_n is not None:
            delta = _finite(self.delta_n, "objective delta")
            expected_delta = float(self.legacy_mae_n) - float(self.v2_mae_n)
            if not math.isclose(delta, expected_delta, rel_tol=0.0, abs_tol=1e-12):
                raise ForceObjectiveError("objective delta differs from bound values")
        if (
            isinstance(self.sample_count, bool)
            or not isinstance(self.sample_count, int)
            or self.sample_count < 0
            or isinstance(self.deduplicated_replays, bool)
            or not isinstance(self.deduplicated_replays, int)
            or self.deduplicated_replays < 0
        ):
            raise ForceObjectiveError("sample counts are invalid")
        if any(
            len(identity) != 64
            or any(character not in "0123456789abcdef" for character in identity)
            for identity in self.sample_identity_ids
        ):
            raise ForceObjectiveError("sample identity ids must be lowercase SHA-256 values")
        _require_digest(self.sample_identity_digest, "sample identity digest")
        identity_count = self.sample_identity_count
        if identity_count is None:
            identity_count = len(self.sample_identity_ids)
        if (
            isinstance(identity_count, bool)
            or not isinstance(identity_count, int)
            or identity_count < 0
            or identity_count > self.sample_count
        ):
            raise ForceObjectiveError("sample identity count is invalid")
        if self.sample_identity_ids:
            raise ForceObjectiveError(
                "raw sample identity IDs must not be persisted; use the aggregate digest"
            )
        if not isinstance(self.provenance, str) or not self.provenance:
            raise ForceObjectiveError("objective provenance is invalid")
        if self.semantic_fingerprint != FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT:
            raise ForceObjectiveError("force objective semantic fingerprint differs")

        formal_sums = tuple(_finite(value, "formal bin sum") for value in self.formal_bin_sum_n)
        legacy_sums = tuple(_finite(value, "legacy bin sum") for value in self.legacy_bin_sum_n)
        formal_counts = tuple(self.formal_bin_count)
        legacy_counts = tuple(self.legacy_bin_count)
        if len(formal_sums) != REQUIRED_BINS or len(legacy_sums) != REQUIRED_BINS:
            raise ForceObjectiveError("sealed formal/legacy sums must contain 550 bins")
        for role, counts in (("formal", formal_counts), ("legacy", legacy_counts)):
            if len(counts) != REQUIRED_BINS or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in counts
            ):
                raise ForceObjectiveError(f"sealed {role} bin counts are invalid")
        if sum(formal_counts) > self.sample_count or sum(legacy_counts) > self.sample_count:
            raise ForceObjectiveError("per-bin sufficient statistics exceed bound sample count")
        expected_formal_ids = tuple(index for index, count in enumerate(formal_counts) if count)
        expected_legacy_ids = tuple(index for index, count in enumerate(legacy_counts) if count)
        if expected_formal_ids != self.formal_bin_ids:
            raise ForceObjectiveError("formal bin identities differ from sealed statistics")
        if expected_legacy_ids != self.legacy_bin_ids:
            raise ForceObjectiveError("legacy bin identities differ from sealed statistics")
        expected_stats_digest = _stats_digest(
            formal_sums, formal_counts, legacy_sums, legacy_counts
        )
        if self.sufficient_statistics_digest != expected_stats_digest:
            raise ForceObjectiveError("sufficient-statistics digest differs")
        _require_digest(self.raw_evidence_digest, "raw evidence digest")
        _require_digest(self.sufficient_statistics_digest, "sufficient-statistics digest")
        _require_digest(self.builder_seal_sha256, "builder seal")
        expected_v2 = _mae_from_stats(formal_sums, formal_counts, role="formal")
        expected_legacy = _mae_from_stats(legacy_sums, legacy_counts, role="legacy")
        if expected_v2 is None and self.v2_mae_n is not None:
            raise ForceObjectiveError("incomplete formal statistics cannot carry a v2 MAE")
        if expected_v2 is not None and (
            self.v2_mae_n is None
            or not math.isclose(self.v2_mae_n, expected_v2, rel_tol=0.0, abs_tol=1e-12)
        ):
            raise ForceObjectiveError("v2 MAE differs from sealed sufficient statistics")
        if expected_legacy is None and self.legacy_mae_n is not None:
            raise ForceObjectiveError("incomplete legacy statistics cannot carry a shadow MAE")
        if expected_legacy is not None and (
            self.legacy_mae_n is None
            or not math.isclose(self.legacy_mae_n, expected_legacy, rel_tol=0.0, abs_tol=1e-12)
        ):
            raise ForceObjectiveError("legacy MAE differs from sealed sufficient statistics")
        if self.delta_n is not None and expected_v2 is not None and expected_legacy is not None:
            if not math.isclose(
                self.delta_n,
                expected_legacy - expected_v2,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ForceObjectiveError("objective delta differs from sealed sufficient statistics")
        receipt = {
            "receipt_version": FORCE_OBJECTIVE_RECEIPT_VERSION,
            "semantic_fingerprint": self.semantic_fingerprint,
            "raw_evidence_digest": self.raw_evidence_digest,
            "sufficient_statistics_digest": self.sufficient_statistics_digest,
            "sample_identity_digest": self.sample_identity_digest,
            "sample_identity_count": identity_count,
            "sample_count": self.sample_count,
            "deduplicated_replays": self.deduplicated_replays,
            "formal_bin_sum_n": list(formal_sums),
            "formal_bin_count": list(formal_counts),
            "legacy_bin_sum_n": list(legacy_sums),
            "legacy_bin_count": list(legacy_counts),
            "provenance": self.provenance,
        }
        expected_seal = _sha256(_canonical(receipt, "builder seal"))
        if self.builder_seal_sha256 != expected_seal:
            raise ForceObjectiveError("builder seal differs from bound receipt")
        object.__setattr__(self, "formal_bin_sum_n", formal_sums)
        object.__setattr__(self, "legacy_bin_sum_n", legacy_sums)
        object.__setattr__(self, "formal_bin_count", formal_counts)
        object.__setattr__(self, "legacy_bin_count", legacy_counts)
        object.__setattr__(self, "sample_identity_count", identity_count)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def trainable(self) -> bool:
        """ForceObjective is always advisory and never optimizer-eligible.

        Eligibility is intentionally represented by the separate
        ``VerifiedObservationEvidence`` wrapper in the r005 observation
        ledger.  Keeping this value permanently false makes direct
        construction, ``dataclasses.replace`` and complete caller mappings
        harmless even when every unkeyed receipt field is fabricated.
        """

        return False

    def validate_sealed(self) -> None:
        """Reassert the receipt boundary before an optimizer tell/ask."""

        # Re-run the complete deterministic validation rather than checking
        # only that two digest strings are present.  This also catches an
        # in-memory object altered through an unsafe ``object.__setattr__``
        # caller before it reaches CUDA tell/ask.
        self.__post_init__()

    @property
    def mae_n(self) -> float | None:
        return None

    @property
    def objective(self) -> float | None:
        return self.mae_n

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "target_force_n": self.target_force_n,
            "formal_window_s": list(self.formal_window_s),
            "legacy_shadow_version": LEGACY_SHADOW_VERSION,
            "legacy_window_s": list(self.legacy_window_s),
            "bin_width_s": self.bin_width_s,
            "required_bins": self.required_bins,
            "complete_bins": self.complete_bins,
            "legacy_complete_bins": self.legacy_complete_bins,
            "coverage_bitmap": self.coverage_bitmap,
            "formal_bin_ids": list(self.formal_bin_ids),
            "legacy_bin_ids": list(self.legacy_bin_ids),
            "sample_identity_digest": self.sample_identity_digest,
            "sample_count": self.sample_count,
            "deduplicated_replays": self.deduplicated_replays,
            "force_mae_v2": self.v2_mae_n,
            "v2_mae_n": self.v2_mae_n,
            "r004_legacy_shadow": self.legacy_mae_n,
            "legacy_mae_n": self.legacy_mae_n,
            "delta_n": self.delta_n,
            "trainable": self.trainable,
            "provenance": self.provenance,
            "semantic_fingerprint": self.semantic_fingerprint,
            "receipt_version": FORCE_OBJECTIVE_RECEIPT_VERSION,
            "raw_evidence_digest": self.raw_evidence_digest,
            "sufficient_statistics_digest": self.sufficient_statistics_digest,
            "builder_seal_sha256": self.builder_seal_sha256,
            "formal_bin_sum_n": list(self.formal_bin_sum_n),
            "formal_bin_count": list(self.formal_bin_count),
            "legacy_bin_sum_n": list(self.legacy_bin_sum_n),
            "legacy_bin_count": list(self.legacy_bin_count),
            "sample_identity_count": self.sample_identity_count,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ForceObjective":
        if not isinstance(value, Mapping):
            raise ForceObjectiveError("force objective payload is not an object")
        required_receipt = {
            "receipt_version",
            "semantic_fingerprint",
            "raw_evidence_digest",
            "sufficient_statistics_digest",
            "builder_seal_sha256",
            "formal_bin_sum_n",
            "formal_bin_count",
            "legacy_bin_sum_n",
            "legacy_bin_count",
            "sample_identity_count",
        }
        missing_receipt = sorted(field for field in required_receipt if field not in value)
        if missing_receipt:
            raise ForceObjectiveError(
                "sealed sufficient-statistics receipt is incomplete: "
                + ",".join(missing_receipt)
            )
        if value.get("receipt_version") != FORCE_OBJECTIVE_RECEIPT_VERSION:
            raise ForceObjectiveError("force objective receipt version differs")
        if "verification_state" in value:
            raise ForceObjectiveError(
                "caller-controlled verified objective state is not a ForceObjective field"
            )
        if value.get("sample_identity_ids") not in (None, [], ()):
            raise ForceObjectiveError("raw sample identity IDs are not accepted in a ledger objective")
        try:
            formal_window = tuple(float(item) for item in value["formal_window_s"])
            legacy_window = tuple(float(item) for item in value["legacy_window_s"])
            sample_ids = tuple(str(item) for item in value.get("sample_identity_ids", ()))
            return cls(
                schema=str(value["schema"]),
                version=str(value["version"]),
                target_force_n=float(value["target_force_n"]),
                formal_window_s=formal_window,  # type: ignore[arg-type]
                legacy_window_s=legacy_window,  # type: ignore[arg-type]
                bin_width_s=float(value["bin_width_s"]),
                required_bins=int(value["required_bins"]),
                complete_bins=int(value["complete_bins"]),
                coverage_bitmap=str(value["coverage_bitmap"]),
                formal_bin_ids=tuple(int(item) for item in value["formal_bin_ids"]),
                legacy_bin_ids=tuple(int(item) for item in value["legacy_bin_ids"]),
                sample_identity_digest=str(value["sample_identity_digest"]),
                sample_identity_ids=sample_ids,
                sample_count=int(value["sample_count"]),
                deduplicated_replays=int(value["deduplicated_replays"]),
                v2_mae_n=None if value.get("v2_mae_n") is None else float(value["v2_mae_n"]),
                legacy_mae_n=(
                    None if value.get("legacy_mae_n") is None else float(value["legacy_mae_n"])
                ),
                delta_n=None if value.get("delta_n") is None else float(value["delta_n"]),
                provenance=str(value.get("provenance", "raw_path_evidence")),
                legacy_complete_bins=int(value.get("legacy_complete_bins", len(value["legacy_bin_ids"]))),
                metadata=value.get("metadata", {}),
                semantic_fingerprint=str(value["semantic_fingerprint"]),
                raw_evidence_digest=str(value["raw_evidence_digest"]),
                sufficient_statistics_digest=str(value["sufficient_statistics_digest"]),
                builder_seal_sha256=str(value["builder_seal_sha256"]),
                formal_bin_sum_n=tuple(float(item) for item in value["formal_bin_sum_n"]),
                formal_bin_count=tuple(int(item) for item in value["formal_bin_count"]),
                legacy_bin_sum_n=tuple(float(item) for item in value["legacy_bin_sum_n"]),
                legacy_bin_count=tuple(int(item) for item in value["legacy_bin_count"]),
                sample_identity_count=int(value["sample_identity_count"]),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ForceObjectiveError("force objective payload is invalid") from exc


class ForceObjectiveBuilder:
    """Build one objective from a single immutable evidence set."""

    def __init__(self) -> None:
        self._formal: dict[int, list[float]] = {}
        self._legacy: dict[int, list[float]] = {}
        self._identities: dict[str, str] = {}
        # Unique samples keyed by identity payload (sorted source_sequences).
        # Digests are computed only on collision during add(); finalize fills
        # _identities for receipt digests.
        self._samples: dict[tuple[tuple[str, int | str], ...], ForcePathSample] = {}
        self._last_path_time_s: float | None = None
        self._replays = 0

    @staticmethod
    def _bin(path_time_s: float, start_s: float, end_s: float) -> int | None:
        if path_time_s < start_s or path_time_s >= end_s:
            return None
        index = math.floor((path_time_s - start_s) / BIN_WIDTH_S + 1e-9)
        return index if 0 <= index < REQUIRED_BINS else None

    def add(self, sample: ForcePathSample) -> bool:
        if not isinstance(sample, ForcePathSample):
            raise ForceObjectiveError("objective builder accepts only ForcePathSample")
        identity_key = tuple(sorted(sample.source_sequences.items()))
        prior = self._samples.get(identity_key)
        if prior is not None:
            # Digests only on collision: identity confirms key; payload is conflict vs replay.
            prior_identity = prior.identity_digest
            sample_identity = sample.identity_digest
            if prior_identity != sample_identity or prior.payload_digest != sample.payload_digest:
                raise ForceObjectiveError("sample identity was reused with a conflicting payload")
            self._replays += 1
            return False
        if self._last_path_time_s is not None and sample.path_time_s < self._last_path_time_s:
            raise ForceObjectiveError("PATH-relative time regressed")
        self._last_path_time_s = sample.path_time_s
        self._samples[identity_key] = sample
        formal_index = self._bin(sample.path_time_s, FORMAL_START_S, FORMAL_END_S)
        if formal_index is not None:
            self._formal.setdefault(formal_index, []).append(sample.filtered_normal_n)
        legacy_index = self._bin(sample.path_time_s, LEGACY_START_S, LEGACY_END_S)
        if legacy_index is not None:
            self._legacy.setdefault(legacy_index, []).append(sample.filtered_normal_n)
        return True

    def extend(self, samples: Any) -> None:
        for sample in samples:
            self.add(sample)

    @staticmethod
    def _mae(bins: Mapping[int, list[float]]) -> float | None:
        if len(bins) != REQUIRED_BINS or any(not values for values in bins.values()):
            return None
        return statistics.fmean(
            abs(statistics.fmean(values) - TARGET_FORCE_N)
            for index in range(REQUIRED_BINS)
            for values in (bins.get(index, []),)
        )

    def finalize(self, *, provenance: str = "raw_path_evidence") -> ForceObjective:
        formal_ids = tuple(sorted(self._formal))
        legacy_ids = tuple(sorted(self._legacy))
        # Digests for all unique retained samples (bit-identical receipt fields).
        self._identities = {
            sample.identity_digest: sample.payload_digest
            for sample in self._samples.values()
        }
        identity_ids = tuple(sorted(self._identities))
        identity_digest = _sha256(_canonical(identity_ids, "sample identity digest"))
        bitmap = "".join("1" if index in self._formal else "0" for index in range(REQUIRED_BINS))
        v2 = self._mae(self._formal)
        legacy = self._mae(self._legacy)
        formal_sums = tuple(
            math.fsum(self._formal.get(index, ())) for index in range(REQUIRED_BINS)
        )
        formal_counts = tuple(
            len(self._formal.get(index, ())) for index in range(REQUIRED_BINS)
        )
        legacy_sums = tuple(
            math.fsum(self._legacy.get(index, ())) for index in range(REQUIRED_BINS)
        )
        legacy_counts = tuple(
            len(self._legacy.get(index, ())) for index in range(REQUIRED_BINS)
        )
        stats_digest = _stats_digest(
            formal_sums, formal_counts, legacy_sums, legacy_counts
        )
        raw_evidence_digest = _sha256(
            _canonical(
                [
                    {"identity": identity, "payload_digest": self._identities[identity]}
                    for identity in identity_ids
                ],
                "raw evidence digest",
            )
        )
        receipt = {
            "receipt_version": FORCE_OBJECTIVE_RECEIPT_VERSION,
            "semantic_fingerprint": FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
            "raw_evidence_digest": raw_evidence_digest,
            "sufficient_statistics_digest": stats_digest,
            "sample_identity_digest": identity_digest,
            "sample_identity_count": len(identity_ids),
            "sample_count": len(self._samples),
            "deduplicated_replays": self._replays,
            "formal_bin_sum_n": list(formal_sums),
            "formal_bin_count": list(formal_counts),
            "legacy_bin_sum_n": list(legacy_sums),
            "legacy_bin_count": list(legacy_counts),
            "provenance": provenance,
        }
        builder_seal = _sha256(_canonical(receipt, "builder seal"))
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
            # The complete IDs stay in the builder's in-memory receipt only;
            # the durable objective carries the aggregate digest/count.
            sample_identity_ids=(),
            sample_count=len(self._samples),
            deduplicated_replays=self._replays,
            v2_mae_n=v2,
            legacy_mae_n=legacy,
            delta_n=None if v2 is None or legacy is None else legacy - v2,
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


__all__ = [
    "BIN_WIDTH_S",
    "FORMAL_END_S",
    "FORMAL_START_S",
    "FORCE_OBJECTIVE_SCHEMA",
    "FORCE_OBJECTIVE_VERSION",
    "FORCE_OBJECTIVE_RECEIPT_VERSION",
    "FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT",
    "ForceObjective",
    "ForceObjectiveBuilder",
    "ForceObjectiveError",
    "ForcePathSample",
    "LEGACY_END_S",
    "LEGACY_SHADOW_VERSION",
    "LEGACY_START_S",
    "MAX_SOURCE_AGE_S",
    "PATH_STAGE",
    "REQUIRED_BINS",
    "TARGET_FORCE_N",
]
