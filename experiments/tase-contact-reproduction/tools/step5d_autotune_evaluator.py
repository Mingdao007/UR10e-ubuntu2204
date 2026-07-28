#!/usr/bin/env python3
"""Fail-closed Step5d-native trial evaluator with fixed-F0 force scoring."""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path
from statistics import fmean, median, pstdev
from typing import Any, Iterable, Mapping, Sequence

from step5d_autotune_contract import CaptureManifest, Evaluation, TrialDisposition, TrialSpec
from step5d_autotune_governor import SaturationSample, SaturationTriggerEvidence
from step5d_autotune_state_machine import classify_terminal_reason


TIME_ALIASES = ("_step4e_path_time_s", "_step4e_line_stage_s", "path_time_s")
STAGE_ALIASES = ("ur_output_double_register_35", "_step5d_expected_stage", "stage")
FORCE_X_ALIASES = ("_step4e_force_b_x", "force_b_x_n", "force_x_base_n")
FORCE_Y_ALIASES = ("_step4e_force_b_y", "force_b_y_n", "force_y_base_n")
FORCE_Z_ALIASES = ("_step4e_force_b_z", "force_b_z_n", "force_z_base_n")
GOVERNOR_DIAGNOSTIC_FAILURES = frozenset(
    {
        "orientation_profile_unqualified",
        "cadence_failed",
        "feedback_failed",
    }
)


class MalformedEvidenceError(ValueError):
    pass


def _ineligible_disposition(manifest: CaptureManifest) -> TrialDisposition:
    """Preserve terminal safety/infra/code semantics when evidence is malformed."""

    return classify_terminal_reason(
        manifest.terminal_reason,
        host_cause=manifest.host_cause,
        safe_closure=manifest.returned_safe,
        eligible_evidence=False,
    )


def _finite_from_aliases(row: Mapping[str, Any], aliases: Sequence[str], label: str) -> float:
    for key in aliases:
        raw = row.get(key)
        if raw not in (None, ""):
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise MalformedEvidenceError(f"{label} is not numeric: {key}") from exc
            if not math.isfinite(value):
                raise MalformedEvidenceError(f"{label} is not finite: {key}")
            return value
    raise MalformedEvidenceError(f"missing {label}; aliases={list(aliases)}")


def _optional_finite(row: Mapping[str, Any], key: str) -> float | None:
    raw = row.get(key)
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise MalformedEvidenceError("CSV header is missing")
            rows = list(reader)
    except (OSError, csv.Error) as exc:
        raise MalformedEvidenceError(f"cannot read trial CSV: {exc}") from exc
    if not rows:
        raise MalformedEvidenceError("trial CSV has no rows")
    return rows


def _unit_normal(normal: Sequence[float]) -> tuple[float, float, float]:
    if len(normal) != 3:
        raise MalformedEvidenceError("F0 shadow normal must have three components")
    values = tuple(float(value) for value in normal)
    if any(not math.isfinite(value) for value in values):
        raise MalformedEvidenceError("F0 shadow normal is nonfinite")
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1e-6):
        raise MalformedEvidenceError("F0 shadow normal is not unit length")
    return values  # type: ignore[return-value]


def _is_stage25(row: Mapping[str, Any]) -> bool:
    stages: list[tuple[str, float]] = []
    for key in STAGE_ALIASES:
        raw = row.get(key)
        if raw in (None, ""):
            continue
        try:
            stage = float(raw)
        except (TypeError, ValueError) as exc:
            raise MalformedEvidenceError(f"stage is not numeric: {key}") from exc
        if not math.isfinite(stage):
            raise MalformedEvidenceError(f"stage is not finite: {key}")
        stages.append((key, stage))
    if not stages:
        raise MalformedEvidenceError(
            f"missing stage evidence; aliases={list(STAGE_ALIASES)}"
        )
    reference_key, reference = stages[0]
    for key, stage in stages[1:]:
        if not math.isclose(stage, reference, rel_tol=0.0, abs_tol=1e-9):
            raise MalformedEvidenceError(
                "conflicting stage aliases: "
                f"{reference_key}={reference!r} {key}={stage!r}"
            )
    return math.isclose(reference, 25.0, rel_tol=0.0, abs_tol=1e-9)


