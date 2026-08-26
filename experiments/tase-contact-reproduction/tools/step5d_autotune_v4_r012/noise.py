"""R012 repeat-noise estimation with a fixed 5-D tau-active model boundary."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .common import R012ValueError, finite, freeze_tree, json_tree
from .offline_checksums import digest


NOISE_SCHEMA = "step5d.autotune-v4/r012-observation-noise-v1"
NOISE_ATTESTATION_SCHEMA = "step5d.autotune-v4/r012-observation-noise-attestation-v1"
NOISE_POLICY_VERSION = "r012-repeat-gain-key-log-shrink-v1"
VARIANCE_ESTIMATOR = "statistics.variance"
NOISE_FLOOR_N2 = 0.01
CALIBRATION_GRID_N2 = (2.5e-5, 1.0e-4, 2.5e-4, 5.0e-4, 1.0e-3, 2.5e-3, 5.0e-3, 1.0e-2, 2.0e-2)
SHRINK_PRIOR_GROUPS = 4.0
MODEL_AXES = (
    "log2_force_p_over_d",
    "log2_force_damping",
    "log2_normal_filter_tau_s",
    "log2_orientation_ko",
    "log2_motion_kp",
)


class ObservationNoiseError(R012ValueError):
    """Noise input is not a full exact R012 observation."""


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
        if not all(isinstance(value, str) and value for value in (self.observation_id, self.kind, self.campaign_epoch)):
            raise ObservationNoiseError("noise observation identity fields are invalid")
        if not self.gain_key:
            raise ObservationNoiseError("gain_key must not be empty")
        value = finite(self.objective_n, "objective_n")
        if value < 0.0 or not isinstance(self.full_observation, bool) or not isinstance(self.exact_observation, bool):
            raise ObservationNoiseError("noise observation is invalid")
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
        selected = finite(self.selected_noise_n2, "selected_noise_n2")
        if selected <= 0.0 or selected not in CALIBRATION_GRID_N2:
            raise ObservationNoiseError("selected noise is outside the calibration grid")
        for axis, value in self.identifiable_lengthscales.items():
            if not isinstance(axis, str) or finite(value, f"lengthscale {axis}") <= 0.0:
                raise ObservationNoiseError("lengthscales must be positive")
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
        if self.total_full_observations < 0 or self.repeat_group_count < 0:
            raise ObservationNoiseError("noise counts are invalid")
        selected = finite(self.selected_noise_n2, "selected_noise_n2")
        if selected <= 0.0 or selected not in CALIBRATION_GRID_N2:
            raise ObservationNoiseError("selected noise is outside the calibration grid")
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
            "grouping": "physical_gain_key_across_campaign_epoch_and_kind",
            "strata": json_tree(self.strata),
            "total_full_observations": self.total_full_observations,
            "repeat_group_count": self.repeat_group_count,
            "selected_noise_n2": self.selected_noise_n2,
            "identifiable_lengthscales": dict(self.identifiable_lengthscales),
            "model_axes": list(MODEL_AXES),
            "i_axes": json_tree(self.i_axes),
            "refits": [
                {
                    "identifiable_lengthscales": dict(item.identifiable_lengthscales),
                    "selected_noise_n2": item.selected_noise_n2,
                    "fit_sequence": item.fit_sequence,
                }
                for item in self.refits
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
    def attestation_id(self) -> str:
        payload = self.as_dict()
        payload["stars_drift"] = None
        return digest(payload)


def _groups(observations: Sequence[NoiseObservation]) -> dict[tuple[Any, ...], list[float]]:
    groups: dict[tuple[Any, ...], list[float]] = {}
    for row in observations:
        if not row.full_observation or not row.exact_observation:
            continue
        groups.setdefault(row.gain_key, []).append(float(row.objective_n))
    return {key: values for key, values in sorted(groups.items(), key=lambda item: repr(item[0]))}


def _variance(values: Sequence[float]) -> float | None:
    return None if len(values) < 2 else max(NOISE_FLOOR_N2, float(statistics.variance(values)))


def _log_mean(values: Sequence[float]) -> float:
    return math.exp(statistics.fmean(math.log(max(1e-12, value)) for value in values)) if values else NOISE_FLOOR_N2


def _shrink(local: float | None, parent: float, count: int) -> float:
    if local is None:
        return parent
    weight = count / (count + SHRINK_PRIOR_GROUPS)
    return math.exp(weight * math.log(local) + (1.0 - weight) * math.log(parent))


def _nearest_grid(value: float) -> float:
    return min(CALIBRATION_GRID_N2, key=lambda item: (abs(item - max(NOISE_FLOOR_N2, value)), item))


def observation_noise_by_gain_key(observations: Sequence[NoiseObservation]) -> dict[tuple[Any, ...], float]:
    groups = _groups(tuple(observations))
    local = {key: _variance(values) for key, values in groups.items()}
    parent = _log_mean([value for value in local.values() if value is not None]) if any(value is not None for value in local.values()) else NOISE_FLOOR_N2
    return {
        key: _nearest_grid(_shrink(local[key], parent, len(groups[key]))) if local[key] is not None else NOISE_FLOOR_N2
        for key in groups
    }


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
    groups = _groups(rows)
    eligible_rows = tuple(row for row in rows if row.full_observation and row.exact_observation)
    repeat_groups = {key: values for key, values in groups.items() if len(values) >= 2}
    parent = _log_mean([_variance(values) for values in repeat_groups.values() if _variance(values) is not None]) if repeat_groups else NOISE_FLOOR_N2
    strata: dict[str, dict[str, Any]] = {}
    for kind, epoch in sorted({row.stratum for row in eligible_rows}):
        matching = [row for row in eligible_rows if row.stratum == (kind, epoch)]
        local_values = [_variance(groups[row.gain_key]) for row in matching if row.gain_key in repeat_groups]
        local_values = [value for value in local_values if value is not None]
        selected = _nearest_grid(
            _shrink(
                _log_mean(local_values) if local_values else None,
                parent,
                sum(len(groups[row.gain_key]) for row in matching if row.gain_key in repeat_groups),
            )
        )
        strata[f"{kind}|{epoch}"] = {
            "kind": kind,
            "campaign_epoch": epoch,
            "full_observation_count": len(matching),
            "repeat_group_count": len({row.gain_key for row in matching if row.gain_key in repeat_groups}),
            "selected_noise_n2": selected,
            "grouping": "gain_key_across_epochs_and_kinds",
        }
    lengths: dict[str, float] = {}
    for axis, value in dict(identifiable_lengthscales or {}).items():
        numeric = finite(value, f"lengthscale {axis}")
        if numeric <= 0.0:
            raise ObservationNoiseError("noise lengthscale is outside the five active axes")
        if axis not in {"force_i_gain", "i_mode"}:
            lengths[axis] = numeric
    fixed_axes = {"force_i_gain": {"status": "fixed_not_a_model_axis"}}
    selected = _nearest_grid(_log_mean([float(item["selected_noise_n2"]) for item in strata.values()]) if strata else parent)
    return ObservationNoiseAttestation(
        strata,
        len(eligible_rows),
        len(repeat_groups),
        selected,
        lengths,
        fixed_axes,
        tuple(refits),
        stars_drift,
    )


def freeze_eligibility(
    *,
    total_full_observations: int,
    repeat_group_count: int,
    refits: Sequence[NoiseRefit],
    i_axes: Mapping[str, Mapping[str, Any]],
    retained_lengthscales: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    del i_axes
    enough_observations = total_full_observations >= 30
    enough_repeats = repeat_group_count >= 8
    consecutive = False
    lengthscale_change = None
    noise_change = None
    retained = set(retained_lengthscales or {})
    if len(refits) >= 2:
        left, right = refits[-2], refits[-1]
        common = sorted(retained & set(left.identifiable_lengthscales) & set(right.identifiable_lengthscales))
        lengthscale_change = max(
            (abs(right.identifiable_lengthscales[a] - left.identifiable_lengthscales[a]) / max(abs(left.identifiable_lengthscales[a]), 1e-12) for a in common),
            default=0.0,
        )
        noise_change = abs(right.selected_noise_n2 - left.selected_noise_n2) / max(left.selected_noise_n2, 1e-12)
        consecutive = right.fit_sequence == left.fit_sequence + 1 and bool(common) and lengthscale_change <= 0.20 and noise_change <= 0.25
    return {
        "eligible": enough_observations and enough_repeats and consecutive,
        "full_observations_ge_30": enough_observations,
        "repeat_groups_ge_8": enough_repeats,
        "two_consecutive_refits": consecutive,
        "identifiable_lengthscale_max_relative_change": lengthscale_change,
        "noise_max_relative_change": noise_change,
        "i_axis_identifiability_removed": True,
    }


def attest_stars_drift(drift: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(drift, Mapping):
        raise ObservationNoiseError("STARS drift must be an object")
    return {
        "diagnostic": json_tree(drift),
        "affects_variance": False,
        "affects_model": False,
        "affects_campaign_identity": False,
        "affects_completion": False,
    }


__all__ = [
    "CALIBRATION_GRID_N2",
    "MODEL_AXES",
    "NOISE_ATTESTATION_SCHEMA",
    "NOISE_FLOOR_N2",
    "NOISE_POLICY_VERSION",
    "NOISE_SCHEMA",
    "NoiseObservation",
    "NoiseRefit",
    "ObservationNoiseAttestation",
    "ObservationNoiseError",
    "VARIANCE_ESTIMATOR",
    "attest_stars_drift",
    "fit_hierarchical_noise",
    "freeze_eligibility",
    "observation_noise_by_gain_key",
]
