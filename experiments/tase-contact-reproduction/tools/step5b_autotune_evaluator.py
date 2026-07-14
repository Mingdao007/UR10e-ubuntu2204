#!/usr/bin/env python3
"""Evaluate one completed Step5b autotune trial without touching hardware."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from step5b_autotune_contract import (
    BRIDGE_PROFILE,
    CONSTRAINT_VIOLATION_REASONS,
    FATAL_SESSION_REASONS,
    OBJECTIVE_NAME,
    OBJECTIVE_UNIT,
    PARAMETER_TRUNCATION_FAILURES,
    Candidate,
    candidate_token_low31,
    is_parameter_constraint_failure,
    is_known_bad_history_path,
)
from step5b_autotune_evidence import (
    BACKEND_ID,
    EVALUATION_SCHEMA,
    TRIAL_SPEC_SCHEMA,
    candidate_uid_from_payload,
    fingerprint_core,
    physical_capture_uid_from_sha256,
    quarantine_evidence,
    sha256_file,
    source_config_fingerprint,
    trial_uid_from_identity,
)


def load_json(path: Path, *, label: str, malformed: list[str]) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant is forbidden: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        malformed.append(f"{label}_malformed:{type(exc).__name__}:{exc}")
        return {}
    if not isinstance(payload, dict):
        malformed.append(f"{label}_malformed:not_a_json_object")
        return {}
    return payload


def numeric(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype=float)
    return pd.to_numeric(df[column], errors="coerce")


def finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def base_result(
    run_dir: Path,
    *,
    failures: list[str],
    candidate: Candidate | None,
    trial_uid: str | None,
    candidate_uid: str | None,
    physical_capture_uid: str | None,
    fingerprint: dict[str, Any],
    quarantine_reasons: list[str],
    artifact_paths: list[Path],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": EVALUATION_SCHEMA,
        "backend_id": BACKEND_ID,
        "run_dir": str(run_dir),
        "trial_uid": trial_uid,
        "candidate_uid": candidate_uid,
        "physical_capture_uid": physical_capture_uid,
        "eligible": False,
        "feasible": False,
        "objective": None,
        "objective_name": OBJECTIVE_NAME,
        "objective_unit": OBJECTIVE_UNIT,
        "full_trial": False,
        "supervisor_closure_verified": False,
        "disposition": "INTEGRITY_FAILURE" if quarantine_reasons else "PLATFORM_FAILURE",
        "fingerprint": fingerprint,
        "failures": failures,
    }
    if candidate is not None:
        result["candidate"] = candidate.payload()
    if quarantine_reasons:
        quarantine_path = quarantine_evidence(
            run_dir,
            reasons=quarantine_reasons,
            artifact_paths=artifact_paths,
            context={"trial_uid": trial_uid, "backend_id": BACKEND_ID},
        )
        result["quarantined"] = True
        result["quarantine_path"] = str(quarantine_path) if quarantine_path else None
    else:
        result["quarantined"] = False
    return result


def active_stage25(df: pd.DataFrame) -> pd.DataFrame:
    stage = numeric(df, "ur_output_double_register_35")
    return df[(stage - 25.0).abs() < 0.05].copy()


def signed_normal_load(active: pd.DataFrame) -> pd.Series:
    """Return the force-frame-contract load without magnitude/sign substitution."""
    return numeric(active, "_step4e_normal_load_n")


def elapsed_s(active: pd.DataFrame) -> float:
    times = numeric(active, "t_monotonic_s").dropna()
    if len(times) < 2:
        return 0.0
    return max(0.0, float(times.iloc[-1] - times.iloc[0]))


def candidate_from_metadata(metadata: dict[str, Any]) -> Candidate:
    args = metadata.get("args") if isinstance(metadata.get("args"), dict) else {}
    return Candidate(
        target_force_n=float(args["target_force_n"]),
        force_p_gain=float(args["step4e_force_p_gain"]),
        force_i_gain=float(args["step4e_force_i_gain"]),
        force_damping=float(args["step4e_force_damping"]),
        normal_filter_alpha=float(args["step4e_normal_filter_alpha"]),
    )


def command_tv(active: pd.DataFrame) -> float:
    columns = [
        "step4e_cmd_vx_m_s",
        "step4e_cmd_vy_m_s",
        "step4e_cmd_vz_m_s",
        "step4e_cmd_wx_rad_s",
        "step4e_cmd_wy_rad_s",
        "step4e_cmd_wz_rad_s",
    ]
    if any(column not in active.columns for column in columns) or len(active) < 2:
        return 1.0
    values = np.column_stack([numeric(active, column).to_numpy(dtype=float) for column in columns])
    scales = np.array([0.004, 0.004, 0.01, 0.15, 0.15, 0.005], dtype=float)
    normalized_delta = np.diff(values, axis=0) / scales
    row_tv = np.linalg.norm(np.nan_to_num(normalized_delta, nan=0.0), axis=1)
    # A 10% cap change per 2 ms sample is already a large command discontinuity.
    return float(np.clip(np.mean(row_tv) / 0.10, 0.0, 1.0))


def near_limit_duty(active: pd.DataFrame) -> float:
    if active.empty:
        return 1.0
    linear = np.column_stack(
        [numeric(active, f"step4e_cmd_v{axis}_m_s").to_numpy(dtype=float) for axis in "xyz"]
    )
    angular = np.column_stack(
        [numeric(active, f"step4e_cmd_w{axis}_rad_s").to_numpy(dtype=float) for axis in "xyz"]
    )
    linear_norm = np.linalg.norm(np.nan_to_num(linear, nan=0.0), axis=1)
    angular_xy = np.linalg.norm(np.nan_to_num(angular[:, :2], nan=0.0), axis=1)
    normal = np.abs(np.nan_to_num(linear[:, 2], nan=0.0))
    near = (linear_norm >= 0.98 * 0.004) | (angular_xy >= 0.98 * 0.15) | (normal >= 0.98 * 0.01)
    return float(np.mean(near))


def evaluate_run(run_dir: Path, *, allow_history: bool = False) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    malformed: list[str] = []
    metadata = load_json(run_dir / "metadata.json", label="metadata", malformed=malformed)
    bridge_summary = load_json(run_dir / "summary.json", label="summary", malformed=malformed)
    runtime = load_json(run_dir / "trial_runtime.json", label="trial_runtime", malformed=malformed)
    capture_marker = load_json(
        run_dir / "capture_complete.json", label="capture_complete", malformed=malformed
    )
    trial_spec = load_json(run_dir / "trial_spec.json", label="trial_spec", malformed=malformed)
    fingerprint_pre = load_json(
        run_dir / "trial_fingerprint_pre.json", label="fingerprint_pre", malformed=malformed
    )
    fingerprint_post = load_json(
        run_dir / "trial_fingerprint_post.json", label="fingerprint_post", malformed=malformed
    )
    csv_paths = sorted(run_dir.glob("bridge_rtde_*hz.csv"))
    failures: list[str] = list(malformed)
    artifact_paths = [
        run_dir / "metadata.json",
        run_dir / "summary.json",
        run_dir / "trial_runtime.json",
        run_dir / "capture_complete.json",
        run_dir / "trial_spec.json",
        run_dir / "trial_fingerprint_pre.json",
        run_dir / "trial_fingerprint_post.json",
        *csv_paths,
    ]

    bridge_csv_sha256 = sha256_file(csv_paths[0]) if len(csv_paths) == 1 else None
    physical_capture_uid = (
        physical_capture_uid_from_sha256(bridge_csv_sha256)
        if bridge_csv_sha256 is not None
        else None
    )
    trial_uid: str | None = None
    candidate_uid: str | None = None

    fingerprint: dict[str, Any] = {
        "verified": False,
        "pre_combined_sha256": None,
        "post_combined_sha256": None,
    }
    pre_core: dict[str, Any] | None = None
    post_core: dict[str, Any] | None = None
    if not fingerprint_pre:
        failures.append("fingerprint_pre_missing")
        malformed.append("fingerprint_pre_missing")
    else:
        try:
            pre_core = fingerprint_core(fingerprint_pre, expected_phase="pre")
            fingerprint["pre_combined_sha256"] = pre_core["combined_sha256"]
        except (KeyError, TypeError, ValueError) as exc:
            reason = f"fingerprint_pre_invalid:{exc}"
            failures.append(reason)
            malformed.append(reason)
    if not fingerprint_post:
        failures.append("fingerprint_post_missing")
        malformed.append("fingerprint_post_missing")
    else:
        try:
            post_core = fingerprint_core(fingerprint_post, expected_phase="post")
            fingerprint["post_combined_sha256"] = post_core["combined_sha256"]
        except (KeyError, TypeError, ValueError) as exc:
            reason = f"fingerprint_post_invalid:{exc}"
            failures.append(reason)
            malformed.append(reason)
    if pre_core is not None and post_core is not None:
        if pre_core != post_core:
            failures.append("source_config_fingerprint_changed_during_trial")
            malformed.append("source_config_fingerprint_changed_during_trial")
        else:
            try:
                current_core = source_config_fingerprint()
            except (OSError, ValueError) as exc:
                reason = f"current_source_config_fingerprint_unavailable:{type(exc).__name__}:{exc}"
                failures.append(reason)
                malformed.append(reason)
            else:
                if post_core != current_core:
                    failures.append("source_config_fingerprint_stale_at_evaluation")
                    malformed.append("source_config_fingerprint_stale_at_evaluation")
                else:
                    fingerprint["verified"] = True

    if is_known_bad_history_path(run_dir):
        failures.append("known_false_or_zero_history_run")
    if not metadata:
        failures.append("metadata_missing")
    args = metadata.get("args") if isinstance(metadata.get("args"), dict) else {}
    if str(args.get("step4e_version", "")) != BRIDGE_PROFILE:
        failures.append("wrong_bridge_profile")
    declared_output_dir = str(args.get("output_dir", "")).strip()
    if declared_output_dir and Path(declared_output_dir).resolve() != run_dir:
        failures.append("metadata_output_dir_mismatch")
        malformed.append("metadata_output_dir_mismatch")
    if not csv_paths:
        failures.append("bridge_csv_missing")
    elif len(csv_paths) != 1:
        failures.append("multiple_bridge_csv_files")
        malformed.append("multiple_bridge_csv_files")

    candidate: Candidate | None = None
    try:
        candidate = candidate_from_metadata(metadata)
        candidate.validate(tier2_unlocked=True)
    except (KeyError, TypeError, ValueError) as exc:
        failures.append(f"candidate_metadata_invalid:{exc}")

    session_uid: str | None = None
    spec_trial_id: int | None = None
    spec_token: int | None = None
    trial_spec_sha256 = sha256_file(run_dir / "trial_spec.json")
    if not trial_spec:
        reason = "trial_spec_missing"
        failures.append(reason)
        malformed.append(reason)
    elif candidate is not None:
        try:
            if trial_spec.get("schema_version") != TRIAL_SPEC_SCHEMA:
                raise ValueError("trial spec schema mismatch")
            if trial_spec.get("backend_id") != BACKEND_ID:
                raise ValueError("trial spec backend mismatch")
            if trial_spec.get("candidate") != candidate.payload():
                raise ValueError("trial spec candidate differs from bridge metadata")
            candidate_uid = candidate_uid_from_payload(candidate.payload())
            if trial_spec.get("candidate_uid") != candidate_uid:
                raise ValueError("trial spec candidate_uid mismatch")
            session_uid = str(trial_spec["session_uid"])
            spec_trial_id = int(trial_spec["trial_id"])
            spec_token = int(trial_spec["candidate_token_low31"])
            trial_uid = trial_uid_from_identity(session_uid, spec_trial_id, candidate_uid)
            if trial_spec.get("trial_uid") != trial_uid:
                raise ValueError("trial spec trial_uid mismatch")
            if spec_token <= 0:
                raise ValueError("trial spec candidate token must be positive")
            if trial_spec.get("fingerprint_pre_sha256") != fingerprint.get("pre_combined_sha256"):
                raise ValueError("trial spec fingerprint does not bind pre fingerprint")
            if trial_spec_sha256 is None:
                raise ValueError("trial spec file digest unavailable")
        except (KeyError, TypeError, ValueError) as exc:
            reason = f"trial_spec_invalid:{exc}"
            failures.append(reason)
            malformed.append(reason)

    if failures or candidate is None or len(csv_paths) != 1:
        return base_result(
            run_dir,
            failures=failures,
            candidate=candidate,
            trial_uid=trial_uid,
            candidate_uid=candidate_uid,
            physical_capture_uid=physical_capture_uid,
            fingerprint=fingerprint,
            quarantine_reasons=malformed,
            artifact_paths=artifact_paths,
        )

    try:
        df = pd.read_csv(csv_paths[0], low_memory=False)
    except (OSError, UnicodeError, ValueError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
        reason = f"bridge_csv_malformed:{type(exc).__name__}:{exc}"
        return base_result(
            run_dir,
            failures=[reason],
            candidate=candidate,
            trial_uid=trial_uid,
            candidate_uid=candidate_uid,
            physical_capture_uid=physical_capture_uid,
            fingerprint=fingerprint,
            quarantine_reasons=[reason],
            artifact_paths=artifact_paths,
        )
    active = active_stage25(df)
    duration_s = elapsed_s(active)
    progress = numeric(active, "step4e_progress_m").dropna()
    max_progress_s = float(progress.max()) if not progress.empty else 0.0
    load_raw = signed_normal_load(active)
    load = load_raw.replace([np.inf, -np.inf], np.nan).dropna()
    target = candidate.target_force_n

    times = numeric(active, "t_monotonic_s")
    finite_times = times.to_numpy(dtype=float)
    time_deltas = np.diff(finite_times) if len(finite_times) >= 2 else np.array([])
    monotonic_time = bool(
        len(finite_times) == len(active)
        and np.isfinite(finite_times).all()
        and time_deltas.size
        and np.all(time_deltas > 0.0)
    )
    max_sample_gap_s = float(np.max(time_deltas)) if monotonic_time else math.inf

    if len(active) < 5000:
        failures.append("insufficient_stage25_samples")
    if duration_s < 59.5:
        failures.append("stage25_duration_lt_59p5s")
    if max_progress_s < 59.9:
        failures.append("path_progress_lt_59p9s")
    if not monotonic_time:
        failures.append("stage25_time_not_strictly_monotonic")
    elif max_sample_gap_s > 0.1:
        failures.append("stage25_sample_gap_gt_0p1s")
    if load.empty or float(load.median()) < 2.0:
        failures.append("missing_or_zero_contact_load")

    required_columns = [
        "t_monotonic_s",
        "sensor_ok",
        "_step4e_normal_load_n",
        "force_norm_n",
        "mx_nm_zeroed",
        "my_nm_zeroed",
        "mz_nm_zeroed",
        *[f"step4e_cmd_v{axis}_m_s" for axis in "xyz"],
        *[f"step4e_cmd_w{axis}_rad_s" for axis in "xyz"],
    ]
    missing_required = [column for column in required_columns if column not in active]
    if missing_required:
        failures.append("required_columns_missing:" + ",".join(missing_required))
    elif any(not np.isfinite(numeric(active, column).to_numpy(dtype=float)).all() for column in required_columns):
        failures.append("nonfinite_required_data")
    elif not bool((numeric(active, "sensor_ok") > 0.5).all()):
        failures.append("sensor_not_ok_during_stage25")

    terminal_reason = runtime.get("terminal_reason")
    if terminal_reason is None:
        failures.append("terminal_reason_missing")
    else:
        try:
            terminal_reason = int(terminal_reason)
        except (TypeError, ValueError) as exc:
            reason = f"terminal_reason_invalid:{type(exc).__name__}:{exc}"
            failures.append(reason)
            malformed.append(reason)
            terminal_reason = None
        else:
            if terminal_reason in CONSTRAINT_VIOLATION_REASONS:
                failures.append(f"constraint_terminal_reason_{terminal_reason}")
            elif terminal_reason in FATAL_SESSION_REASONS:
                failures.append(f"fatal_terminal_reason_{terminal_reason}")
            elif terminal_reason != 1:
                failures.append(f"nonparameter_terminal_reason_{terminal_reason}")
    if runtime and not bool(runtime.get("home_verified", False)):
        failures.append("home_not_verified")

    supervisor_closure_verified = False
    runtime_identity: tuple[int, int, int] | None = None
    if runtime:
        if runtime.get("schema_version") != "step5b_autotune_trial_runtime_v4":
            failures.append("supervisor_runtime_schema_invalid")
        try:
            identity_values = (
                int(runtime["session_epoch"]),
                int(runtime["trial_id"]),
                int(runtime["candidate_token_low31"]),
            )
            if any(value <= 0 for value in identity_values):
                raise ValueError("identity values must be positive")
            runtime_identity = identity_values
            expected_token = candidate_token_low31(identity_values[0], identity_values[1], candidate)
            if identity_values[2] != expected_token:
                raise ValueError("candidate token does not match epoch, trial, and candidate")
            if identity_values[1] != spec_trial_id or identity_values[2] != spec_token:
                raise ValueError("runtime identity differs from trial spec")
            repeated_runtime_identity = {
                "backend_id": BACKEND_ID,
                "session_uid": session_uid,
                "candidate_uid": candidate_uid,
                "trial_uid": trial_uid,
                "trial_spec_sha256": trial_spec_sha256,
                "physical_capture_uid": physical_capture_uid,
            }
            if any(runtime.get(key) != value for key, value in repeated_runtime_identity.items()):
                raise ValueError("runtime full-width identity binding mismatch")
        except (KeyError, TypeError, ValueError) as exc:
            runtime_identity = None
            reason = f"supervisor_runtime_identity_invalid:{exc}"
            failures.append(reason)
            malformed.append(reason)
        closure_checks = {
            "runtime_identity": runtime_identity is not None,
            "stable_trial_uid": trial_uid is not None,
            "fingerprint_verified": bool(fingerprint.get("verified")),
            "runtime_fingerprint_pre": runtime.get("fingerprint_pre_sha256")
            == fingerprint.get("pre_combined_sha256"),
            "runtime_fingerprint_post": runtime.get("fingerprint_post_sha256")
            == fingerprint.get("post_combined_sha256"),
            "capture_identity_binding": all(
                capture_marker.get(key) == value
                for key, value in {
                    "backend_id": BACKEND_ID,
                    "session_uid": session_uid,
                    "candidate_uid": candidate_uid,
                    "trial_uid": trial_uid,
                    "trial_spec_sha256": trial_spec_sha256,
                    "physical_capture_uid": physical_capture_uid,
                }.items()
            ),
            "capture_schema": capture_marker.get("schema_version")
            == "step5b_autotune_capture_manifest_v4",
            "fresh_run_observed": bool(runtime.get("fresh_run_observed", False)),
            "home_verified": bool(runtime.get("home_verified", False)),
            "home_release_ack": bool(runtime.get("home_release_ack", False)),
            "capture_complete": bool(capture_marker.get("complete", False)),
            "process_group_reaped": bool(capture_marker.get("process_group_reaped", False)),
            "capture_fresh_run": bool(capture_marker.get("fresh_run_observed", False)),
            "capture_home_verified": bool(capture_marker.get("home_verified", False)),
            "capture_home_release_ack": bool(capture_marker.get("home_release_ack", False)),
            "capture_fingerprint_verified": bool(capture_marker.get("fingerprint_verified", False)),
            "no_runtime_fatal_detail": not bool(runtime.get("fatal_detail")),
            "no_capture_fatal_detail": not bool(capture_marker.get("fatal_detail")),
        }
        failed_closure = [name for name, passed in closure_checks.items() if not passed]
        if failed_closure:
            failures.append("supervisor_closure_invalid:" + ",".join(failed_closure))
        else:
            supervisor_closure_verified = True

    force_norm = numeric(active, "force_norm_n")
    torque_cols = [column for column in ("mx_nm_zeroed", "my_nm_zeroed", "mz_nm_zeroed") if column in active]
    torque_norm = (
        np.linalg.norm(np.column_stack([numeric(active, column) for column in torque_cols]), axis=1)
        if len(torque_cols) == 3
        else np.array([])
    )
    if force_norm.notna().any() and float(force_norm.max()) >= 60.0:
        failures.append("force_norm_guard_reached")
    if load_raw.notna().any() and float(load_raw.abs().max()) >= 50.0:
        failures.append("raw_normal_guard_reached")
    if torque_norm.size and float(np.nanmax(torque_norm)) >= 3.0:
        failures.append("torque_guard_reached")

    full_trial = bool(
        duration_s >= 59.5
        and max_progress_s >= 59.9
        and monotonic_time
        and max_sample_gap_s <= 0.1
        and len(active) >= 5000
    )
    history_eligible = False
    if allow_history and not runtime:
        failures.append("legacy_history_is_audit_only")
    identity_and_closure_eligible = bool(
        trial_uid
        and candidate_uid
        and session_uid
        and physical_capture_uid
        and fingerprint.get("verified")
        and (supervisor_closure_verified or history_eligible)
    )
    if not identity_and_closure_eligible:
        failures.append("missing_supervisor_runtime_or_history_gate")

    parameter_constraint_failures = [
        failure for failure in failures if is_parameter_constraint_failure(failure)
    ]
    # A parameter guard normally truncates Stage25. Those three completeness
    # failures are consequences of the guard, not independent platform faults.
    tolerated_constraint_failures = set(parameter_constraint_failures)
    if parameter_constraint_failures:
        tolerated_constraint_failures.update(PARAMETER_TRUNCATION_FAILURES)
    non_parameter_failures = [
        failure for failure in failures if failure not in tolerated_constraint_failures
    ]
    eligible = bool(
        identity_and_closure_eligible
        and not malformed
        and not non_parameter_failures
        and (not failures or parameter_constraint_failures)
    )

    force_error = load.to_numpy(dtype=float) - target
    force_mae_n = float(np.mean(np.abs(force_error))) if load.size else math.inf
    force_rmse_n = float(np.sqrt(np.mean(force_error**2))) if load.size else math.inf
    force_p99_absolute_error_n = float(np.quantile(np.abs(force_error), 0.99)) if load.size else math.inf
    force_nrmse = float(np.clip(np.sqrt(np.mean(force_error**2)) / target, 0.0, 1.0)) if load.size else 1.0
    p99_error = float(np.clip(np.quantile(np.abs(force_error), 0.99) / target, 0.0, 1.0)) if load.size else 1.0
    path_x = numeric(active, "_step4e_path_error_x_m")
    path_y = numeric(active, "_step4e_path_error_y_m")
    path_sq = path_x**2 + path_y**2
    xy_rmse_m = float(np.sqrt(path_sq.dropna().mean())) if path_sq.notna().any() else math.inf
    xy_metric = float(np.clip(xy_rmse_m / 0.005, 0.0, 1.0)) if math.isfinite(xy_rmse_m) else 1.0
    tv_metric = command_tv(active)
    limit_duty = near_limit_duty(active)

    metrics = {
        "force_mae_n": force_mae_n if math.isfinite(force_mae_n) else None,
        "force_rmse_n": force_rmse_n if math.isfinite(force_rmse_n) else None,
        "force_p99_absolute_error_n": force_p99_absolute_error_n if math.isfinite(force_p99_absolute_error_n) else None,
        "force_nrmse": force_nrmse,
        "force_p99_absolute_error_over_target": p99_error,
        "xy_rmse_m": xy_rmse_m if math.isfinite(xy_rmse_m) else None,
        "xy_rmse_over_5mm": xy_metric,
        "normalized_command_total_variation": tv_metric,
        "near_limit_dwell_duty": limit_duty,
        "stage25_duration_s": duration_s,
        "stage25_samples": int(len(active)),
        "max_path_progress_s": max_progress_s,
        "max_sample_gap_s": max_sample_gap_s if math.isfinite(max_sample_gap_s) else None,
        "time_strictly_monotonic": monotonic_time,
        "normal_load_mean_n": float(load.mean()) if load.size else None,
        "normal_load_p99_n": float(load.quantile(0.99)) if load.size else None,
    }
    feasible = eligible and not failures
    objective = force_mae_n if feasible and math.isfinite(force_mae_n) else None
    if feasible:
        disposition = "OBJECTIVE"
    elif malformed:
        disposition = "INTEGRITY_FAILURE"
    elif eligible and parameter_constraint_failures:
        disposition = "PARAMETER_CONSTRAINT"
    else:
        disposition = "PLATFORM_FAILURE"
    quarantine_path = (
        quarantine_evidence(
            run_dir,
            reasons=malformed,
            artifact_paths=artifact_paths,
            context={"trial_uid": trial_uid, "backend_id": BACKEND_ID},
        )
        if malformed
        else None
    )
    return {
        "schema_version": EVALUATION_SCHEMA,
        "backend_id": BACKEND_ID,
        "run_dir": str(run_dir),
        "trial_uid": trial_uid,
        "session_uid": session_uid,
        "session_epoch": runtime_identity[0] if runtime_identity is not None else None,
        "trial_id": runtime_identity[1] if runtime_identity is not None else spec_trial_id,
        "candidate_uid": candidate_uid,
        "physical_capture_uid": physical_capture_uid,
        "trial_spec_sha256": trial_spec_sha256,
        "eligible": eligible,
        "feasible": feasible,
        "objective": objective,
        "objective_name": OBJECTIVE_NAME,
        "objective_unit": OBJECTIVE_UNIT,
        "full_trial": full_trial,
        "supervisor_closure_verified": supervisor_closure_verified,
        "disposition": disposition,
        "fingerprint": fingerprint,
        "quarantined": bool(malformed),
        "quarantine_path": str(quarantine_path) if quarantine_path else None,
        "candidate": candidate.payload(),
        "metrics": metrics,
        "failures": failures,
        "parameter_constraint_failures": parameter_constraint_failures,
        "non_parameter_failures": non_parameter_failures,
        "provenance": {
            "metadata": {"path": str(run_dir / "metadata.json"), "sha256": sha256_file(run_dir / "metadata.json")},
            "summary": {"path": str(run_dir / "summary.json"), "sha256": sha256_file(run_dir / "summary.json")},
            "trial_runtime": {
                "path": str(run_dir / "trial_runtime.json"),
                "sha256": sha256_file(run_dir / "trial_runtime.json"),
            } if runtime else None,
            "trial_spec": {
                "path": str(run_dir / "trial_spec.json"),
                "sha256": trial_spec_sha256,
            },
            "capture_complete": {
                "path": str(run_dir / "capture_complete.json"),
                "sha256": sha256_file(run_dir / "capture_complete.json"),
            } if capture_marker else None,
            "fingerprint_pre": {
                "path": str(run_dir / "trial_fingerprint_pre.json"),
                "sha256": sha256_file(run_dir / "trial_fingerprint_pre.json"),
            },
            "fingerprint_post": {
                "path": str(run_dir / "trial_fingerprint_post.json"),
                "sha256": sha256_file(run_dir / "trial_fingerprint_post.json"),
            },
            "bridge_csv": {"path": str(csv_paths[0]), "sha256": sha256_file(csv_paths[0])},
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--allow-history", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate_run(args.run_dir, allow_history=args.allow_history)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result["eligible"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