def fixed_f0_bin_metrics(
    rows: Iterable[Mapping[str, Any]],
    *,
    reaction_normal_base: Sequence[float],
    target_force_n: float = 12.0,
    start_s: float = 5.0,
    end_s: float = 60.0,
    bin_s: float = 0.1,
) -> dict[str, Any]:
    normal = _unit_normal(reaction_normal_base)
    required_bins = round((end_s - start_s) / bin_s)
    buckets: dict[int, list[float]] = defaultdict(list)
    selected_rows: list[Mapping[str, Any]] = []
    for row in rows:
        if not _is_stage25(row):
            continue
        time_s = _finite_from_aliases(row, TIME_ALIASES, "Stage25 path time")
        if time_s < start_s or time_s >= end_s:
            continue
        force = (
            _finite_from_aliases(row, FORCE_X_ALIASES, "base force x"),
            _finite_from_aliases(row, FORCE_Y_ALIASES, "base force y"),
            _finite_from_aliases(row, FORCE_Z_ALIASES, "base force z"),
        )
        load_n = sum(force[index] * normal[index] for index in range(3))
        bin_index = int(math.floor((time_s - start_s) / bin_s + 1e-9))
        if 0 <= bin_index < required_bins:
            buckets[bin_index].append(load_n)
            selected_rows.append(row)
    complete_ids = tuple(index for index in range(required_bins) if buckets.get(index))
    bin_means = tuple(fmean(buckets[index]) for index in complete_ids)
    missing_bins = tuple(index for index in range(required_bins) if not buckets.get(index))
    result: dict[str, Any] = {
        "complete_bins": len(complete_ids),
        "required_bins": required_bins,
        "missing_bins": missing_bins,
        "selected_rows": len(selected_rows),
        "bin_means_n": bin_means,
        "fixed_f0_reaction_normal_base": normal,
    }
    if len(complete_ids) == required_bins:
        errors = tuple(load - target_force_n for load in bin_means)
        result.update(
            {
                "mae_n": fmean(abs(value) for value in errors),
                "bias_n": fmean(errors),
                "std_n": pstdev(errors),
                "coverage_12_plus_minus_1_ratio": fmean(abs(value) <= 1.0 for value in errors),
            }
        )
    return result


