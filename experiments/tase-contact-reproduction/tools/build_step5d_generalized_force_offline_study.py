#!/usr/bin/env python3
"""Build a leakage-aware offline study for the generalized force loop.

This module has two deliberately separate evidence lanes:

* ``audit_trace`` reads raw R013 path-trace JSONL and computes descriptive
  identification features without fitting a controller.  An entire attempt is
  excluded when any row carries runtime phase strategy or a non-zero phase
  target correction.  No interpolation is used for the 550-bin metric.
* ``run_synthetic_study`` exercises the proposed loop against a small bounded
  Kelvin--Voigt contact family.  This is a nominal/robust simulation stress
  test, never physical acceptance and never a CAD/runtime prior.

The CLI writes one explicit JSON artifact and never modifies the source run.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any, Iterable, Mapping, Sequence

from step5d_generalized_force_outer_loop import (
    GENERALIZED_FORCE_SCHEMA,
    GeneralizedForceConfigV1,
    GeneralizedForceInputsV1,
    GeneralizedForceStateV1,
    step_generalized_force_outer_loop,
)
from step5d_generalized_force_stability import (
    interval_vertices,
    search_common_lyapunov,
)


STUDY_SCHEMA = "step5d.offline/generalized-force-study-v1"
TRACE_SCHEMA = "step5d.autotune-v4/r008-state25-path-trace-v1"
BIN_WIDTH_S = 0.1
FORMAL_START_S = 5.0
FORMAL_END_S = 60.0
REQUIRED_BINS = 550


class StudyError(ValueError):
    """The offline study input violates its explicit evidence contract."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise StudyError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StudyError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise StudyError(f"{name} must be finite")
    return result


def _optional_finite(value: Any, name: str) -> float | None:
    if value is None:
        return None
    try:
        return _finite(value, name)
    except StudyError:
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _quantiles(values: Iterable[float]) -> dict[str, float | None]:
    data = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not data:
        return {"p01": None, "p05": None, "p50": None, "p95": None, "p99": None, "max": None}

    def percentile(fraction: float) -> float:
        if len(data) == 1:
            return data[0]
        index = fraction * (len(data) - 1)
        lower = int(math.floor(index))
        upper = min(len(data) - 1, lower + 1)
        weight = index - lower
        return data[lower] * (1.0 - weight) + data[upper] * weight

    return {
        "p01": percentile(0.01),
        "p05": percentile(0.05),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": data[-1],
    }


def _boolish(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "none", "null"}
    # Unknown encodings are not silently treated as a clean row.
    return True


def phase_leakage_reasons(row: Mapping[str, Any]) -> tuple[str, ...]:
    reasons: list[str] = []
    if _boolish(row.get("runtime_strategy_enabled")):
        reasons.append("runtime_strategy_enabled")
    if "phase_target_correction_n" in row and row.get("phase_target_correction_n") is not None:
        try:
            correction = _finite(row.get("phase_target_correction_n"), "phase_target_correction_n")
        except StudyError:
            reasons.append("invalid_phase_target_correction_n")
        else:
            if abs(correction) > 1e-12:
                reasons.append("nonzero_phase_target_correction_n")
    return tuple(reasons)


def _required_row_value(row: Mapping[str, Any], key: str) -> Any:
    if key not in row:
        raise StudyError(f"trace row is missing required field {key!r}")
    return row[key]


