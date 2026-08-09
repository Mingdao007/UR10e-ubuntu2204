"""Deterministic R010 GP calibration dataset, CV policy, and artifact schema."""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from step5d_force_objective import FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT


CALIBRATION_SCHEMA = "step5d.autotune-v4/r010-gp-calibration-v1"
CALIBRATION_POLICY_VERSION = "r010-gp-calibration-policy-v1"
NOISE_FLOOR_GRID_N2 = (
    2.5e-5,
    1.0e-4,
    2.5e-4,
    5.0e-4,
    1.0e-3,
    2.5e-3,
    5.0e-3,
    1.0e-2,
    2.0e-2,
)
VARIANCE_ESTIMATOR = "statistics.variance"
CV_SEED = 20260809
CV_FOLDS = 5
PREDICTIVE_COVERAGE_INTERVAL = (0.925, 0.975)
NLPD_TIE_EPSILON = 0.01
EXPECTED_ADMITTED_ROWS = 567
EXPECTED_TIMING_INELIGIBLE_ROWS = 13
FEATURE_NAMES = (
    "log2_p_over_d",
    "log2_d",
    "log2_i_over_p",
    "log2_tau",
    "log2_ko",
    "log2_kp",
    "i_mode",
)
CALIBRATED_FEATURE_INDICES = (0, 1, 3, 4, 5)
NOT_IDENTIFIABLE_FEATURE_INDICES = (2, 6)
GAIN_KEYS = (
    "force_p_gain",
    "force_damping",
    "force_i_gain",
    "normal_filter_tau_s",
    "orientation_ko",
    "motion_kp",
    "i_off",
)
REQUIRED_TRUE_GATES = (
    "sealed",
    "eligible",
    "binding_ok",
    "identity_gate",
    "timing_gate",
    "contact_gate",
    "safety_gate",
    "return_gate",
    "safe_return",
)
ALLOWED_KINDS = frozenset({"ANCHOR", "BO_TRIAL", "SPACEFILL", "STAIRCASE"})