def _window_profile_metrics(
    rows: Iterable[Mapping[str, Any]],
    *,
    start_s: float,
    end_s: float,
    tp_accel_limit_rad_s2: float,
) -> dict[str, Any]:
    orientation_errors: list[float] = []
    angular_saturation = 0
    angular_saturation_evidence_rows = 0
    qdot_saturation = 0
    slew_saturation = 0
    slew_evidence_rows = 0
    tp_accel_saturation = 0
    tp_accel_evidence_rows = 0
    count = 0
    for row in rows:
        if not _is_stage25(row):
            continue
        try:
            time_s = _finite_from_aliases(row, TIME_ALIASES, "Stage25 path time")
        except MalformedEvidenceError:
            continue
        if not start_s <= time_s < end_s:
            continue
        count += 1
        orientation = _optional_finite(row, "_step5d_contact_orientation_error_rad")
        if orientation is None:
            orientation = _optional_finite(row, "_step5d_outer_orientation_error_rad")
        if orientation is not None:
            orientation_errors.append(abs(orientation))
        raw_qdot = _optional_finite(row, "_step5d_rnn_qdot_max_abs_raw_rad_s")
        qdot_cap = _optional_finite(row, "_step5d_qdot_cap_rad_s")
        if raw_qdot is not None and qdot_cap and raw_qdot >= 0.98 * qdot_cap:
            qdot_saturation += 1
        slew = _optional_finite(row, "_step5d_qdot_slew_limiter_active")
        if slew is not None:
            slew_evidence_rows += 1
            slew_saturation += int(slew > 0.5)
        actual_qdd = [
            _optional_finite(row, f"ur_actual_qdd_{index}")
            for index in range(6)
        ]
        if all(value is not None for value in actual_qdd):
            tp_accel_evidence_rows += 1
            tp_accel_saturation += int(
                max(abs(float(value)) for value in actual_qdd if value is not None)
                >= 0.98 * tp_accel_limit_rad_s2
            )
        normal_rate_limited = _optional_finite(
            row, "_step5d_normal_rate_limiter_active"
        )
        if normal_rate_limited is not None:
            angular_saturation_evidence_rows += 1
            angular_saturation += int(normal_rate_limited > 0.5)

    sorted_orientation = sorted(orientation_errors)
    p95 = None
    maximum = None
    if sorted_orientation:
        index = min(len(sorted_orientation) - 1, math.ceil(0.95 * len(sorted_orientation)) - 1)
        p95 = sorted_orientation[index]
        maximum = sorted_orientation[-1]
    denominator = max(1, count)
    return {
        "eligible_rows": count,
        "orientation_error_p95_rad": p95,
        "orientation_error_max_rad": maximum,
        "orientation_evidence_complete": bool(
            count > 0 and len(orientation_errors) == count
        ),
        "angular_saturation_duty": angular_saturation / denominator,
        "angular_saturation_evidence_complete": bool(
            count > 0 and angular_saturation_evidence_rows == count
        ),
        "qdot_saturation_duty": min(qdot_saturation, count) / denominator,
        "host_slew_saturation_duty": slew_saturation / denominator,
        "host_slew_saturation_evidence_complete": bool(
            count > 0 and slew_evidence_rows == count
        ),
        "tp_accel_saturation_duty": tp_accel_saturation / denominator,
        "tp_accel_saturation_evidence_complete": bool(
            count > 0 and tp_accel_evidence_rows == count
        ),
    }


def profile_metrics(
    rows: Iterable[Mapping[str, Any]],
    *,
    angular_rate_limit_rad_s: float,
    tp_accel_limit_rad_s2: float,
) -> dict[str, Any]:
    materialized = list(rows)
    qualification = _window_profile_metrics(
        materialized,
        start_s=5.0,
        end_s=60.0,
        tp_accel_limit_rad_s2=tp_accel_limit_rad_s2,
    )
    entry = _window_profile_metrics(
        materialized,
        start_s=0.0,
        end_s=5.0,
        tp_accel_limit_rad_s2=tp_accel_limit_rad_s2,
    )
    p95 = qualification["orientation_error_p95_rad"]
    maximum = qualification["orientation_error_max_rad"]
    angular_duty = qualification["angular_saturation_duty"]
    qualification["orientation_qualified"] = bool(
        p95 is not None
        and maximum is not None
        and qualification["orientation_evidence_complete"]
        and qualification["angular_saturation_evidence_complete"]
        and p95 <= 0.036
        and maximum <= 0.05
        and angular_duty <= 0.05
    )
    return {**qualification, "entry_0_5s": entry}


def _correlation(lhs: Sequence[float], rhs: Sequence[float]) -> float:
    if len(lhs) != len(rhs) or not lhs:
        raise ValueError("tracking vectors must be non-empty and equal length")
    mean_lhs = fmean(lhs)
    mean_rhs = fmean(rhs)
    centered_lhs = [value - mean_lhs for value in lhs]
    centered_rhs = [value - mean_rhs for value in rhs]
    lhs_norm = math.sqrt(sum(value * value for value in centered_lhs))
    rhs_norm = math.sqrt(sum(value * value for value in centered_rhs))
    if lhs_norm <= 1e-15 or rhs_norm <= 1e-15:
        return 1.0 if all(
            math.isclose(a, b, rel_tol=0.0, abs_tol=1e-12)
            for a, b in zip(lhs, rhs)
        ) else 0.0
    return sum(a * b for a, b in zip(centered_lhs, centered_rhs)) / (
        lhs_norm * rhs_norm
    )