def load_trace(path: Path, *, max_rows: int | None = None) -> tuple[dict[int, list[dict[str, Any]]], dict[str, Any]]:
    """Load one JSONL trace while retaining row-level leakage provenance."""

    if not path.is_file():
        raise StudyError(f"trace does not exist: {path}")
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    row_count = 0
    malformed_count = 0
    schema_counts: Counter[str] = Counter()
    leakage_rows = 0
    leakage_reasons: Counter[str] = Counter()
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if max_rows is not None and row_count >= max_rows:
                break
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                malformed_count += 1
                raise StudyError(f"invalid JSON at line {line_number}: {exc}") from exc
            if not isinstance(row, Mapping):
                raise StudyError(f"trace row {line_number} is not an object")
            attempt_value = _required_row_value(row, "attempt_ordinal")
            try:
                attempt = int(attempt_value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise StudyError(f"attempt_ordinal at line {line_number} is invalid") from exc
            if attempt <= 0:
                raise StudyError(f"attempt_ordinal at line {line_number} must be positive")
            _finite(_required_row_value(row, "monotonic_s"), "monotonic_s")
            _finite(_required_row_value(row, "filtered_normal_n"), "filtered_normal_n")
            pose = _required_row_value(row, "tcp_pose_m_rad")
            if not isinstance(pose, Sequence) or len(pose) < 3:
                raise StudyError(f"tcp_pose_m_rad at line {line_number} is not a 3+ vector")
            for item in pose[:3]:
                _finite(item, "tcp_pose_m_rad")
            reasons = phase_leakage_reasons(row)
            if reasons:
                leakage_rows += 1
                leakage_reasons.update(reasons)
            schema_counts[str(row.get("schema", "<missing>"))] += 1
            groups[attempt].append(dict(row))
            row_count += 1
    if not groups:
        raise StudyError("trace has no rows")
    return groups, {
        "row_count": row_count,
        "malformed_row_count": malformed_count,
        "attempt_count": len(groups),
        "schema_counts": dict(sorted(schema_counts.items())),
        "leakage_row_count": leakage_rows,
        "leakage_reason_row_counts": dict(sorted(leakage_reasons.items())),
        "truncated": max_rows is not None and row_count >= max_rows,
    }


def _time_vector(rows: Sequence[Mapping[str, Any]]) -> tuple[list[float], str]:
    path_times = [_optional_finite(row.get("runtime_strategy_path_time_s"), "runtime_strategy_path_time_s") for row in rows]
    if all(value is not None for value in path_times) and path_times:
        values = [float(value) for value in path_times if value is not None]
        return values, "runtime_strategy_path_time_s"
    monotonic = [_finite(row["monotonic_s"], "monotonic_s") for row in rows]
    if not monotonic:
        return [], "unavailable"
    origin = monotonic[0]
    return [value - origin for value in monotonic], "attempt_relative_monotonic_s"


def sealed_style_metric(
    rows: Sequence[Mapping[str, Any]],
    *,
    target_force_n: float = 5.0,
) -> dict[str, Any]:
    """Recompute the 550-bin statistic without interpolation or gap filling."""

    if not rows:
        return {"status": "no_rows", "authoritative": False}
    ordered = sorted(rows, key=lambda row: _finite(row["monotonic_s"], "monotonic_s"))
    times, time_basis = _time_vector(ordered)
    if not times or any(b < a for a, b in zip(times, times[1:])):
        return {"status": "non_monotonic_time", "authoritative": False, "time_basis": time_basis}
    bins: dict[int, list[float]] = defaultdict(list)
    for time_s, row in zip(times, ordered, strict=True):
        if time_s < 0.0 or time_s >= FORMAL_END_S:
            continue
        index = int(math.floor((time_s + 1e-12) / BIN_WIDTH_S))
        bins[index].append(_finite(row["filtered_normal_n"], "filtered_normal_n"))
    formal_indices = tuple(range(int(FORMAL_START_S / BIN_WIDTH_S), int(FORMAL_END_S / BIN_WIDTH_S)))
    missing = [index for index in formal_indices if not bins.get(index)]
    bin_means = {
        str(index): math.fsum(bins[index]) / len(bins[index])
        for index in formal_indices
        if bins.get(index)
    }
    result: dict[str, Any] = {
        "status": "sealed" if not missing else "gap",
        "authoritative": time_basis == "runtime_strategy_path_time_s",
        "time_basis": time_basis,
        "formal_window_s": [FORMAL_START_S, FORMAL_END_S],
        "bin_width_s": BIN_WIDTH_S,
        "required_bin_count": REQUIRED_BINS,
        "complete_bin_count": REQUIRED_BINS - len(missing),
        "missing_bin_ids": missing,
        "sample_count": len(rows),
        "bin_mean_n": bin_means,
    }
    if not missing:
        errors = [abs(bin_means[str(index)] - target_force_n) for index in formal_indices]
        result["formal_mae_n"] = math.fsum(errors) / REQUIRED_BINS
    else:
        result["formal_mae_n"] = None
    return result


def _saturation(row: Mapping[str, Any]) -> bool:
    names = (
        "integral_authority_clamped",
        "integral_conditional_frozen",
        "integral_saturated",
        "integral_state_clamped",
        "integral_velocity_saturated",
    )
    return any(_boolish(row.get(name)) for name in names)


def _attempt_identification(rows: Sequence[Mapping[str, Any]], *, target_force_n: float) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: _finite(row["monotonic_s"], "monotonic_s"))
    times = [_finite(row["monotonic_s"], "monotonic_s") for row in ordered]
    filtered = [_finite(row["filtered_normal_n"], "filtered_normal_n") for row in ordered]
    raw = [_optional_finite(row.get("normal_load_n"), "normal_load_n") for row in ordered]
    rates: list[float] = []
    speeds: list[float] = []
    force_filter_residual: list[float] = []
    for index in range(1, len(ordered)):
        dt = times[index] - times[index - 1]
        if dt <= 0.0:
            continue
        rates.append((filtered[index] - filtered[index - 1]) / dt)
        pose_a = ordered[index - 1]["tcp_pose_m_rad"]
        pose_b = ordered[index]["tcp_pose_m_rad"]
        distance = math.sqrt(sum((float(pose_b[k]) - float(pose_a[k])) ** 2 for k in range(3)))
        speeds.append(distance / dt)
    for raw_value, filtered_value in zip(raw, filtered, strict=True):
        if raw_value is not None:
            force_filter_residual.append(raw_value - filtered_value)
    duration_s = max(0.0, times[-1] - times[0]) if times else 0.0
    metric = sealed_style_metric(ordered, target_force_n=target_force_n)
    gains = {}
    for name in ("force_p_gain", "force_i_gain", "force_damping", "force_integral_limit_n_s"):
        values = [_optional_finite(row.get(name), name) for row in ordered]
        finite_values = [value for value in values if value is not None]
        gains[name] = finite_values[0] if finite_values and all(value == finite_values[0] for value in finite_values) else _quantiles(finite_values)
    return {
        "sample_count": len(rows),
        "duration_s": duration_s,
        "force_n_quantiles": _quantiles(filtered),
        "force_rate_n_per_s_quantiles": _quantiles(rates),
        "force_rate_abs_n_per_s_quantiles": _quantiles(abs(value) for value in rates),
        "tcp_linear_speed_m_per_s_quantiles": _quantiles(speeds),
        "raw_minus_filtered_force_n_quantiles": _quantiles(force_filter_residual),
        "saturation_fraction": sum(_saturation(row) for row in ordered) / max(1, len(ordered)),
        "target_force_n": target_force_n,
        "reported_force_error_consistency_max_n": max(
            (
                abs(
                    (
                        _optional_finite(row.get("force_error_n"), "force_error_n")
                        if _optional_finite(row.get("force_error_n"), "force_error_n") is not None
                        else target_force_n
                    )
                    - (target_force_n - _finite(row["filtered_normal_n"], "filtered_normal_n"))
                )
                for row in ordered
                if row.get("force_error_n") is not None
            ),
            default=None,
        ),
        "controller_gains": gains,
        "metric": metric,
    }


