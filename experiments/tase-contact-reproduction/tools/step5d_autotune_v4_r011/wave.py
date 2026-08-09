"""Pure canonical-force metrics and matched manual-Wave qualification."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .behavior import ManualWaveSchedule, default_manual_wave, validate_manual_wave
from .common import R011ValueError, digest, finite, freeze_tree, json_tree, require_digest


WAVE_METRICS_SCHEMA = "step5d.autotune-v4/r011-wave-normal-force-metrics-v1"
WAVE_RECEIPT_SCHEMA = "step5d.autotune-v4/r011-wave-qualification-receipt-v2"
SETTLING_WINDOW_S = 0.5
SETTLING_BAND_FRACTION = 0.10


class WaveQualificationError(R011ValueError):
    """A trace or qualification receipt is invalid."""


def _sample_time(sample: Mapping[str, Any]) -> float:
    return finite(sample.get("time_s"), "sample time_s")


def _normal_force(sample: Mapping[str, Any]) -> float:
    return finite(sample.get("normal_force_n"), "sample normal_force_n")


def _target(sample: Mapping[str, Any], target_force_n: float) -> float:
    return finite(sample.get("target_force_n", target_force_n), "sample target_force_n")


def _valid_samples(samples: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)) or len(samples) < 2:
        raise WaveQualificationError("normal-force trace requires at least two samples")
    result: list[dict[str, Any]] = []
    previous = None
    for sample in samples:
        if not isinstance(sample, Mapping):
            raise WaveQualificationError("normal-force trace sample must be an object")
        current = _sample_time(sample)
        if previous is not None and current <= previous:
            raise WaveQualificationError("normal-force trace time is not strictly increasing")
        _normal_force(sample)
        finite(sample.get("travel_m"), "sample travel_m")
        if "contact_confirmed" in sample and not isinstance(sample["contact_confirmed"], bool):
            raise WaveQualificationError("contact_confirmed must be boolean")
        result.append(json_tree(sample))
        previous = current
    return tuple(result)


def _contact_time(samples: Sequence[Mapping[str, Any]], explicit: float | None) -> float:
    if explicit is not None:
        value = finite(explicit, "contact_confirmation_time_s")
        if value < _sample_time(samples[0]) or value > _sample_time(samples[-1]):
            raise WaveQualificationError("contact confirmation time is outside trace")
        return value
    for sample in samples:
        if sample.get("contact_confirmed") is True:
            return _sample_time(sample)
    raise WaveQualificationError("contact confirmation is absent")


@dataclass(frozen=True)
class NormalForceMetrics:
    peak_overshoot_n: float
    positive_overshoot_impulse_n_s: float
    settling_after_contact_s: float
    search_time_s: float
    travel_m: float
    contact_confirmation_time_s: float
    settling_onset_time_s: float | None = None
    schema: str = WAVE_METRICS_SCHEMA

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "peak_overshoot_n": self.peak_overshoot_n,
            "positive_overshoot_impulse_n_s": self.positive_overshoot_impulse_n_s,
            "settling_after_contact_s": self.settling_after_contact_s if math.isfinite(self.settling_after_contact_s) else None,
            "search_time_s": self.search_time_s,
            "travel_m": self.travel_m,
            "contact_confirmation_time_s": self.contact_confirmation_time_s,
            "settling_onset_time_s": self.settling_onset_time_s,
        }


def canonical_normal_force_metrics(
    samples: Sequence[Mapping[str, Any]], *, target_force_n: float,
    contact_confirmation_time_s: float | None = None,
    settling_window_s: float = SETTLING_WINDOW_S,
    settling_band_fraction: float = SETTLING_BAND_FRACTION,
) -> NormalForceMetrics:
    """Compute canonical normal-force metrics; impulse ends at settling onset."""
    target = finite(target_force_n, "target_force_n")
    window = finite(settling_window_s, "settling_window_s")
    band = finite(settling_band_fraction, "settling_band_fraction")
    if target <= 0.0 or window <= 0.0 or not 0.0 < band < 1.0:
        raise WaveQualificationError("target or settling configuration is invalid")
    rows = _valid_samples(samples)
    contact = _contact_time(rows, contact_confirmation_time_s)
    after = [row for row in rows if _sample_time(row) >= contact]
    if not after:
        raise WaveQualificationError("trace has no post-contact samples")
    lower, upper = target * (1.0 - band), target * (1.0 + band)
    settle = math.inf
    onset: float | None = None
    for index, left in enumerate(after):
        if not lower <= _normal_force(left) <= upper:
            continue
        end = _sample_time(left)
        valid = True
        for right in after[index + 1:]:
            if not lower <= _normal_force(right) <= upper:
                valid = False
                break
            end = _sample_time(right)
            if end - _sample_time(left) >= window:
                settle = _sample_time(left) - contact
                onset = _sample_time(left)
                break
        if onset is None and valid and end - _sample_time(left) >= window:
            settle = _sample_time(left) - contact
            onset = _sample_time(left)
        if onset is not None:
            break
    if onset is None:
        impulse_rows = after
    else:
        impulse_rows = [row for row in after if _sample_time(row) <= onset]
    overshoots = [max(0.0, _normal_force(row) - _target(row, target)) for row in after]
    impulse = 0.0
    for left, right in zip(impulse_rows, impulse_rows[1:]):
        dt = _sample_time(right) - _sample_time(left)
        impulse += 0.5 * (max(0.0, _normal_force(left) - _target(left, target)) + max(0.0, _normal_force(right) - _target(right, target))) * dt
    travel_values = [finite(row["travel_m"], "sample travel_m") for row in rows]
    times = [_sample_time(row) for row in rows]
    return NormalForceMetrics(
        peak_overshoot_n=max(overshoots), positive_overshoot_impulse_n_s=impulse,
        settling_after_contact_s=settle, search_time_s=contact - times[0],
        travel_m=max(travel_values) - min(travel_values), contact_confirmation_time_s=contact,
        settling_onset_time_s=onset,
    )


@dataclass(frozen=True)
class WaveRunTrace:
    run_id: str
    matched_key: str
    repeat_index: int
    samples: tuple[Mapping[str, Any], ...]
    schedule_sha256: str
    release_identity_sha256: str
    execution_identity_sha256: str
    matched_condition_identity_sha256: str
    raw_trace_sha256: str
    contact_ok: bool = True
    identity_ok: bool = True
    timing_ok: bool = True
    safety_ok: bool = True
    contact_confirmation_time_s: float | None = None
    trainable: bool = False

    def __post_init__(self) -> None:
        if not self.run_id or not self.matched_key or isinstance(self.repeat_index, bool) or self.repeat_index not in (0, 1, 2):
            raise WaveQualificationError("Wave run identity/repeat index is invalid")
        for name, value in (("schedule_sha256", self.schedule_sha256), ("release_identity_sha256", self.release_identity_sha256), ("execution_identity_sha256", self.execution_identity_sha256), ("matched_condition_identity_sha256", self.matched_condition_identity_sha256), ("raw_trace_sha256", self.raw_trace_sha256)):
            require_digest(value, name)
        for name, value in (("contact_ok", self.contact_ok), ("identity_ok", self.identity_ok), ("timing_ok", self.timing_ok), ("safety_ok", self.safety_ok), ("trainable", self.trainable)):
            if not isinstance(value, bool):
                raise WaveQualificationError(f"{name} must be boolean")
        if self.trainable:
            raise WaveQualificationError("Wave qualification run cannot be trainable")
        object.__setattr__(self, "samples", tuple(freeze_tree(json_tree(sample)) for sample in self.samples))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WaveRunTrace":
        required = {"run_id", "matched_key", "repeat_index", "samples", "schedule_sha256", "release_identity_sha256", "execution_identity_sha256", "matched_condition_identity_sha256", "raw_trace_sha256"}
        optional = {"contact_ok", "identity_ok", "timing_ok", "safety_ok", "contact_confirmation_time_s", "trainable"}
        if not isinstance(value, Mapping) or not required.issubset(value) or set(value) - required - optional:
            raise WaveQualificationError("Wave run trace fields differ")
        values = dict(value)
        values.setdefault("contact_ok", True); values.setdefault("identity_ok", True); values.setdefault("timing_ok", True); values.setdefault("safety_ok", True); values.setdefault("contact_confirmation_time_s", None); values.setdefault("trainable", False)
        return cls(**values)

    def metrics(self, *, target_force_n: float) -> NormalForceMetrics:
        return canonical_normal_force_metrics(self.samples, target_force_n=target_force_n, contact_confirmation_time_s=self.contact_confirmation_time_s)


@dataclass(frozen=True)
class WaveQualificationReceipt:
    qualified: bool
    baseline_release_identity_sha256: str
    candidate_release_identity_sha256: str
    baseline_schedule_sha256: str
    candidate_schedule_sha256: str
    baseline_execution_identity_sha256: str
    candidate_execution_identity_sha256: str
    matched_condition_identity_sha256: str
    raw_trace_hashes: tuple[str, ...]
    baseline_run_ids: tuple[str, ...]
    candidate_run_ids: tuple[str, ...]
    baseline_metrics: tuple[NormalForceMetrics, ...]
    candidate_metrics: tuple[NormalForceMetrics, ...]
    gates: Mapping[str, bool]
    rejection_reasons: tuple[str, ...]
    wave_is_bo_variable: bool = False
    trainable: bool = False
    schema: str = WAVE_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != WAVE_RECEIPT_SCHEMA:
            raise WaveQualificationError("Wave receipt schema differs")
        for name, value in (("baseline_release_identity_sha256", self.baseline_release_identity_sha256), ("candidate_release_identity_sha256", self.candidate_release_identity_sha256), ("baseline_schedule_sha256", self.baseline_schedule_sha256), ("candidate_schedule_sha256", self.candidate_schedule_sha256), ("baseline_execution_identity_sha256", self.baseline_execution_identity_sha256), ("candidate_execution_identity_sha256", self.candidate_execution_identity_sha256), ("matched_condition_identity_sha256", self.matched_condition_identity_sha256)):
            require_digest(value, name)
        for value in self.raw_trace_hashes:
            require_digest(value, "raw_trace_hash")
        if self.wave_is_bo_variable or self.trainable:
            raise WaveQualificationError("Wave qualification cannot enter BO training")
        object.__setattr__(self, "gates", freeze_tree(json_tree(self.gates)))

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "qualified": self.qualified, "baseline_release_identity_sha256": self.baseline_release_identity_sha256, "candidate_release_identity_sha256": self.candidate_release_identity_sha256, "baseline_schedule_sha256": self.baseline_schedule_sha256, "candidate_schedule_sha256": self.candidate_schedule_sha256, "baseline_execution_identity_sha256": self.baseline_execution_identity_sha256, "candidate_execution_identity_sha256": self.candidate_execution_identity_sha256, "matched_condition_identity_sha256": self.matched_condition_identity_sha256, "raw_trace_hashes": list(self.raw_trace_hashes), "baseline_run_ids": list(self.baseline_run_ids), "candidate_run_ids": list(self.candidate_run_ids), "baseline_metrics": [metric.as_dict() for metric in self.baseline_metrics], "candidate_metrics": [metric.as_dict() for metric in self.candidate_metrics], "gates": json_tree(self.gates), "rejection_reasons": list(self.rejection_reasons), "wave_is_bo_variable": self.wave_is_bo_variable, "trainable": self.trainable}

    @property
    def receipt_sha256(self) -> str:
        return digest(self.as_dict())


def _median(metrics: Sequence[NormalForceMetrics], field: str) -> float:
    return float(statistics.median(float(getattr(metric, field)) for metric in metrics))


def qualify_manual_wave(
    baseline_runs: Sequence[WaveRunTrace], candidate_runs: Sequence[WaveRunTrace], *,
    baseline_release_identity_sha256: str, candidate_release_identity_sha256: str,
    baseline_schedule_sha256: str | None = None, candidate_schedule_sha256: str | None = None,
    baseline_execution_identity_sha256: str, candidate_execution_identity_sha256: str,
    matched_condition_identity_sha256: str, baseline_schedule: ManualWaveSchedule | Mapping[str, Any] | None = None,
    candidate_schedule: ManualWaveSchedule | Mapping[str, Any] | None = None,
    target_force_n: float,
) -> WaveQualificationReceipt:
    base_schedule = default_manual_wave() if baseline_schedule is None else (baseline_schedule if isinstance(baseline_schedule, ManualWaveSchedule) else validate_manual_wave(baseline_schedule))
    cand_schedule = default_manual_wave() if candidate_schedule is None else (candidate_schedule if isinstance(candidate_schedule, ManualWaveSchedule) else validate_manual_wave(candidate_schedule))
    base_sched = digest(base_schedule.as_dict()) if baseline_schedule_sha256 is None else require_digest(baseline_schedule_sha256, "baseline_schedule_sha256")
    cand_sched = digest(cand_schedule.as_dict()) if candidate_schedule_sha256 is None else require_digest(candidate_schedule_sha256, "candidate_schedule_sha256")
    for name, value in (("baseline_release_identity_sha256", baseline_release_identity_sha256), ("candidate_release_identity_sha256", candidate_release_identity_sha256), ("baseline_execution_identity_sha256", baseline_execution_identity_sha256), ("candidate_execution_identity_sha256", candidate_execution_identity_sha256), ("matched_condition_identity_sha256", matched_condition_identity_sha256)):
        require_digest(value, name)
    baseline, candidates = tuple(baseline_runs), tuple(candidate_runs)
    reasons: list[str] = []
    if len(baseline) != 3: reasons.append("baseline_run_count")
    if len(candidates) != 3: reasons.append("candidate_run_count")
    all_runs = baseline + candidates
    if len({run.run_id for run in all_runs}) != len(all_runs): reasons.append("duplicate_run_id")
    for side, rows, release, schedule_digest, execution in (("baseline", baseline, baseline_release_identity_sha256, base_sched, baseline_execution_identity_sha256), ("candidate", candidates, candidate_release_identity_sha256, cand_sched, candidate_execution_identity_sha256)):
        if {run.repeat_index for run in rows} != {0, 1, 2}: reasons.append(f"{side}_repeat_indices")
        keys = {run.matched_key for run in rows}
        if len(keys) != 3: reasons.append(f"{side}_matched_conditions")
        for run in rows:
            if run.release_identity_sha256 != release: reasons.append(f"release_identity:{run.run_id}")
            if run.schedule_sha256 != schedule_digest: reasons.append(f"schedule_identity:{run.run_id}")
            if run.execution_identity_sha256 != execution: reasons.append(f"execution_identity:{run.run_id}")
            if run.matched_condition_identity_sha256 != matched_condition_identity_sha256: reasons.append(f"condition_identity:{run.run_id}")
            for flag in ("contact_ok", "identity_ok", "timing_ok", "safety_ok"):
                if not getattr(run, flag): reasons.append(f"{flag}:{run.run_id}")
    if {run.matched_key for run in baseline} != {run.matched_key for run in candidates}: reasons.append("matched_identity")
    base_metrics: tuple[NormalForceMetrics, ...] = (); cand_metrics: tuple[NormalForceMetrics, ...] = ()
    try:
        base_metrics = tuple(run.metrics(target_force_n=target_force_n) for run in baseline)
        cand_metrics = tuple(run.metrics(target_force_n=target_force_n) for run in candidates)
    except WaveQualificationError as exc:
        reasons.append(f"trace_failure:{exc}")
    gates = {"peak_overshoot_le_baseline_median_1_05": False, "positive_impulse_le_baseline_median_1_05": False, "peak_or_impulse_le_baseline_median_0_90": False, "settling_le_baseline_median_1_10": False, "search_time_le_baseline_median_1_10": False, "travel_le_baseline_median_1_10": False}
    if len(base_metrics) == 3 and len(cand_metrics) == 3 and not reasons:
        bp, bi = _median(base_metrics, "peak_overshoot_n"), _median(base_metrics, "positive_overshoot_impulse_n_s")
        cp, ci = _median(cand_metrics, "peak_overshoot_n"), _median(cand_metrics, "positive_overshoot_impulse_n_s")
        gates["peak_overshoot_le_baseline_median_1_05"] = cp <= bp * 1.05
        gates["positive_impulse_le_baseline_median_1_05"] = ci <= bi * 1.05
        gates["peak_or_impulse_le_baseline_median_0_90"] = cp <= bp * 0.90 or ci <= bi * 0.90
        for field, gate_name in (("settling_after_contact_s", "settling_le_baseline_median_1_10"), ("search_time_s", "search_time_le_baseline_median_1_10"), ("travel_m", "travel_le_baseline_median_1_10")):
            base = _median(base_metrics, field); cand = _median(cand_metrics, field)
            gates[gate_name] = math.isfinite(base) and math.isfinite(cand) and cand <= base * 1.10
    for name, passed in gates.items():
        if not passed: reasons.append(name)
    return WaveQualificationReceipt(not reasons, baseline_release_identity_sha256, candidate_release_identity_sha256, base_sched, cand_sched, baseline_execution_identity_sha256, candidate_execution_identity_sha256, matched_condition_identity_sha256, tuple(run.raw_trace_sha256 for run in all_runs), tuple(run.run_id for run in baseline), tuple(run.run_id for run in candidates), base_metrics, cand_metrics, gates, tuple(dict.fromkeys(reasons)))


__all__ = ["NormalForceMetrics", "WAVE_METRICS_SCHEMA", "WAVE_RECEIPT_SCHEMA", "WaveQualificationError", "WaveQualificationReceipt", "WaveRunTrace", "canonical_normal_force_metrics", "qualify_manual_wave"]