class R010CalibrationError(ValueError):
    """Historical evidence or a calibration artifact is not admissible."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise R010CalibrationError(f"value is not canonical JSON: {exc}") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R010CalibrationError(f"calibration input is missing or unsafe: {path}")
    return sha256_bytes(path.read_bytes())


def _digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise R010CalibrationError(f"{role} must be a lowercase SHA-256")
    return value


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise R010CalibrationError(f"{role} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise R010CalibrationError(f"{role} must be finite")
    return result


def _candidate_key(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    if not isinstance(candidate, Mapping) or any(key not in candidate for key in GAIN_KEYS):
        raise R010CalibrationError("historical candidate lacks a gain-key field")
    values: list[Any] = []
    for key in GAIN_KEYS:
        value = candidate[key]
        if key == "i_off":
            if not isinstance(value, bool):
                raise R010CalibrationError("candidate i_off must be bool")
            values.append(value)
        else:
            values.append(_finite(value, f"candidate {key}"))
    return tuple(values)


def feature_map(candidate: Mapping[str, Any]) -> tuple[float, ...]:
    """The unchanged R008 seven-dimensional production feature map."""

    key = _candidate_key(candidate)
    p, damping, i_gain, tau, ko, kp, i_off = key
    if min(float(p), float(damping), float(tau), float(ko), float(kp)) <= 0.0:
        raise R010CalibrationError("candidate continuous gains must be positive")
    if float(i_gain) < 0.0:
        raise R010CalibrationError("candidate I gain cannot be negative")
    if bool(i_off) != math.isclose(float(i_gain), 0.0, rel_tol=0.0, abs_tol=0.0):
        raise R010CalibrationError("candidate I gain and i_off disagree")
    return (
        math.log2(float(p) / float(damping)),
        math.log2(float(damping)),
        0.0 if bool(i_off) else math.log2(float(i_gain) / float(p)),
        math.log2(float(tau)),
        math.log2(float(ko)),
        math.log2(float(kp)),
        0.0 if bool(i_off) else 1.0,
    )


@dataclass(frozen=True)
class CalibrationRow:
    attempt_sequence: int
    kind: str
    candidate: Mapping[str, Any]
    objective_n: float
    campaign_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate", MappingProxyType(dict(self.candidate)))

    @property
    def gain_key(self) -> tuple[Any, ...]:
        return _candidate_key(self.candidate)

    @property
    def features(self) -> tuple[float, ...]:
        return feature_map(self.candidate)

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempt_sequence": self.attempt_sequence,
            "kind": self.kind,
            "candidate": dict(self.candidate),
            "objective_n": self.objective_n,
            "campaign_fingerprint": self.campaign_fingerprint,
        }


@dataclass(frozen=True)
class AdmissionResult:
    rows: tuple[CalibrationRow, ...]
    header: Mapping[str, Any]
    input_sha256: str
    total_observation_rows: int
    rejection_counts: Mapping[str, int]
    rejected_attempt_sequences: Mapping[str, tuple[int, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "header", MappingProxyType(dict(self.header)))
        object.__setattr__(self, "rejection_counts", MappingProxyType(dict(self.rejection_counts)))
        object.__setattr__(
            self,
            "rejected_attempt_sequences",
            MappingProxyType({key: tuple(value) for key, value in self.rejected_attempt_sequences.items()}),
        )

    @property
    def dataset_sha256(self) -> str:
        return sha256_bytes(canonical_bytes([row.as_dict() for row in self.rows]))


def _strict_json_lines(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R010CalibrationError(f"historical ledger is missing or unsafe: {path}")
    result: list[dict[str, Any]] = []

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for key, value in pairs:
            if key in row:
                raise R010CalibrationError(f"historical ledger repeats key {key!r}")
            row[key] = value
        return row

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise R010CalibrationError(f"historical ledger has blank line {line_number}")
        try:
            row = json.loads(
                line,
                object_pairs_hook=unique_pairs,
                parse_constant=lambda token: (_ for _ in ()).throw(
                    R010CalibrationError(f"historical ledger has {token}")
                ),
            )
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise R010CalibrationError(f"historical ledger line {line_number} is invalid") from exc
        if not isinstance(row, dict):
            raise R010CalibrationError(f"historical ledger line {line_number} is not an object")
        result.append(row)
    if not result:
        raise R010CalibrationError("historical ledger is empty")
    return result


def _cold_objective(row: Mapping[str, Any]) -> float | None:
    objective = row.get("force_objective")
    raw = row.get("raw_artifact")
    if not isinstance(objective, Mapping) or not isinstance(raw, Mapping):
        return None
    if objective.get("semantic_fingerprint") != FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT:
        return None
    if objective.get("version") != "force_mae_v2":
        return None
    if raw.get("verification_state") != "verified_fresh_subprocess":
        return None
    verified = raw.get("verified_receipt")
    if not isinstance(verified, Mapping):
        return None
    if verified.get("semantic_fingerprint") != FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT:
        return None
    value = objective.get("force_mae_v2", objective.get("v2_mae_n"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or value < 0.0:
        return None
    for alias in (row.get("objective"), row.get("mae_n")):
        if isinstance(alias, bool) or not isinstance(alias, (int, float)):
            return None
        if not math.isclose(float(alias), value, rel_tol=0.0, abs_tol=1e-12):
            return None
    return value


def admit_phase5_ledger(
    path: Path,
    *,
    expected_rows: int | None = EXPECTED_ADMITTED_ROWS,
    expected_timing_ineligible: int | None = EXPECTED_TIMING_INELIGIBLE_ROWS,
) -> AdmissionResult:
    """Rebuild Phase5 through the production sealed/eligible/gate predicate."""

    records = _strict_json_lines(path)
    header = records[0]
    if header.get("record_type") != "header":
        raise R010CalibrationError("historical ledger header differs")
    campaign = _digest(header.get("campaign_fingerprint"), "historical campaign")
    rows: list[CalibrationRow] = []
    rejection_counts: dict[str, int] = {}
    rejected: dict[str, list[int]] = {}
    total = 0
    seen_sequences: set[int] = set()

    def reject(reason: str, sequence: int) -> None:
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        rejected.setdefault(reason, []).append(sequence)

    for record in records[1:]:
        if record.get("record_type") != "observation":
            raise R010CalibrationError("historical ledger contains a non-observation record")
        total += 1
        sequence = record.get("attempt_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise R010CalibrationError("historical attempt sequence is invalid")
        if sequence in seen_sequences:
            raise R010CalibrationError("historical attempt sequence repeats")
        seen_sequences.add(sequence)
        if record.get("campaign_fingerprint") != campaign:
            raise R010CalibrationError("historical observation campaign differs")
        if record.get("sealed") is not True:
            reject("not_sealed", sequence)
            continue
        if record.get("eligible") is not True:
            reason = (
                "timing_ineligible"
                if record.get("timing_gate") is False
                and record.get("kind") in ALLOWED_KINDS
                and _cold_objective(record) is not None
                else "not_eligible"
            )
            reject(reason, sequence)
            continue
        failed_gate = next((gate for gate in REQUIRED_TRUE_GATES if record.get(gate) is not True), None)
        if failed_gate is not None:
            reject(f"gate_{failed_gate}", sequence)
            continue
        kind = record.get("kind")
        if kind not in ALLOWED_KINDS:
            reject("kind_not_trainable", sequence)
            continue
        objective = _cold_objective(record)
        if objective is None:
            reject("cold_objective_invalid", sequence)
            continue
        candidate = record.get("candidate")
        try:
            if not isinstance(candidate, Mapping):
                raise R010CalibrationError("candidate is not an object")
            feature_map(candidate)
        except R010CalibrationError:
            reject("candidate_invalid", sequence)
            continue
        rows.append(
            CalibrationRow(
                attempt_sequence=sequence,
                kind=str(kind),
                candidate=dict(candidate),
                objective_n=objective,
                campaign_fingerprint=campaign,
            )
        )

    rows.sort(key=lambda row: row.attempt_sequence)
    if expected_rows is not None and len(rows) != expected_rows:
        raise R010CalibrationError(
            f"production admission returned {len(rows)} rows, expected {expected_rows}"
        )
    timing_count = rejection_counts.get("timing_ineligible", 0)
    if expected_timing_ineligible is not None and timing_count != expected_timing_ineligible:
        raise R010CalibrationError(
            f"production admission rejected {timing_count} timing rows, expected {expected_timing_ineligible}"
        )
    if rows and any(row.features[6] != 0.0 or row.features[2] != 0.0 for row in rows):
        raise R010CalibrationError("Phase5 calibration unexpectedly contains I-on data")
    return AdmissionResult(
        rows=tuple(rows),
        header=header,
        input_sha256=sha256_file(path),
        total_observation_rows=total,
        rejection_counts=dict(sorted(rejection_counts.items())),
        rejected_attempt_sequences={key: tuple(value) for key, value in sorted(rejected.items())},
    )


def _group_rows(rows: Sequence[CalibrationRow]) -> dict[tuple[Any, ...], list[CalibrationRow]]:
    groups: dict[tuple[Any, ...], list[CalibrationRow]] = {}
    for row in rows:
        groups.setdefault(row.gain_key, []).append(row)
    return groups


def deterministic_grouped_folds(
    rows: Sequence[CalibrationRow],
    *,
    folds: int = CV_FOLDS,
    seed: int = CV_SEED,
) -> tuple[tuple[tuple[Any, ...], ...], ...]:
    """Split gain keys into deterministic MAE-octile/kind-stratified folds."""

    if folds != 5:
        raise R010CalibrationError("R010 calibration requires deterministic 5-fold CV")
    groups = _group_rows(rows)
    if len(groups) < folds:
        raise R010CalibrationError("calibration has fewer gain groups than folds")
    ordered = sorted(
        groups,
        key=lambda key: (statistics.fmean(row.objective_n for row in groups[key]), canonical_bytes(list(key))),
    )
    octile_for = {
        key: min(7, (index * 8) // len(ordered))
        for index, key in enumerate(ordered)
    }
    strata: dict[tuple[int, str], list[tuple[Any, ...]]] = {}
    for key in ordered:
        kinds = "+".join(sorted({row.kind for row in groups[key]}))
        strata.setdefault((octile_for[key], kinds), []).append(key)
    result: list[list[tuple[Any, ...]]] = [[] for _ in range(folds)]
    for stratum, keys in sorted(strata.items()):
        ranked = sorted(
            keys,
            key=lambda key: sha256_bytes(
                canonical_bytes({"seed": seed, "stratum": list(stratum), "gain_key": list(key)})
            ),
        )
        offset = int(
            sha256_bytes(canonical_bytes({"seed": seed, "stratum": list(stratum)}))[:8], 16
        ) % folds
        for index, key in enumerate(ranked):
            result[(offset + index) % folds].append(key)
    if any(not fold for fold in result):
        raise R010CalibrationError("deterministic CV produced an empty fold")
    normalized = tuple(tuple(sorted(fold, key=lambda key: canonical_bytes(list(key)))) for fold in result)
    flattened = [key for fold in normalized for key in fold]
    if len(flattened) != len(groups) or len(set(flattened)) != len(groups):
        raise R010CalibrationError("deterministic CV does not partition gain keys")
    return normalized


def fold_assignment_sha256(folds: Sequence[Sequence[tuple[Any, ...]]]) -> str:
    return sha256_bytes(canonical_bytes([[list(key) for key in fold] for fold in folds]))


def grouped_training_rows(
    rows: Sequence[CalibrationRow],
    *,
    noise_floor_n2: float,
) -> tuple[dict[str, Any], ...]:
    """Deduplicate gain keys using sample variance, never population variance."""

    floor = _finite(noise_floor_n2, "noise floor")
    if floor not in NOISE_FLOOR_GRID_N2:
        raise R010CalibrationError("noise floor is outside the R010 candidate grid")
    groups = _group_rows(rows)
    result: list[dict[str, Any]] = []
    for key in sorted(groups, key=lambda item: canonical_bytes(list(item))):
        group = groups[key]
        objectives = [row.objective_n for row in group]
        variance = statistics.variance(objectives) if len(objectives) > 1 else floor
        result.append(
            {
                "gain_key": list(key),
                "features": list(group[0].features),
                "objective_n": statistics.fmean(objectives),
                "noise_n2": max(floor, variance),
                "repeat_count": len(objectives),
            }
        )
    return tuple(result)


@dataclass(frozen=True)
class FoldMetric:
    fold: int
    coverage95: float
    mean_nlpd: float
    lengthscales: tuple[float, ...]
    boundary_hits: tuple[bool, ...]
    test_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "fold": self.fold,
            "coverage95": self.coverage95,
            "mean_nlpd": self.mean_nlpd,
            "lengthscales": list(self.lengthscales),
            "boundary_hits": list(self.boundary_hits),
            "test_count": self.test_count,
        }


@dataclass(frozen=True)
class NoiseCandidateResult:
    noise_floor_n2: float
    folds: tuple[FoldMetric, ...]

    @property
    def coverage95(self) -> float:
        total = sum(fold.test_count for fold in self.folds)
        return sum(fold.coverage95 * fold.test_count for fold in self.folds) / total

    @property
    def mean_nlpd(self) -> float:
        total = sum(fold.test_count for fold in self.folds)
        return sum(fold.mean_nlpd * fold.test_count for fold in self.folds) / total

    def as_dict(self) -> dict[str, Any]:
        return {
            "noise_floor_n2": self.noise_floor_n2,
            "coverage95": self.coverage95,
            "mean_nlpd": self.mean_nlpd,
            "folds": [fold.as_dict() for fold in self.folds],
        }


def select_noise_floor(results: Sequence[NoiseCandidateResult]) -> NoiseCandidateResult:
    if {result.noise_floor_n2 for result in results} != set(NOISE_FLOOR_GRID_N2):
        raise R010CalibrationError("noise calibration did not evaluate the complete grid")
    eligible = [
        result
        for result in results
        if PREDICTIVE_COVERAGE_INTERVAL[0]
        <= result.coverage95
        <= PREDICTIVE_COVERAGE_INTERVAL[1]
    ]
    if not eligible:
        raise R010CalibrationError("no noise-floor candidate satisfies predictive coverage")
    eligible.sort(key=lambda result: (result.mean_nlpd, result.noise_floor_n2))
    best_nlpd = eligible[0].mean_nlpd
    tied = [result for result in eligible if result.mean_nlpd - best_nlpd < NLPD_TIE_EPSILON]
    return min(tied, key=lambda result: result.noise_floor_n2)


def lengthscale_policy(
    selected: NoiseCandidateResult,
    *,
    span_initialization: Sequence[float],
) -> dict[str, Any]:
    if len(span_initialization) != len(FEATURE_NAMES):
        raise R010CalibrationError("span initialization must cover seven features")
    spans = tuple(_finite(value, "span initialization") for value in span_initialization)
    if any(value <= 0.0 for value in spans):
        raise R010CalibrationError("span initialization must be positive")
    active_values: dict[int, list[float]] = {index: [] for index in CALIBRATED_FEATURE_INDICES}
    active_hits: dict[int, int] = {index: 0 for index in CALIBRATED_FEATURE_INDICES}
    for fold in selected.folds:
        if len(fold.lengthscales) != len(FEATURE_NAMES) or len(fold.boundary_hits) != len(FEATURE_NAMES):
            raise R010CalibrationError("fold lengthscale vectors must have seven entries")
        for index in CALIBRATED_FEATURE_INDICES:
            value = _finite(fold.lengthscales[index], "fold lengthscale")
            if value <= 0.0:
                raise R010CalibrationError("fold lengthscale must be positive")
            active_values[index].append(value)
            active_hits[index] += int(fold.boundary_hits[index])
    values = list(spans)
    confidence: list[dict[str, Any]] = []
    for index, name in enumerate(FEATURE_NAMES):
        if index in NOT_IDENTIFIABLE_FEATURE_INDICES:
            values[index] = spans[index]
            confidence.append(
                {
                    "feature": name,
                    "status": "not_identifiable",
                    "value": values[index],
                    "reason": "Phase5 production admission contains I-off only",
                }
            )
            continue
        fold_values = active_values[index]
        log_median = math.exp(statistics.median(math.log(value) for value in fold_values))
        ratio = max(fold_values) / min(fold_values)
        low = active_hits[index] >= 2 or ratio > 4.0
        values[index] = spans[index] if low else log_median
        confidence.append(
            {
                "feature": name,
                "status": "low_confidence_fallback" if low else "calibrated",
                "value": values[index],
                "log_space_median": log_median,
                "fold_max_min_ratio": ratio,
                "boundary_hit_folds": active_hits[index],
            }
        )
    return {
        "policy": "five_fold_log_space_median_with_span_fallback",
        "continuous_calibrated_indices": list(CALIBRATED_FEATURE_INDICES),
        "not_identifiable_indices": list(NOT_IDENTIFIABLE_FEATURE_INDICES),
        "shared": values,
        "same_mode": values,
        "i_on_only": spans,
        "i_on_only_policy": "existing_broad_prior",
        "confidence": confidence,
    }


def span_initialization(rows: Sequence[CalibrationRow]) -> tuple[float, ...]:
    columns = list(zip(*(row.features for row in rows), strict=True))
    result = []
    for values in columns:
        span = max(values) - min(values)
        result.append(max(1.0, float(span)))
    return tuple(result)


def build_calibration_artifact(
    admission: AdmissionResult,
    results: Sequence[NoiseCandidateResult],
    *,
    kernel_implementation_sha256: str,
    source_path_label: str,
) -> dict[str, Any]:
    selected = select_noise_floor(results)
    folds = deterministic_grouped_folds(admission.rows)
    spans = span_initialization(admission.rows)
    lengths = lengthscale_policy(selected, span_initialization=spans)
    artifact: dict[str, Any] = {
        "schema": CALIBRATION_SCHEMA,
        "policy_version": CALIBRATION_POLICY_VERSION,
        "source": {
            "label": source_path_label,
            "ledger_sha256": admission.input_sha256,
            "historical_campaign_fingerprint": admission.header["campaign_fingerprint"],
            "production_admission_predicate": {
                "required_true": list(REQUIRED_TRUE_GATES),
                "allowed_kinds": sorted(ALLOWED_KINDS),
                "cold_read_objective": True,
                "penalties": False,
                "shadow_observations": False,
                "historical_seed": False,
            },
            "total_observation_rows": admission.total_observation_rows,
            "admitted_rows": len(admission.rows),
            "dataset_sha256": admission.dataset_sha256,
            "rejection_counts": dict(admission.rejection_counts),
            "rejected_attempt_sequences": {
                key: list(value) for key, value in admission.rejected_attempt_sequences.items()
            },
        },
        "feature_map": {
            "version": "r008-log2-p-over-d-v1",
            "names": list(FEATURE_NAMES),
            "calibrated_indices": list(CALIBRATED_FEATURE_INDICES),
            "not_identifiable_indices": list(NOT_IDENTIFIABLE_FEATURE_INDICES),
            "phase5_i_mode": "off_only",
        },
        "kernel": {
            "family": "conditional_matern52_shared_same_mode_i_on_only",
            "implementation_sha256": _digest(kernel_implementation_sha256, "kernel implementation"),
            "family_changed": False,
            "diag_mask_fixed": True,
            "calibration_fit_view": "i_off_tied_shared_same_effective_matern52",
            "shared_same_initialization_tied": True,
        },
        "noise": {
            "candidate_grid_n2": list(NOISE_FLOOR_GRID_N2),
            "variance_estimator": VARIANCE_ESTIMATOR,
            "predictive_coverage_interval": list(PREDICTIVE_COVERAGE_INTERVAL),
            "nlpd_tie_epsilon": NLPD_TIE_EPSILON,
            "selected_floor_n2": selected.noise_floor_n2,
            "selected_coverage95": selected.coverage95,
            "selected_mean_nlpd": selected.mean_nlpd,
            "candidates": [result.as_dict() for result in sorted(results, key=lambda item: item.noise_floor_n2)],
        },
        "cross_validation": {
            "folds": CV_FOLDS,
            "seed": CV_SEED,
            "group_key": list(GAIN_KEYS),
            "stratification": ["mae_octile", "kind"],
            "assignment_sha256": fold_assignment_sha256(folds),
            "fold_group_counts": [len(fold) for fold in folds],
        },
        "lengthscales": lengths,
        "runtime_policy": {
            "role": "initialization_and_prior",
            "historical_observations_imported": False,
            "new_ledger_refit": True,
            "freeze_after_existing_second_group": True,
        },
    }
    artifact["calibration_sha256"] = sha256_bytes(canonical_bytes(artifact))
    return artifact


def validate_calibration_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise R010CalibrationError("calibration artifact must be an object")
    required = {
        "schema",
        "policy_version",
        "source",
        "feature_map",
        "kernel",
        "noise",
        "cross_validation",
        "lengthscales",
        "runtime_policy",
        "calibration_sha256",
    }
    if set(value) != required:
        raise R010CalibrationError("calibration artifact fields differ")
    if value.get("schema") != CALIBRATION_SCHEMA or value.get("policy_version") != CALIBRATION_POLICY_VERSION:
        raise R010CalibrationError("calibration artifact schema/version differs")
    payload = {key: item for key, item in value.items() if key != "calibration_sha256"}
    if value.get("calibration_sha256") != sha256_bytes(canonical_bytes(payload)):
        raise R010CalibrationError("calibration artifact digest differs")
    source = value.get("source")
    if not isinstance(source, Mapping) or source.get("admitted_rows") != EXPECTED_ADMITTED_ROWS:
        raise R010CalibrationError("calibration artifact admitted-row count differs")
    rejection = source.get("rejection_counts")
    if not isinstance(rejection, Mapping) or rejection.get("timing_ineligible") != EXPECTED_TIMING_INELIGIBLE_ROWS:
        raise R010CalibrationError("calibration artifact timing exclusion differs")
    noise = value.get("noise")
    if not isinstance(noise, Mapping) or tuple(noise.get("candidate_grid_n2", ())) != NOISE_FLOOR_GRID_N2:
        raise R010CalibrationError("calibration noise grid differs")
    if noise.get("variance_estimator") != VARIANCE_ESTIMATOR:
        raise R010CalibrationError("calibration variance estimator differs")
    selected = _finite(noise.get("selected_floor_n2"), "selected noise floor")
    if selected not in NOISE_FLOOR_GRID_N2:
        raise R010CalibrationError("calibration selected noise floor is outside the grid")
    lengths = value.get("lengthscales")
    if not isinstance(lengths, Mapping):
        raise R010CalibrationError("calibration lengthscale policy is absent")
    if lengths.get("not_identifiable_indices") != list(NOT_IDENTIFIABLE_FEATURE_INDICES):
        raise R010CalibrationError("I-related lengthscales are not marked unidentifiable")
    for component in ("shared", "same_mode", "i_on_only"):
        values = lengths.get(component)
        if not isinstance(values, (list, tuple)) or len(values) != 7 or any(_finite(item, component) <= 0 for item in values):
            raise R010CalibrationError(f"calibration {component} lengthscales differ")
    if list(lengths.get("shared", ())) != list(lengths.get("same_mode", ())):
        raise R010CalibrationError("I-off shared and same-mode initializations must match")
    runtime = value.get("runtime_policy")
    if not isinstance(runtime, Mapping) or runtime.get("historical_observations_imported") is not False:
        raise R010CalibrationError("calibration artifact attempts to import old observations")
    return json.loads(canonical_bytes(value).decode("utf-8"))


def load_calibration_artifact(path: Path) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R010CalibrationError(f"calibration artifact is missing or unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise R010CalibrationError("calibration artifact is not strict JSON") from exc
    return validate_calibration_artifact(value)


def synthetic_candidate_results(
    *,
    selected_floor_n2: float = 5.0e-3,
) -> tuple[NoiseCandidateResult, ...]:
    """Deterministic test-only results; never accepted by the release builder."""

    results = []
    for floor in NOISE_FLOOR_GRID_N2:
        coverage = 0.95 if floor == selected_floor_n2 else 0.8
        nlpd = 0.5 if floor == selected_floor_n2 else 2.0 + floor
        folds = tuple(
            FoldMetric(
                fold=index,
                coverage95=coverage,
                mean_nlpd=nlpd,
                lengthscales=(1.0, 2.0, 1.0, 1.5, 1.2, 1.1, 1.0),
                boundary_hits=(False,) * 7,
                test_count=10,
            )
            for index in range(CV_FOLDS)
        )
        results.append(NoiseCandidateResult(noise_floor_n2=floor, folds=folds))
    return tuple(results)


__all__ = [
    "ALLOWED_KINDS",
    "CALIBRATED_FEATURE_INDICES",
    "CALIBRATION_POLICY_VERSION",
    "CALIBRATION_SCHEMA",
    "CV_FOLDS",
    "CV_SEED",
    "CalibrationRow",
    "EXPECTED_ADMITTED_ROWS",
    "EXPECTED_TIMING_INELIGIBLE_ROWS",
    "FEATURE_NAMES",
    "FoldMetric",
    "NOISE_FLOOR_GRID_N2",
    "NOT_IDENTIFIABLE_FEATURE_INDICES",
    "NoiseCandidateResult",
    "R010CalibrationError",
    "VARIANCE_ESTIMATOR",
    "admit_phase5_ledger",
    "build_calibration_artifact",
    "canonical_bytes",
    "deterministic_grouped_folds",
    "feature_map",
    "fold_assignment_sha256",
    "grouped_training_rows",
    "lengthscale_policy",
    "load_calibration_artifact",
    "select_noise_floor",
    "sha256_file",
    "span_initialization",
    "synthetic_candidate_results",
    "validate_calibration_artifact",
]