def _split_attempts(attempts: Sequence[int], *, modulus: int = 5, remainder: int = 0) -> tuple[list[int], list[int]]:
    if modulus <= 1 or remainder < 0 or remainder >= modulus:
        raise StudyError("holdout split requires modulus > 1 and a valid remainder")
    holdout = sorted(attempt for attempt in attempts if attempt % modulus == remainder)
    train = sorted(attempt for attempt in attempts if attempt not in set(holdout))
    return train, holdout


@dataclass(frozen=True)
class SyntheticScenario:
    scenario_id: str
    split: str
    stiffness_n_per_m: float
    disturbance_amplitude_n: float
    initial_penetration_fraction: float = 0.8
    environment_damping_n_s_per_m: float = 0.0


def _synthetic_metric(samples: Sequence[float], *, dt_s: float, target_force_n: float = 5.0) -> dict[str, Any]:
    bins: list[float] = []
    for index in range(50, 600):
        start = int(round(index * BIN_WIDTH_S / dt_s))
        end = int(round((index + 1) * BIN_WIDTH_S / dt_s))
        values = samples[start:end]
        if not values:
            return {"status": "gap", "formal_mae_n": None, "complete_bin_count": len(bins)}
        bins.append(abs(math.fsum(values) / len(values) - target_force_n))
    return {
        "status": "sealed",
        "formal_mae_n": math.fsum(bins) / len(bins),
        "complete_bin_count": len(bins),
        "required_bin_count": REQUIRED_BINS,
    }