def qd_tracking_metrics(
    rows: Iterable[Mapping[str, Any]],
    *,
    start_s: float = 5.0,
    end_s: float = 60.0,
) -> dict[str, Any]:
    """Measure sent/actual qd tracking from immutable per-trial CSV rows."""

    samples: list[tuple[float, tuple[float, ...], tuple[float, ...]]] = []
    sample_periods: list[float] = []
    selected_rows = 0
    sent_keys = (
        "step4e_cmd_vx_m_s",
        "step4e_cmd_vy_m_s",
        "step4e_cmd_vz_m_s",
        "step4e_cmd_wx_rad_s",
        "step4e_cmd_wy_rad_s",
        "step4e_cmd_wz_rad_s",
    )
    for row in rows:
        if not _is_stage25(row):
            continue
        try:
            time_s = _finite_from_aliases(row, TIME_ALIASES, "Stage25 path time")
        except MalformedEvidenceError:
            continue
        if not start_s <= time_s < end_s:
            continue
        selected_rows += 1
        sent = tuple(_optional_finite(row, key) for key in sent_keys)
        actual = tuple(
            _optional_finite(row, f"ur_actual_qd_{index}") for index in range(6)
        )
        if any(value is None for value in (*sent, *actual)):
            continue
        samples.append(
            (
                time_s,
                tuple(float(value) for value in sent if value is not None),
                tuple(float(value) for value in actual if value is not None),
            )
        )
        period = _optional_finite(row, "ur_kinematics_dt_s")
        if period is not None and period > 0.0:
            sample_periods.append(period)
    if len(samples) < 32 or len(samples) != selected_rows:
        return {
            "evidence_complete": False,
            "sample_count": len(samples),
            "selected_row_count": selected_rows,
            "lag_s": None,
            "correlation": None,
            "nrmse": None,
        }
    deltas = [
        later[0] - earlier[0]
        for earlier, later in zip(samples, samples[1:])
        if later[0] > earlier[0]
    ]
    if not deltas:
        return {
            "evidence_complete": False,
            "sample_count": len(samples),
            "selected_row_count": selected_rows,
            "lag_s": None,
            "correlation": None,
            "nrmse": None,
        }
    dt_s = (
        median(sample_periods)
        if len(sample_periods) == len(samples)
        else median(deltas)
    )
    if not math.isfinite(dt_s) or dt_s <= 0.0 or dt_s > 0.020:
        return {
            "evidence_complete": False,
            "sample_count": len(samples),
            "selected_row_count": selected_rows,
            "lag_s": None,
            "correlation": None,
            "nrmse": None,
        }
    maximum_lag = max(0, int(math.floor(0.020 / dt_s + 1e-9)))
    best: tuple[float, int, list[float], list[float]] | None = None
    for lag in range(maximum_lag + 1):
        aligned = samples[:-lag] if lag else samples
        actual_rows = samples[lag:]
        if not aligned or len(aligned) != len(actual_rows):
            continue
        sent_flat = [value for row in aligned for value in row[1]]
        actual_flat = [value for row in actual_rows for value in row[2]]
        correlation = _correlation(sent_flat, actual_flat)
        if best is None or correlation > best[0]:
            best = (correlation, lag, sent_flat, actual_flat)
    if best is None:
        raise AssertionError("tracking lag search produced no candidate")
    correlation, lag, sent_flat, actual_flat = best
    rmse = math.sqrt(
        fmean((sent - actual) ** 2 for sent, actual in zip(sent_flat, actual_flat))
    )
    scale = max(max(sent_flat) - min(sent_flat), 1e-12)
    return {
        "evidence_complete": True,
        "sample_count": len(samples),
        "selected_row_count": selected_rows,
        "lag_s": lag * dt_s,
        "correlation": correlation,
        "nrmse": rmse / scale,
    }


