"""Hierarchical observation-noise calibration for the offline R011 shadow."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .common import R011ValueError, digest, finite, freeze_tree, json_tree, positive


NOISE_SCHEMA = "step5d.autotune-v4/r011-observation-noise-v1"
NOISE_ATTESTATION_SCHEMA = "step5d.autotune-v4/r011-observation-noise-attestation-v1"
NOISE_POLICY_VERSION = "r011-hierarchical-log-shrink-v1"
VARIANCE_ESTIMATOR = "statistics.variance"
NOISE_FLOOR_N2 = 0.005
CALIBRATION_GRID_N2 = (2.5e-5, 1.0e-4, 2.5e-4, 5.0e-4, 1.0e-3, 2.5e-3, 5.0e-3, 1.0e-2, 2.0e-2)
SHRINK_PRIOR_GROUPS = 4.0
I_AXES = ("force_i_gain", "i_mode")


class ObservationNoiseError(R011ValueError):
    """Noise observations or calibration attestations are invalid."""


def _key(value: Any, role: str) -> str:
    if not isinstance(value, str) or not value:
        raise ObservationNoiseError(f"{role} must be a non-empty string")
    return value


@dataclass(frozen=True)
class NoiseObservation:
    observation_id: str
    kind: str
    campaign_epoch: str
    gain_key: tuple[Any, ...]
    objective_n: float
    full_observation: bool = True
    i_values: tuple[Any, ...] = ()
    exact_observation: bool = True

    def __post_init__(self) -> None:
        _key(self.observation_id, "observation_id")
        _key(self.kind, "kind")
        _key(self.campaign_epoch, "campaign_epoch")
        if not self.gain_key:
            raise ObservationNoiseError("gain_key must not be empty")
        value = finite(self.objective_n, "objective_n")
        if value < 0.0:
            raise ObservationNoiseError("objective_n must not be negative")
        if not isinstance(self.full_observation, bool):
            raise ObservationNoiseError("full_observation must be boolean")
        if not isinstance(self.exact_observation, bool):
            raise ObservationNoiseError("exact_observation must be boolean")
        if not self.exact_observation and self.full_observation:
            raise ObservationNoiseError("non-exact rows cannot be full observations")
        object.__setattr__(self, "gain_key", tuple(json_tree(self.gain_key)))
        object.__setattr__(self, "i_values", tuple(json_tree(self.i_values)))

    @property
    def stratum(self) -> tuple[str, str]:
        return (self.kind, self.campaign_epoch)


@dataclass(frozen=True)
class NoiseRefit:
    identifiable_lengthscales: Mapping[str, float]
    selected_noise_n2: float
    fit_sequence: int

    def __post_init__(self) -> None:
        if isinstance(self.fit_sequence, bool) or self.fit_sequence <= 0:
            raise ObservationNoiseError("fit_sequence must be positive")
        noise = finite(self.selected_noise_n2, "selected_noise_n2")
        if noise < NOISE_FLOOR_N2 or noise not in CALIBRATION_GRID_N2:
            raise ObservationNoiseError("selected noise is outside the calibration-grid bounds")
        for axis, value in self.identifiable_lengthscales.items():
            if not isinstance(axis, str) or not math.isfinite(float(value)) or float(value) <= 0.0:
                raise ObservationNoiseError("identifiable lengthscales must be positive")
        object.__setattr__(self, "identifiable_lengthscales", freeze_tree(dict(sorted(self.identifiable_lengthscales.items()))))


@dataclass(frozen=True)
class ObservationNoiseAttestation:
    strata: Mapping[str, Mapping[str, Any]]
    total_full_observations: int
    repeat_group_count: int
    selected_noise_n2: float
    identifiable_lengthscales: Mapping[str, float]
    i_axes: Mapping[str, Mapping[str, Any]]
    refits: tuple[NoiseRefit, ...]
    stars_drift: Mapping[str, Any] | None = None
    schema: str = NOISE_ATTESTATION_SCHEMA
    policy_version: str = NOISE_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.schema != NOISE_ATTESTATION_SCHEMA or self.policy_version != NOISE_POLICY_VERSION:
            raise ObservationNoiseError("noise attestation schema/version differs")
        if isinstance(self.total_full_observations, bool) or self.total_full_observations < 0:
            raise ObservationNoiseError("total_full_observations is invalid")
        if isinstance(self.repeat_group_count, bool) or self.repeat_group_count < 0:
            raise ObservationNoiseError("repeat_group_count is invalid")
        noise = finite(self.selected_noise_n2, "selected_noise_n2")
        if noise < NOISE_FLOOR_N2 or noise not in CALIBRATION_GRID_N2:
            raise ObservationNoiseError("selected noise is outside calibration-grid bounds")
        if not isinstance(self.refits, tuple):
            raise ObservationNoiseError("refits must be a tuple")
        if set(self.i_axes) != set(I_AXES):
            raise ObservationNoiseError("I-axis identifiability fields differ")
        if not isinstance(self.identifiable_lengthscales, Mapping):
            raise ObservationNoiseError("identifiable_lengthscales must be an object")
        for axis, value in self.identifiable_lengthscales.items():
            if not isinstance(axis, str) or not axis or finite(value, f"lengthscale {axis}") <= 0.0:
                raise ObservationNoiseError("retained lengthscales must be finite and positive")
        object.__setattr__(self, "strata", freeze_tree(json_tree(self.strata)))
        object.__setattr__(self, "i_axes", freeze_tree(json_tree(self.i_axes)))
        object.__setattr__(self, "identifiable_lengthscales", freeze_tree(dict(sorted(self.identifiable_lengthscales.items()))))
        object.__setattr__(self, "stars_drift", None if self.stars_drift is None else freeze_tree(json_tree(self.stars_drift)))

    @property
    def freeze_eligible(self) -> bool:
        return freeze_eligibility(
            total_full_observations=self.total_full_observations,
            repeat_group_count=self.repeat_group_count,
            refits=self.refits,
            i_axes=self.i_axes,
            retained_lengthscales=self.identifiable_lengthscales,
        )["eligible"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "policy_version": self.policy_version,
            "noise_floor_n2": NOISE_FLOOR_N2,
            "calibration_grid_n2": list(CALIBRATION_GRID_N2),
            "variance_estimator": VARIANCE_ESTIMATOR,
            "strata": json_tree(self.strata),
            "total_full_observations": self.total_full_observations,
            "repeat_group_count": self.repeat_group_count,
            "selected_noise_n2": self.selected_noise_n2,
            "identifiable_lengthscales": dict(self.identifiable_lengthscales),
            "i_axes": json_tree(self.i_axes),
            "refits": [
                {
                    "identifiable_lengthscales": dict(refit.identifiable_lengthscales),
                    "selected_noise_n2": refit.selected_noise_n2,
                    "fit_sequence": refit.fit_sequence,
                }
                for refit in self.refits
            ],
            "freeze_eligibility": freeze_eligibility(
                total_full_observations=self.total_full_observations,
                repeat_group_count=self.repeat_group_count,
                refits=self.refits,
                i_axes=self.i_axes,
                retained_lengthscales=self.identifiable_lengthscales,
            ),
            "stars_drift": json_tree(self.stars_drift),
        }

    @property
    def attestation_sha256(self) -> str:
        # STARS drift is diagnostic only and is excluded from model/fingerprint bytes.
        model = self.as_dict()
        model["stars_drift"] = None
        return digest(model)


def _group(observations: Sequence[NoiseObservation]) -> dict[tuple[str, str, tuple[Any, ...]], list[float]]:
    groups: dict[tuple[str, str, tuple[Any, ...]], list[float]] = {}
    for observation in observations:
        if not observation.full_observation or not observation.exact_observation:
            continue
        key = (observation.kind, observation.campaign_epoch, observation.gain_key)
        groups.setdefault(key, []).append(float(observation.objective_n))
    return {key: values for key, values in sorted(groups.items(), key=lambda item: repr(item[0]))}


def _variance(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    # The estimator is intentionally explicit: repeats use sample variance.
    return max(NOISE_FLOOR_N2, float(statistics.variance(values)))


def _log_mean(values: Sequence[float]) -> float:
    return math.exp(statistics.fmean(math.log(max(NOISE_FLOOR_N2, value)) for value in values))


def _shrink(local: float | None, parent: float, sample_count: int) -> float:
    if local is None:
        return parent
    weight = sample_count / (sample_count + SHRINK_PRIOR_GROUPS)
    return math.exp(weight * math.log(local) + (1.0 - weight) * math.log(parent))


def i_axis_identifiability(
    observations: Sequence[NoiseObservation],
    *,
    fold_count: int = 5,
) -> dict[str, dict[str, Any]]:
    if fold_count != 5:
        raise ObservationNoiseError("R011 I-axis support requires five folds")
    values = {axis: [set() for _ in range(5)] for axis in I_AXES}
    group_folds: dict[tuple[str, str, tuple[Any, ...]], int] = {}
    for observation in observations:
        if not observation.full_observation or not observation.exact_observation:
            continue
        group = (observation.kind, observation.campaign_epoch, observation.gain_key)
        fold = group_folds.setdefault(group, int(digest(json_tree(group))[:8], 16) % 5)
        if len(observation.i_values) >= 1:
            values["force_i_gain"][fold].add(observation.i_values[0])
        if len(observation.i_values) >= 2:
            values["i_mode"][fold].add(observation.i_values[1])
    result: dict[str, dict[str, Any]] = {}
    for axis in I_AXES:
        fold_support = [len(support) for support in values[axis]]
        identifiable = all(count >= 2 for count in fold_support)
        result[axis] = {
            "status": "identifiable" if identifiable else "not_identifiable",
            "varying_support_count": len(set().union(*values[axis])),
            "fold_varying_support": fold_support,
            "required_five_fold_varying_support": True,
        }
    return result


def fit_hierarchical_noise(
    observations: Sequence[NoiseObservation],
    *,
    identifiable_lengthscales: Mapping[str, float] | None = None,
    refits: Sequence[NoiseRefit] = (),
    stars_drift: Mapping[str, Any] | None = None,
) -> ObservationNoiseAttestation:
    rows = tuple(observations)
    if any(not isinstance(row, NoiseObservation) for row in rows):
        raise ObservationNoiseError("noise fit requires typed observations")
    groups = _group(rows)
    repeat_groups = {key: values for key, values in groups.items() if len(values) >= 2}
    stratum_values: dict[tuple[str, str], list[float]] = {}
    kind_values: dict[str, list[float]] = {}
    global_values: list[float] = []
    for (kind, epoch, _gain), values in repeat_groups.items():
        variance = _variance(values)
        if variance is not None:
            stratum_values.setdefault((kind, epoch), []).append(variance)
            kind_values.setdefault(kind, []).append(variance)
            global_values.append(variance)
    global_parent = _log_mean(global_values) if global_values else NOISE_FLOOR_N2
    kind_parent = {kind: _log_mean(values) for kind, values in sorted(kind_values.items())}
    strata: dict[str, dict[str, Any]] = {}
    eligible_rows = tuple(row for row in rows if row.full_observation and row.exact_observation)
    for kind, epoch in sorted(set((row.kind, row.campaign_epoch) for row in eligible_rows)):
        local_values = stratum_values.get((kind, epoch), [])
        parent = kind_parent.get(kind, global_parent)
        local = _log_mean(local_values) if local_values else None
        count = sum(
            len(values) for (row_kind, row_epoch, _), values in groups.items()
            if row_kind == kind and row_epoch == epoch and len(values) >= 2
        )
        selected_raw = max(NOISE_FLOOR_N2, _shrink(local, parent, count))
        selected = min(CALIBRATION_GRID_N2, key=lambda value: (abs(value - selected_raw), value))
        strata[f"{kind}|{epoch}"] = {
            "kind": kind,
            "campaign_epoch": epoch,
            "repeat_group_count": len(local_values),
            "full_observation_count": sum(
                len(values) for (row_kind, row_epoch, _), values in groups.items()
                if row_kind == kind and row_epoch == epoch
            ),
            "local_log_variance_n2": local,
            "parent_log_variance_n2": parent,
            "selected_noise_n2": selected,
            "shrinkage": "deterministic_log_space_small_sample",
        }
    selected_noise = max(
        NOISE_FLOOR_N2,
        _log_mean([float(row["selected_noise_n2"]) for row in strata.values()]) if strata else global_parent,
    )
    axes = i_axis_identifiability(rows)
    lengths: dict[str, float] = {}
    for axis, value in dict(identifiable_lengthscales or {}).items():
        numeric = finite(value, f"lengthscale {axis}")
        if numeric <= 0.0:
            raise ObservationNoiseError("lengthscales must be positive")
        if axis not in I_AXES or axes.get(axis, {}).get("status") == "identifiable":
            lengths[axis] = numeric
    return ObservationNoiseAttestation(
        strata=strata,
        total_full_observations=sum(1 for row in rows if row.full_observation and row.exact_observation),
        repeat_group_count=len(repeat_groups),
        selected_noise_n2=min(CALIBRATION_GRID_N2, key=lambda value: (abs(value - selected_noise), value)),
        identifiable_lengthscales=lengths,
        i_axes=axes,
        refits=tuple(refits),
        stars_drift=stars_drift,
    )


def freeze_eligibility(
    *,
    total_full_observations: int,
    repeat_group_count: int,
    refits: Sequence[NoiseRefit],
    i_axes: Mapping[str, Mapping[str, Any]],
    retained_lengthscales: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    if total_full_observations < 30:
        enough_observations = False
    else:
        enough_observations = True
    enough_repeats = repeat_group_count >= 8
    consecutive = False
    lengthscale_change = None
    noise_change = None
    retained = set(retained_lengthscales or {})
    if len(refits) >= 2:
        left, right = refits[-2], refits[-1]
        same_sequence = right.fit_sequence == left.fit_sequence + 1
        same_axes = bool(retained) and set(left.identifiable_lengthscales) == set(right.identifiable_lengthscales) == retained
        common = sorted(retained if same_axes else set())
        changes = [
            abs(right.identifiable_lengthscales[axis] - left.identifiable_lengthscales[axis])
            / max(abs(left.identifiable_lengthscales[axis]), 1e-12)
            for axis in common
        ]
        lengthscale_change = max(changes, default=0.0)
        noise_change = abs(right.selected_noise_n2 - left.selected_noise_n2) / max(left.selected_noise_n2, 1e-12)
        consecutive = bool(same_sequence and common and lengthscale_change <= 0.20 and noise_change <= 0.25)
    i_support = all(i_axes.get(axis, {}).get("status") == "identifiable" for axis in I_AXES)
    return {
        "eligible": enough_observations and enough_repeats and consecutive,
        "full_observations_ge_30": enough_observations,
        "repeat_groups_ge_8": enough_repeats,
        "two_consecutive_refits": consecutive,
        "identifiable_lengthscale_max_relative_change": lengthscale_change,
        "noise_max_relative_change": noise_change,
        "i_axes_identifiable": i_support,
        "i_axes_remain_not_identifiable_until_five_fold_support": not i_support,
        "freeze_does_not_override_i_axis_status": True,
    }


def attest_stars_drift(drift: Mapping[str, Any]) -> dict[str, Any]:
    """Return diagnostic drift with an explicit no-model/no-fingerprint boundary."""

    if not isinstance(drift, Mapping):
        raise ObservationNoiseError("STARS drift must be an object")
    return {
        "diagnostic": json_tree(drift),
        "affects_variance": False,
        "affects_model": False,
        "affects_campaign_fingerprint": False,
        "affects_completion": False,
    }


__all__ = [
    "CALIBRATION_GRID_N2", "I_AXES", "NOISE_ATTESTATION_SCHEMA", "NOISE_FLOOR_N2",
    "NOISE_POLICY_VERSION", "NOISE_SCHEMA", "NoiseObservation", "NoiseRefit",
    "ObservationNoiseAttestation", "ObservationNoiseError", "VARIANCE_ESTIMATOR",
    "attest_stars_drift", "fit_hierarchical_noise", "freeze_eligibility",
    "i_axis_identifiability",
]