def _simulate_scenario(config: GeneralizedForceConfigV1, scenario: SyntheticScenario, *, dt_s: float = 0.01, duration_s: float = 60.0) -> dict[str, Any]:
    if dt_s <= 0.0 or duration_s < FORMAL_END_S:
        raise StudyError("synthetic simulation requires positive dt and a horizon of at least 60 s")
    normal = (0.0, 0.0, -1.0)
    position = scenario.initial_penetration_fraction * config.force_target_n / scenario.stiffness_n_per_m
    velocity = 0.0
    state = GeneralizedForceStateV1()
    samples: list[float] = []
    for step in range(int(round(duration_s / dt_s))):
        time_s = step * dt_s
        disturbance = scenario.disturbance_amplitude_n * (
            math.sin(2.0 * math.pi * time_s / 7.0)
            + 3.0 * math.sin(2.0 * math.pi * time_s / 0.7)
        )
        load = max(
            0.0,
            scenario.stiffness_n_per_m * position
            + scenario.environment_damping_n_s_per_m * velocity
            + disturbance,
        )
        output = step_generalized_force_outer_loop(
            config,
            state,
            GeneralizedForceInputsV1(
                tcp_position_base=(0.0, 0.0, 0.0),
                force_base_n=tuple(normal[index] * load for index in range(3)),
                desired_position_base=(0.0, 0.0, 0.0),
                desired_velocity_base=(0.0, 0.0, 0.0),
                dt_s=dt_s,
                reaction_normal_base=normal,
            ),
        )
        state = output.next_state
        velocity = output.normal_velocity_m_s
        position = max(0.0, position + velocity * dt_s)
        samples.append(load)
    metric = _synthetic_metric(samples, dt_s=dt_s, target_force_n=config.force_target_n)
    return {
        "scenario_id": scenario.scenario_id,
        "split": scenario.split,
        "stiffness_n_per_m": scenario.stiffness_n_per_m,
        "disturbance_amplitude_n": scenario.disturbance_amplitude_n,
        "formal_metric": metric,
        "force_min_n": min(samples),
        "force_max_n": max(samples),
        "final_force_n": samples[-1],
        "final_penetration_m": position,
        "final_velocity_m_s": velocity,
        "bounded": all(math.isfinite(value) and abs(value) < 100.0 for value in samples),
    }


def _candidate_configs() -> dict[str, GeneralizedForceConfigV1]:
    common = dict(normal_velocity_limit_m_s=0.01)
    return {
        "baseline_zero_extension": GeneralizedForceConfigV1(**common),
        "nonlinear_i_ratio_025": GeneralizedForceConfigV1(nonlinear_i_ratio=0.25, **common),
        "gated_force_rate": GeneralizedForceConfigV1(
            force_rate_gain_m_s2=0.5,
            force_rate_scale_n_per_s=2.0,
            force_rate_deadband_n_per_s=2.0,
            **common,
        ),
        "combined_i_and_rate": GeneralizedForceConfigV1(
            nonlinear_i_ratio=0.25,
            force_rate_gain_m_s2=0.5,
            force_rate_scale_n_per_s=2.0,
            force_rate_deadband_n_per_s=2.0,
            **common,
        ),
    }