def governor_trigger_metrics(
    rows: Iterable[Mapping[str, Any]],
    *,
    tp_accel_limit_rad_s2: float,
    tracking: Mapping[str, Any],
    start_s: float = 5.0,
    end_s: float = 60.0,
) -> dict[str, Any]:
    """Derive governor triggers from exact immutable trace rows."""

    lag_s = tracking.get("lag_s") if tracking.get("evidence_complete") is True else None
    correlation = (
        tracking.get("correlation")
        if tracking.get("evidence_complete") is True
        else None
    )
    nrmse = tracking.get("nrmse") if tracking.get("evidence_complete") is True else None
    samples: list[SaturationSample] = []
    for row in rows:
        if not _is_stage25(row):
            continue
        try:
            timestamp_s = _finite_from_aliases(
                row, TIME_ALIASES, "Stage25 path time"
            )
        except MalformedEvidenceError:
            continue
        if not start_s <= timestamp_s < end_s:
            continue
        normal_limited = _optional_finite(
            row, "_step5d_normal_rate_limiter_active"
        )
        raw_qdot = _optional_finite(
            row, "_step5d_rnn_qdot_max_abs_raw_rad_s"
        )
        qdot_cap = _optional_finite(row, "_step5d_qdot_cap_rad_s")
        host_slew_limited = _optional_finite(
            row, "_step5d_qdot_slew_limiter_active"
        )
        actual_qdd = tuple(
            _optional_finite(row, f"ur_actual_qdd_{index}")
            for index in range(6)
        )
        tp_utilization = 0.0
        if all(value is not None for value in actual_qdd):
            tp_utilization = max(
                abs(float(value)) for value in actual_qdd if value is not None
            ) / tp_accel_limit_rad_s2
        samples.append(
            SaturationSample(
                timestamp_s=timestamp_s,
                normal_filter_limited=bool(
                    normal_limited is not None and normal_limited > 0.5
                ),
                qdot_limited=bool(
                    raw_qdot is not None
                    and qdot_cap is not None
                    and qdot_cap > 0.0
                    and raw_qdot >= 0.98 * qdot_cap
                ),
                host_slew_limited=bool(
                    host_slew_limited is not None and host_slew_limited > 0.5
                ),
                tp_accel_utilization=tp_utilization,
                lag_s=lag_s,
                correlation=correlation,
                nrmse=nrmse,
            )
        )
    trigger = SaturationTriggerEvidence.from_samples(samples)
    return {
        "source": "immutable_trial_csv",
        "sample_count": len(samples),
        **trigger.payload(),
    }


def _structural_failures(trial: TrialSpec, manifest: CaptureManifest, complete_bins: int) -> list[str]:
    failures: list[str] = []
    if manifest.trial_uid != trial.trial_uid:
        failures.append("trial_uid_mismatch")
    if manifest.backend_id != trial.backend_id:
        failures.append("backend_identity_mismatch")
    if manifest.candidate_token != trial.candidate_token:
        failures.append("candidate_token_mismatch")
    if manifest.source_fingerprint_pre != trial.source_fingerprint:
        failures.append("source_fingerprint_pre_mismatch")
    if manifest.config_fingerprint_pre != trial.config_fingerprint:
        failures.append("config_fingerprint_pre_mismatch")
    if not manifest.fingerprint_closed:
        failures.append("pre_post_fingerprint_mismatch")
    if not manifest.hashes_complete:
        failures.append("capture_hashes_incomplete")
    if not manifest.completion_marker:
        failures.append("completion_marker_missing")
    if manifest.stage25_complete_s < 60.0:
        failures.append("stage25_shorter_than_60s")
    if complete_bins != trial.campaign.required_bins:
        failures.append("objective_bins_incomplete")
    for name, value in (
        ("cadence", manifest.cadence_ok),
        ("feedback", manifest.feedback_fresh),
        ("rnn_oracle", manifest.rnn_oracle_aligned),
        ("safety_normal", manifest.safety_normal),
        ("returned_safe", manifest.returned_safe),
    ):
        if not value:
            failures.append(f"{name}_failed")
    return failures