def run_synthetic_study() -> dict[str, Any]:
    scenarios = (
        SyntheticScenario("nominal_holdout", "holdout_nominal", 100.0, 0.0),
        SyntheticScenario("soft_contact_train", "train", 60.0, 0.2),
        SyntheticScenario("nominal_disturbed_train", "train", 100.0, 0.2),
        SyntheticScenario("hard_contact_holdout", "holdout_robust", 160.0, 0.3),
        SyntheticScenario("soft_contact_holdout", "holdout_robust", 60.0, 0.2),
    )
    candidates: dict[str, Any] = {}
    for name, config in _candidate_configs().items():
        scenario_results = [_simulate_scenario(config, scenario) for scenario in scenarios]
        train_mae = [
            result["formal_metric"]["formal_mae_n"]
            for result in scenario_results
            if result["split"] == "train"
        ]
        robust_holdout_mae = [
            result["formal_metric"]["formal_mae_n"]
            for result in scenario_results
            if result["split"] == "holdout_robust"
        ]
        nominal = next(result for result in scenario_results if result["split"] == "holdout_nominal")
        candidates[name] = {
            "controller_schema": GENERALIZED_FORCE_SCHEMA,
            "config": {
                "nonlinear_i_ratio": config.nonlinear_i_ratio,
                "force_rate_gain_m_s2": config.force_rate_gain_m_s2,
                "force_rate_scale_n_per_s": config.force_rate_scale_n_per_s,
                "force_rate_deadband_n_per_s": config.force_rate_deadband_n_per_s,
            },
            "train_mean_mae_n": statistics.fmean(train_mae),
            "robust_holdout_mean_mae_n": statistics.fmean(robust_holdout_mae),
            "nominal_holdout_mae_n": nominal["formal_metric"]["formal_mae_n"],
            "nominal_holdout_target_0p1_pass": nominal["formal_metric"]["formal_mae_n"] <= 0.1,
            "scenario_results": scenario_results,
        }
    selected = min(
        candidates,
        key=lambda name: (
            candidates[name]["robust_holdout_mean_mae_n"],
            candidates[name]["train_mean_mae_n"],
        ),
    )
    # This certificate is conditional on a declared nominal stiffness range;
    # it is not inferred from the unknown-surface trace.
    certificate_vertices = interval_vertices(
        stiffness_bounds_n_per_m=(90.0, 110.0),
        integral_slope_bounds_m_s2_per_n_s=(1.0 / 12.0, 1.0 / 12.0 * 1.25),
        force_p_gain_m_s2_per_n=1.0 / 12.0,
        force_damping_s_inv=550.0 / 12.0,
    )
    certificate = search_common_lyapunov(certificate_vertices, seed=17, maxiter=80, population_size=8)
    return {
        "simulator": "synthetic_kelvin_voigt_contact_v1",
        "model_role": "offline_stress_test_only_no_runtime_CAD_or_STL_prior",
        "dt_s": 0.01,
        "duration_s": 60.0,
        "target_force_n": 5.0,
        "nominal_target_interpretation": "held_out_nominal_simulation_target_not_physical_acceptance",
        "candidates": candidates,
        "selected_by": "minimum_robust_holdout_mean_mae_then_train_mean_mae",
        "selected_candidate": selected,
        "conditional_stability_certificate": certificate.as_dict(),
        "certificate_assumptions": {
            "effective_stiffness_bounds_n_per_m": [90.0, 110.0],
            "integral_slope_bounds_m_s2_per_n_s": [1.0 / 12.0, 1.0 / 12.0 * 1.25],
            "disturbance_treated_as_bounded_input": True,
            "inner_robot_or_RNN_residual": "not included in outer-loop certificate; use ISS cascade corollary",
        },
    }


def audit_trace(
    path: Path,
    *,
    target_force_n: float = 5.0,
    holdout_modulus: int = 5,
    holdout_remainder: int = 0,
    max_rows: int | None = None,
) -> dict[str, Any]:
    groups, load_meta = load_trace(path, max_rows=max_rows)
    attempt_ids = sorted(groups)
    train_ids, holdout_ids = _split_attempts(
        attempt_ids,
        modulus=holdout_modulus,
        remainder=holdout_remainder,
    )
    summaries: dict[str, Any] = {}
    excluded: dict[int, list[str]] = {}
    eligible: list[int] = []
    for attempt in attempt_ids:
        rows = groups[attempt]
        reasons = sorted({reason for row in rows for reason in phase_leakage_reasons(row)})
        if reasons:
            excluded[attempt] = reasons
            continue
        eligible.append(attempt)
        summaries[str(attempt)] = _attempt_identification(rows, target_force_n=target_force_n)
    eligible_train = [attempt for attempt in train_ids if attempt in eligible]
    eligible_holdout = [attempt for attempt in holdout_ids if attempt in eligible]

    def aggregate(attempts: Sequence[int]) -> dict[str, Any]:
        force_values: list[float] = []
        rate_values: list[float] = []
        abs_rate_values: list[float] = []
        raw_force_values: list[float] = []
        raw_rate_values: list[float] = []
        raw_abs_rate_values: list[float] = []
        speeds: list[float] = []
        residuals: list[float] = []
        saturation = []
        raw_saturation_count = 0
        raw_saturation_total = 0
        sealed_mae: list[float] = []
        for attempt in attempts:
            summary = summaries[str(attempt)]
            ordered_rows = sorted(
                groups[attempt],
                key=lambda row: _finite(row["monotonic_s"], "monotonic_s"),
            )
            raw_force_values.extend(
                _finite(row["filtered_normal_n"], "filtered_normal_n")
                for row in ordered_rows
            )
            raw_saturation_count += sum(_saturation(row) for row in ordered_rows)
            raw_saturation_total += len(ordered_rows)
            for previous, current in zip(ordered_rows, ordered_rows[1:]):
                dt = _finite(current["monotonic_s"], "monotonic_s") - _finite(previous["monotonic_s"], "monotonic_s")
                if dt > 0.0:
                    rate = (
                        _finite(current["filtered_normal_n"], "filtered_normal_n")
                        - _finite(previous["filtered_normal_n"], "filtered_normal_n")
                    ) / dt
                    raw_rate_values.append(rate)
                    raw_abs_rate_values.append(abs(rate))
            force_values.extend(
                value for value in [summary["force_n_quantiles"]["p50"]] if value is not None
            )
            for target, sink in (
                (summary["force_rate_n_per_s_quantiles"], rate_values),
                (summary["force_rate_abs_n_per_s_quantiles"], abs_rate_values),
                (summary["tcp_linear_speed_m_per_s_quantiles"], speeds),
                (summary["raw_minus_filtered_force_n_quantiles"], residuals),
            ):
                if target["p50"] is not None:
                    sink.append(float(target["p50"]))
            saturation.append(summary["saturation_fraction"])
            metric = summary["metric"]
            if metric.get("formal_mae_n") is not None:
                sealed_mae.append(float(metric["formal_mae_n"]))
        return {
            "attempt_count": len(attempts),
            "raw_force_n_quantiles": _quantiles(raw_force_values),
            "raw_force_rate_n_per_s_quantiles": _quantiles(raw_rate_values),
            "raw_force_rate_abs_n_per_s_quantiles": _quantiles(raw_abs_rate_values),
            "raw_saturation_fraction": raw_saturation_count / max(1, raw_saturation_total),
            "median_attempt_force_medians_n": _quantiles(force_values),
            "median_attempt_force_rate_medians_n_per_s": _quantiles(rate_values),
            "median_attempt_abs_force_rate_medians_n_per_s": _quantiles(abs_rate_values),
            "median_attempt_tcp_speed_medians_m_per_s": _quantiles(speeds),
            "median_attempt_raw_minus_filtered_medians_n": _quantiles(residuals),
            "saturation_fraction_quantiles": _quantiles(saturation),
            "attempt_formal_mae_n_quantiles": _quantiles(sealed_mae),
        }

    identification_status = (
        "partial_stiffness_unidentified_from_trace"
        if eligible
        else "no_leakage_free_attempts"
    )
    return {
        "schema": STUDY_SCHEMA,
        "version": 1,
        "source": {
            "path": str(path),
            "sha256": _sha256(path),
            "trace_schema_expected": TRACE_SCHEMA,
            **load_meta,
        },
        "phase_leakage_audit": {
            "excluded_attempt_count": len(excluded),
            "excluded_row_count": load_meta["leakage_row_count"],
            "excluded_attempts": {str(key): value for key, value in sorted(excluded.items())},
            "policy": "exclude_entire_attempt_if_any_row_has_runtime_strategy_or_phase_correction",
            "used_for_identification_or_tuning": False,
        },
        "split": {
            "rule": f"attempt_ordinal % {holdout_modulus} == {holdout_remainder} is holdout",
            "train_attempts": train_ids,
            "holdout_attempts": holdout_ids,
            "eligible_train_attempts": eligible_train,
            "eligible_holdout_attempts": eligible_holdout,
        },
        "identification": {
            "status": identification_status,
            "train": aggregate(eligible_train),
            "holdout": aggregate(eligible_holdout),
            "features": [
                "filtered_force",
                "finite_difference_force_rate",
                "tcp_linear_speed_proxy",
                "raw_minus_filtered_force",
                "saturation_flags",
            ],
            "not_identified": [
                "effective_normal_stiffness_n_per_m",
                "contact_damping_n_s_per_m",
                "normal_velocity_command",
                "transport_delay_s",
            ],
            "reason": "path_trace_has_no_commanded_normal_displacement_and_no_authoritative_contact_model",
        },
        "attempts": summaries,
        "claims": [
            "phase-correction/ILC rows are excluded before identification and tuning",
            "raw force bins are recomputed with a gap-preserving rule; no interpolation is used",
            "the trace can bound measured force-rate and saturation behavior but cannot identify contact stiffness",
            "the synthetic certificate is conditional on declared stiffness and sector bounds, not on this unknown-surface trace",
        ],
    }


def build_study(path: Path, *, max_rows: int | None = None) -> dict[str, Any]:
    result = audit_trace(path, max_rows=max_rows)
    result["synthetic_study"] = run_synthetic_study()
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True, help="one raw R013 path-trace JSONL")
    parser.add_argument("--output", type=Path, required=True, help="explicit JSON artifact path")
    parser.add_argument("--max-rows", type=int, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = build_study(args.trace, max_rows=args.max_rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(args.output), "schema": result["schema"], "selected_candidate": result["synthetic_study"]["selected_candidate"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