def evaluate_rows(
    trial: TrialSpec,
    manifest: CaptureManifest,
    rows: Iterable[Mapping[str, Any]],
) -> Evaluation:
    materialized = list(rows)
    try:
        normal = trial.campaign.f0_shadow_reaction_normal_base
        if normal is None:
            raise MalformedEvidenceError("campaign F0 shadow reaction normal is not frozen")
        objective = fixed_f0_bin_metrics(
            materialized,
            reaction_normal_base=normal,
            target_force_n=trial.campaign.target_force_n,
            start_s=trial.campaign.objective_window_start_s,
            end_s=trial.campaign.objective_window_end_s,
            bin_s=trial.campaign.objective_bin_s,
        )
        complete_bins = int(objective["complete_bins"])
        failures = _structural_failures(trial, manifest, complete_bins)
        profiles = profile_metrics(
            materialized,
            angular_rate_limit_rad_s=trial.execution_profile.normal_max_rate_rad_s,
            tp_accel_limit_rad_s2=trial.execution_profile.tp_speedj_accel_rad_s2,
        )
        orientation_evidence_complete = bool(
            profiles["orientation_evidence_complete"]
            and profiles["angular_saturation_evidence_complete"]
        )
        if not orientation_evidence_complete:
            failures.append("orientation_evidence_incomplete")
        # Orientation/profile qualification is a diagnostic, not a
        # structural objective gate.  A high normal-rate-limiter saturation
        # duty must not delete a complete, safely closed force-MAE trial.
        # Gross live safety and final safe closure remain independently gated.
        tracking = qd_tracking_metrics(materialized)
        if not tracking["evidence_complete"]:
            failures.append("qd_tracking_evidence_incomplete")
        trigger = governor_trigger_metrics(
            materialized,
            tp_accel_limit_rad_s2=trial.execution_profile.tp_speedj_accel_rad_s2,
            tracking=tracking,
        )
        for layer, complete in (
            (
                "normal_filter_rate",
                profiles["angular_saturation_evidence_complete"],
            ),
            (
                "host_qdot_slew",
                profiles["host_slew_saturation_evidence_complete"],
            ),
            (
                "tp_speedj_acceleration",
                profiles["tp_accel_saturation_evidence_complete"],
            ),
        ):
            if not complete:
                failures.append(f"governor_{layer}_burden_evidence_incomplete")
        disposition = classify_terminal_reason(
            manifest.terminal_reason,
            host_cause=manifest.host_cause,
            safe_closure=manifest.returned_safe and manifest.safety_normal,
            eligible_evidence=not failures,
        )
        eligible = not failures and disposition is TrialDisposition.OBJECTIVE
        profile_diagnostic_available = bool(
            failures
            and set(failures).issubset(GOVERNOR_DIAGNOSTIC_FAILURES)
            and manifest.terminal_reason == 1
            and complete_bins == trial.campaign.required_bins
        )
        objective_metrics = {
            key: value for key, value in objective.items() if key != "bin_means_n"
        }
        if not eligible:
            for key in (
                "mae_n",
                "bias_n",
                "std_n",
                "coverage_12_plus_minus_1_ratio",
            ):
                objective_metrics.pop(key, None)
        metrics = {
            "objective": objective_metrics,
            "profile": profiles,
            "governor": {
                "burden_by_layer": {
                    "normal_filter_rate": profiles["angular_saturation_duty"],
                    "host_qdot_slew": profiles["host_slew_saturation_duty"],
                    "tp_speedj_acceleration": profiles[
                        "tp_accel_saturation_duty"
                    ],
                },
                "burden_evidence_complete_by_layer": {
                    "normal_filter_rate": profiles[
                        "angular_saturation_evidence_complete"
                    ],
                    "host_qdot_slew": profiles[
                        "host_slew_saturation_evidence_complete"
                    ],
                    "tp_speedj_acceleration": profiles[
                        "tp_accel_saturation_evidence_complete"
                    ],
                },
                "tracking": tracking,
                "trigger": trigger,
                "nontrainable_profile_diagnostic": {
                    "available": profile_diagnostic_available,
                    "trainable_objective": False,
                    "failure_scope": (
                        (
                            "orientation_profile_unqualified"
                            if failures == ["orientation_profile_unqualified"]
                            else "governor_profile_nontrainable"
                        )
                        if profile_diagnostic_available
                        else None
                    ),
                    "structural_failures": (
                        list(failures) if profile_diagnostic_available else None
                    ),
                    "force_mae_n": (
                        float(objective["mae_n"])
                        if profile_diagnostic_available
                        else None
                    ),
                },
                "orientation": {
                    "evidence_complete": orientation_evidence_complete,
                    "qualified": profiles["orientation_qualified"],
                    "p95_error_rad": profiles["orientation_error_p95_rad"],
                    "max_error_rad": profiles["orientation_error_max_rad"],
                    "saturation_duty": profiles["angular_saturation_duty"],
                },
            },
            "terminal_reason": manifest.terminal_reason,
            "host_cause": manifest.host_cause,
            "fingerprint_closed": manifest.fingerprint_closed,
        }
        return Evaluation(
            trial_uid=trial.trial_uid,
            backend_id=trial.backend_id,
            eligible=eligible,
            disposition=disposition,
            objective_mae_n=float(objective["mae_n"]) if eligible else None,
            force_bias_n=float(objective["bias_n"]) if eligible else None,
            force_std_n=float(objective["std_n"]) if eligible else None,
            coverage_12_plus_minus_1_ratio=(
                float(objective["coverage_12_plus_minus_1_ratio"]) if eligible else None
            ),
            complete_bins=complete_bins,
            safe_closure=manifest.returned_safe,
            structural_failures=tuple(failures),
            metrics=metrics,
        )
    except (MalformedEvidenceError, KeyError, TypeError, ValueError) as exc:
        return Evaluation(
            trial_uid=trial.trial_uid,
            backend_id=trial.backend_id,
            eligible=False,
            disposition=_ineligible_disposition(manifest),
            objective_mae_n=None,
            force_bias_n=None,
            force_std_n=None,
            coverage_12_plus_minus_1_ratio=None,
            complete_bins=0,
            safe_closure=manifest.returned_safe,
            structural_failures=(f"malformed_evidence:{type(exc).__name__}:{exc}",),
            metrics={"quarantine_required": True},
        )


def evaluate_csv(trial: TrialSpec, manifest: CaptureManifest, csv_path: Path) -> Evaluation:
    try:
        rows = read_csv_rows(csv_path)
    except MalformedEvidenceError as exc:
        return Evaluation(
            trial_uid=trial.trial_uid,
            backend_id=trial.backend_id,
            eligible=False,
            disposition=_ineligible_disposition(manifest),
            objective_mae_n=None,
            force_bias_n=None,
            force_std_n=None,
            coverage_12_plus_minus_1_ratio=None,
            complete_bins=0,
            safe_closure=manifest.returned_safe,
            structural_failures=(f"malformed_evidence:{type(exc).__name__}:{exc}",),
            metrics={"quarantine_required": True, "csv_path": str(csv_path)},
        )
    return evaluate_rows(trial, manifest, rows)
