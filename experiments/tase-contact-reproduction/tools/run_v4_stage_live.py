#!/usr/bin/env python3
"""Run one isolated V4 Stage A/B campaign through the mature R013 owner.

The caller supplies a *fresh prepared compatibility run directory*.  The
compatibility R013 campaign is used only for resident/source/guard binding;
the new V4 stage ledger owns proposal budget, stage identity and exact/censored
accounting.  No second RTDE writer is created.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import math
from pathlib import Path
from typing import Any, Mapping

from step5d_autotune_v4_r013.campaign import Campaign
from step5d_autotune_v4_r013.live_owner import (
    R013OwnerError,
    R013PathProfileV1,
    build_r013_live_context,
)
from step5d_autotune_v4_r012.live_host import ActiveCensorRuntime
from step5d_autotune_v4_r012.scheduler import ScheduledCandidate
from step5d_autotune_v4_r006.live_adapter import R006LiveAdapterError
from step5d_autotune_v4_r013.v4_stage_live_adapter import V4StageDispatchBindingV1
from step5d_autotune_v4_r013.v4_two_stage_campaign import (
    V4Stage,
    V4StageCampaignV1,
    V4CrossStageBaselineV1,
    V4FailureClass,
    V4FailureDisposition,
    V4FailureEvidenceV1,
    V4RecoveryReceiptV1,
    load_three_stage_config,
)
from step5d_autotune_v4_r013.recovery import (
    safe_home_then_resume_for_recoverable_failures_only,
)
from step5d_autotune_v4_r013.v4_stage_censor import V4ActiveCensorRuntime


HISTORICAL_COORDINATE = {
    "force_p_gain": 0.019027313840405524,
    "force_damping": 188.36079701683204,
    "force_i_gain": 0.0,
    "i_off": True,
    "normal_filter_tau_s": 0.04375,
    "orientation_ko": 0.05,
    "motion_kp": 2.5226892457611436,
    "target_force_n": 5.0,
}

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--stage-ledger", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--stage", choices=[stage.value for stage in V4Stage], required=True)
    parser.add_argument("--launch-profile", type=Path, required=True)
    parser.add_argument(
        "--seed-candidate-json",
        type=Path,
        help="Stage-B seed receipt; only controller coordinates are inherited.",
    )
    parser.add_argument(
        "--cross-stage-baseline-json",
        type=Path,
        help=(
            "Validated prior-stage confirmation report used only as the early-stop "
            "threshold until this stage has its own confirmed n=3 incumbent."
        ),
    )
    parser.add_argument(
        "--timing-characterization-json",
        type=Path,
        required=True,
        help="independent 3-run SCHED_FIFO coalescing timing receipt",
    )
    parser.add_argument(
        "--boundary-canary-attempts",
        type=int,
        choices=(2,),
        help=(
            "run exactly two fixed-candidate physical attempts as timing-only "
            "evidence; rows are never exact/BO observations"
        ),
    )
    parser.add_argument(
        "--stop-target-mae-n",
        type=float,
        help=(
            "Optional bounded campaign stop: after a newly sealed exact row "
            "passes all physical gates and has MAE <= this value, stop "
            "without winner confirmation."
        ),
    )
    parser.add_argument(
        "--stop-after-attempts",
        type=int,
        help=(
            "Optional hard physical-attempt budget for a bounded campaign. "
            "No confirmation attempts are issued after this budget."
        ),
    )
    parser.add_argument("--controller-host", default="192.168.1.18")
    parser.add_argument("--kunwei-host", default="192.168.50.25")
    parser.add_argument("--kunwei-port", type=int, default=5152)
    return parser.parse_args()


def _write_status(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_value(value), sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _validate_bounded_stop_controls(
    *, stop_target_mae_n: float | None, stop_after_attempts: int | None
) -> None:
    """Validate the optional target-or-epoch stop contract before opening live IO."""

    if stop_target_mae_n is not None:
        if not math.isfinite(float(stop_target_mae_n)) or float(stop_target_mae_n) <= 0.0:
            raise ValueError("--stop-target-mae-n must be finite and positive")
    if stop_after_attempts is not None:
        if isinstance(stop_after_attempts, bool) or int(stop_after_attempts) <= 0:
            raise ValueError("--stop-after-attempts must be a positive integer")
    if stop_target_mae_n is None and stop_after_attempts is None:
        return
    if stop_target_mae_n is None or stop_after_attempts is None:
        raise ValueError(
            "bounded V4 stop requires both --stop-target-mae-n and --stop-after-attempts"
        )


def _bounded_stop_reason(
    *,
    target_hit: bool,
    attempt_count: int,
    stop_after_attempts: int | None,
    campaign_complete: bool,
) -> str | None:
    """Return the first bounded stop reason without entering confirmation."""

    if target_hit:
        return "target_achieved"
    if stop_after_attempts is None:
        return None
    if attempt_count >= stop_after_attempts:
        return "epoch_budget_exhausted"
    if campaign_complete:
        return "search_budget_exhausted"
    return None


def _json_value(value: Any) -> Any:
    if hasattr(value, "as_dict"):
        return _json_value(value.as_dict())
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _latest_stage_lifecycle_receipt(run_dir: Path) -> Mapping[str, Any] | None:
    """Find the newest non-qualification lifecycle receipt after a flow gate."""

    candidates: list[tuple[float, Mapping[str, Any]]] = []
    for path in Path(run_dir).glob("*.r013life.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, Mapping) or value.get("kind") == "QUALIFICATION":
            continue
        candidates.append((path.stat().st_mtime, {**dict(value), "receipt_path": str(path)}))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _load_stage_seed(path: Path, *, stage: V4Stage) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    candidate = raw.get("candidate") if isinstance(raw, Mapping) else None
    if not isinstance(candidate, Mapping):
        raise RuntimeError("stage seed receipt has no candidate object")
    seed = dict(candidate)
    if stage in {V4Stage.FF_ION_6D_100, V4Stage.FF_ION_LIMIT_7D_100}:
        seed.update({"force_i_gain": 0.008610779292198037, "i_off": False})
    else:
        seed.update({"force_i_gain": 0.0, "i_off": True})
    return seed


def _record_terminal_failure(
    *,
    stage_campaign: V4StageCampaignV1,
    run_dir: Path,
    status_path: Path,
    stage: V4Stage,
    error: BaseException,
    context: Any | None = None,
) -> V4RecoveryReceiptV1 | None:
    return _record_recovery_failure(
        stage_campaign=stage_campaign,
        run_dir=run_dir,
        status_path=status_path,
        stage=stage,
        error=error,
        context=context,
    )


def _is_timing_or_coverage_failure(error: BaseException) -> bool:
    """Classify the resident attempt-boundary failures that must pause the lane."""

    text = str(error).lower()
    return any(
        marker in text
        for marker in (
            "freshness gap",
            "timing gap",
            "path coverage",
            "complete force lifecycle",
            "lifecycle freshness",
            "exact trial duration",
            "sealed bins",
        )
    )


def _failure_evidence_for_exception(
    error: BaseException,
    *,
    lifecycle: Mapping[str, Any] | None,
    context: Any | None,
) -> V4FailureEvidenceV1:
    """Map owner evidence to the closed recovery vocabulary."""

    text = str(error).lower()
    if any(marker in text for marker in (
        "force invariant",
        "invariant envelope",
        "hard_abs",
        "hard_force",
        "force_norm",
        "torque_norm",
    )):
        failure_class = V4FailureClass.FORCE_INVARIANT
    elif any(marker in text for marker in ("protective stop", "protective_stop")):
        failure_class = V4FailureClass.PROTECTIVE_STOP
    elif any(marker in text for marker in ("emergency stop", "emergency_stop")):
        failure_class = V4FailureClass.EMERGENCY_STOP
    elif any(marker in text for marker in ("raw sensor", "sensor anomaly", "sensor_stale")):
        failure_class = V4FailureClass.RAW_SENSOR_ANOMALY
    elif any(marker in text for marker in ("joint anomaly", "joint fault", "actual_qd")):
        failure_class = V4FailureClass.JOINT_ANOMALY
    elif any(marker in text for marker in ("source identity", "source_identity", "triplet")):
        failure_class = V4FailureClass.SOURCE_IDENTITY_MISMATCH
    elif any(marker in text for marker in ("home unsafe", "unsafe home", "not verified at home")):
        failure_class = V4FailureClass.HOME_UNSAFE
    elif _is_timing_or_coverage_failure(error):
        failure_class = V4FailureClass.TIMING_BOUNDARY
    elif any(marker in text for marker in ("transport", "socket", "connection", "rtde")):
        failure_class = V4FailureClass.TRANSPORT
    elif any(marker in text for marker in ("host", "writer", "runtime", "boundary")):
        failure_class = V4FailureClass.HOST_BOUNDARY
    else:
        failure_class = V4FailureClass.UNKNOWN

    observed = lifecycle if isinstance(lifecycle, Mapping) else {}
    evidence = V4FailureEvidenceV1(
        failure_class=failure_class,
        reason=f"{type(error).__name__}: {error}",
        home_permitted=False,
        home_verified=observed.get("home_verified") is True,
        safety_fault=(
            observed.get("safety_gate") is False
            or failure_class in {
                V4FailureClass.PROTECTIVE_STOP,
                V4FailureClass.EMERGENCY_STOP,
            }
        ),
        force_fault=failure_class is V4FailureClass.FORCE_INVARIANT,
        sensor_fault=failure_class is V4FailureClass.RAW_SENSOR_ANOMALY,
        joint_fault=failure_class is V4FailureClass.JOINT_ANOMALY,
        protective_stop=failure_class is V4FailureClass.PROTECTIVE_STOP,
        emergency_stop=failure_class is V4FailureClass.EMERGENCY_STOP,
        source_identity_matches=failure_class is not V4FailureClass.SOURCE_IDENTITY_MISMATCH,
    )
    owner_permission = getattr(context, "safe_home_permitted", None)
    if callable(owner_permission):
        evidence = replace(evidence, home_permitted=bool(owner_permission(evidence)))
    return evidence


def _resident_epoch(run_dir: Path) -> int | None:
    try:
        ready = json.loads((Path(run_dir) / "r013_live_owner_ready.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    try:
        epoch = int(ready["session_epoch"])
    except (KeyError, TypeError, ValueError):
        return None
    return epoch if epoch > 0 else None


def _record_recovery_failure(
    *,
    stage_campaign: V4StageCampaignV1,
    run_dir: Path,
    status_path: Path,
    stage: V4Stage,
    error: BaseException,
    context: Any | None,
) -> V4RecoveryReceiptV1 | None:
    if stage_campaign.in_flight is None:
        return None
    lifecycle = _latest_stage_lifecycle_receipt(run_dir)
    evidence = _failure_evidence_for_exception(
        error,
        lifecycle=lifecycle,
        context=context,
    )
    current_attempt = stage_campaign.in_flight
    owner_home = getattr(context, "safe_home_after_failure", None)
    home_action = owner_home if evidence.home_permitted and callable(owner_home) else None
    receipt = safe_home_then_resume_for_recoverable_failures_only(
        failure=evidence,
        current_attempt_ordinal=current_attempt.ordinal,
        attempt_count=stage_campaign.attempt_count + 1,
        prior_epoch=_resident_epoch(run_dir) or 1,
        fresh_epoch=None,
        fresh_readiness=False,
        # The failed dispatch is closed by record_recoverable_failure below;
        # the next dispatch is gated independently by the fresh epoch/readiness
        # fields and must never reuse this in-flight identity.
        dispatch_in_flight=False,
        dispatch_id=None,
        home_action=home_action,
    )
    receipt_path = None if lifecycle is None else str(lifecycle.get("receipt_path", ""))
    home_receipt = receipt.home_receipt or {}
    home_path = str(home_receipt.get("receipt_path", "")) or receipt_path
    if receipt.disposition is V4FailureDisposition.RECOVERABLE_RESUME:
        stage_campaign.record_recoverable_failure(
            receipt,
            partial_receipt_path=receipt_path or None,
            home_receipt_path=home_path or None,
        )
    else:
        stage_campaign.record_failure(
            receipt.reason,
            partial_receipt_path=receipt_path or None,
            home_receipt_path=home_path or None,
        )
    _write_status(status_path, {
        "state": (
            "recoverable_resume_pending"
            if receipt.disposition is V4FailureDisposition.RECOVERABLE_RESUME
            else "terminal_failure"
        ),
        "stage": stage.value,
        "campaign": stage_campaign.status(),
        "error": receipt.reason,
        "lifecycle_receipt": lifecycle,
        "recovery": receipt.as_dict(),
        "next_dispatch_permitted": receipt.auto_dispatch_permitted,
    })
    return receipt


def _load_coalescing_timing_contract(path: Path, *, run_dir: Path) -> dict[str, Any]:
    """Load the independent, fresh 3-run timing characterization receipt."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("coalescing timing characterization receipt is unreadable") from exc
    if (
        not isinstance(value, Mapping)
        or value.get("schema") != "step5d.autotune-v4/r013-timing-characterization-v2"
        or value.get("classification") != "coalescing_candidate"
        or value.get("target_met") is not True
        or int(value.get("timing_observation_count", 0)) < 3
    ):
        raise RuntimeError("coalescing timing characterization is not a 3-run pass")
    process = value.get("timing_process")
    if (
        not isinstance(process, Mapping)
        or process.get("schema") != "step5d.autotune-v4/r013-timing-process-receipt-v2"
        or process.get("scope") != "per_attempt"
        or process.get("profile_id") not in {"late_control_fifo_v1", "quota_safe_other_v1"}
        or process.get("receipt_count", 0) < 3
        or process.get("helper_non_other_thread_count") != 0
        or process.get("restore_verified") is not True
        or process.get("kernel_rt_bandwidth_unchanged") is not True
        or process.get("early_process_promotion") is not False
        or process.get("gc_window_receipt_count", 0) < 3
        or process.get("gc_window_entered") is not True
        or process.get("gc_window_restored") is not True
        or (process.get("profile_id") == "late_control_fifo_v1" and process.get("policy") != "SCHED_FIFO")
        or (process.get("profile_id") == "quota_safe_other_v1" and process.get("policy") != "SCHED_OTHER")
        or process.get("priority") != (20 if process.get("profile_id") == "late_control_fifo_v1" else 0)
        or tuple(process.get("cpu_affinity", ())) != (11, 13, 14, 15)
    ):
        raise RuntimeError("coalescing timing characterization process receipt differs")
    for row in value.get("observations", ()):
        if not isinstance(row, Mapping):
            continue
        row_run_dir = row.get("run_dir")
        if row_run_dir is not None and Path(str(row_run_dir)).resolve() != Path(run_dir).resolve():
            raise RuntimeError("coalescing timing characterization run root differs")
        scheduler = row.get("timing_scheduler")
        if (
            not isinstance(scheduler, Mapping)
            or scheduler.get("schema") != "step5d.autotune-v4/r013-timing-scheduler-receipt-v2"
            or scheduler.get("restore_verified") is not True
            or scheduler.get("kernel_rt_bandwidth_unchanged") is not True
            or scheduler.get("helper_non_other_thread_count") != 0
        ):
            raise RuntimeError("timing observation lacks a complete per-attempt scheduler receipt")
        gc_window = row.get("gc_window")
        if (
            not isinstance(gc_window, Mapping)
            or gc_window.get("schema") != "step5d.autotune-v4/r013-gc-window-receipt-v1"
            or gc_window.get("entered") is not True
            or gc_window.get("restored") is not True
            or gc_window.get("restored_after_timing_lease") is not True
            or gc_window.get("pre_enabled") != gc_window.get("post_enabled")
        ):
            raise RuntimeError("timing observation lacks a complete GC critical-window receipt")
    return dict(value)
def _coalescing_trial_gate(result: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Convert only a legacy ratio-only failure into the reviewed gate."""

    admission = result.get("physical_admission")
    timing = result.get("timing_evidence")
    admission_mapping = (
        admission.as_dict()
        if hasattr(admission, "as_dict")
        else admission
    )
    if not isinstance(admission_mapping, Mapping) or not isinstance(timing, Mapping):
        return False, {"reason": "missing_per_trial_timing_evidence"}
    rates = timing.get("layer_rates_hz")
    try:
        writer_hz = float(rates["writer_publishes"])
        rtde_hz = float(rates["rtde_frames"])
        tp_hz = float(rates["tp_consumed_packet_echoes"])
        p99_s = float(timing["feedback_age_p99_s"])
        gap_s = float(timing["max_fresh_gap_s"])
        ratio = tp_hz / writer_hz
    except (TypeError, KeyError, ValueError, ZeroDivisionError, OverflowError):
        return False, {"reason": "invalid_per_trial_timing_evidence"}
    passed = bool(
        admission_mapping.get("sealed") is True
        and result.get("home") is True
        and result.get("safe_return") is True
        and admission_mapping.get("motion_gate") is True
        and writer_hz >= 460.0
        and rtde_hz >= 460.0
        and tp_hz >= 460.0
        and p99_s <= 0.010
        and gap_s < 0.020
        and 0.95 <= ratio < 0.98
        and result.get("legacy_timing_gate") is False
    )
    return passed, {
        "schema": "step5d.autotune-v4/r013-coalescing-aware-trial-timing-v1",
        "accepted": passed,
        "legacy_timing_gate": bool(result.get("legacy_timing_gate")),
        "writer_hz": writer_hz,
        "rtde_hz": rtde_hz,
        "tp_hz": tp_hz,
        "tp_writer_ratio": ratio,
        "feedback_p99_s": p99_s,
        "max_fresh_gap_s": gap_s,
        "legacy_ratio_diagnostic_only": True,
    }


def main() -> int:
    args = _parse_args()
    try:
        _validate_bounded_stop_controls(
            stop_target_mae_n=args.stop_target_mae_n,
            stop_after_attempts=args.stop_after_attempts,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    run_dir = args.run_dir.resolve()
    timing_contract = _load_coalescing_timing_contract(
        args.timing_characterization_json,
        run_dir=run_dir,
    )
    ledger_path = run_dir / "r013_ledger.jsonl"
    if not ledger_path.is_file():
        raise SystemExit(f"fresh compatibility R013 ledger is missing: {ledger_path}")
    raw = json.loads(args.config.read_text(encoding="utf-8"))
    stage_a, stage_b, stage_b2 = load_three_stage_config(raw)
    stage = V4Stage(args.stage)
    config = {
        V4Stage.FF_IOFF_100: stage_a,
        V4Stage.FF_ION_6D_100: stage_b,
        V4Stage.FF_ION_LIMIT_7D_100: stage_b2,
    }[stage]
    seed = dict(HISTORICAL_COORDINATE)
    if stage is V4Stage.FF_ION_6D_100:
        seed.update({"force_i_gain": 0.008610779292198037, "i_off": False})
    if args.seed_candidate_json is not None:
        seed = _load_stage_seed(args.seed_candidate_json, stage=stage)
    cross_stage_baseline = None
    if args.cross_stage_baseline_json is not None:
        cross_stage_baseline = V4CrossStageBaselineV1.from_confirmation_report(
            args.cross_stage_baseline_json,
            target_stage=stage,
        )
    if args.stage_ledger.is_file() and args.stage_ledger.stat().st_size > 0:
        stage_campaign = V4StageCampaignV1.resume(
            config=config,
            seed_candidate=seed,
            fingerprint_sha256=config.fingerprint_sha256,
            ledger_path=args.stage_ledger,
            cross_stage_baseline=cross_stage_baseline,
        )
    else:
        stage_campaign = V4StageCampaignV1(
            config=config,
            seed_candidate=seed,
            fingerprint_sha256=config.fingerprint_sha256,
            ledger_path=args.stage_ledger,
            cross_stage_baseline=cross_stage_baseline,
        )
    compatibility_campaign = Campaign.resume(ledger_path)
    runtime_strategy = compatibility_campaign.runtime_strategy
    context = None
    boundary_canary = args.boundary_canary_attempts is not None
    status_path = run_dir / (
        f"v4_{stage.value.lower()}_boundary_canary_status.json"
        if boundary_canary
        else f"v4_{stage.value.lower()}_status.json"
    )
    bounded_target_hit = False
    bounded_target_mae_n: float | None = None
    bounded_stop_reason: str | None = None
    try:
        context = build_r013_live_context(
            run_dir=run_dir,
            controller_host=args.controller_host,
            kunwei_host=args.kunwei_host,
            kunwei_port=args.kunwei_port,
            launch_profile=args.launch_profile.resolve(),
            campaign_id=str(compatibility_campaign.ledger.header["campaign_id"]),
            run_id=str(compatibility_campaign.ledger.header["run_id"]),
            attempt_id=str(compatibility_campaign.ledger.header["attempt_id"]),
            runtime_strategy=runtime_strategy,
            campaign=compatibility_campaign,
            path_profile=R013PathProfileV1.cycloid(),
            feedforward_mode="on",
            timing_scheduler_profile=str(timing_contract["timing_process"]["profile_id"]),
        )
        _write_status(status_path, {
            "state": "owner_open",
            "stage": stage.value,
            "campaign": stage_campaign.status(),
            "scheduler": timing_contract["timing_process"],
            "timing_contract": {
                "path": str(args.timing_characterization_json.resolve()),
                "classification": timing_contract["classification"],
                "timing_observation_count": timing_contract["timing_observation_count"],
            },
        })

        def execute_attempt(stage_attempt: Any) -> None:
            nonlocal bounded_target_hit, bounded_target_mae_n
            try:
                binding = V4StageDispatchBindingV1.from_attempt(
                    stage_attempt,
                    stage=stage,
                    runtime_strategy_sha256=compatibility_campaign.runtime_strategy_sha256,
                    campaign_fingerprint=compatibility_campaign.campaign_fingerprint.as_dict(),
                )
            except R006LiveAdapterError as exc:
                # Candidate materialization is pre-motion.  Keep the typed
                # proposal failure in the 100-attempt ledger, but never open
                # a live context or retry it as an optimizer observation.
                stage_campaign.record_ineligible(
                    f"candidate_binding:{type(exc).__name__}: {exc}"
                )
                _write_status(status_path, {
                    "state": "ineligible_pre_motion",
                    "stage": stage.value,
                    "campaign": stage_campaign.status(),
                    "error": f"{type(exc).__name__}: {exc}",
                })
                return
            censor_runtime = None
            can_censor = (
                not boundary_canary
                and stage_attempt.ordinal <= config.attempt_budget
                and stage_attempt.method.value in {"qlognei_local", "qlognei_global", "local_polish"}
                and stage_campaign.censor_incumbent is not None
            )
            if can_censor:
                if context.path_early_end is None or context.configure_active_censor is None or context.configure_path_sample_observer is None:
                    raise RuntimeError("V4 active censor path seam is unavailable")
                censor_runtime = V4ActiveCensorRuntime(
                    controller=context.path_early_end,
                    campaign_id=str(compatibility_campaign.ledger.header["campaign_id"]),
                    run_id=str(compatibility_campaign.ledger.header["run_id"]),
                    attempt_id=str(compatibility_campaign.ledger.header["attempt_id"]),
                )
                incumbent = stage_campaign.censor_incumbent
                censor_runtime.arm(
                    scheduled=ScheduledCandidate(
                        candidate=binding.runtime_candidate,
                        kind="BO_TRIAL",
                        ordinal=stage_attempt.ordinal,
                        abort_allowed=True,
                    ),
                    # The R013 owner allocates the physical sequence only
                    # after qualifications and Home are accounted for; bind
                    # the register handshake inside run_trial, not to the
                    # stage-local ordinal.
                    attempt_sequence=None,
                    incumbent_mean_n=float(incumbent["mean_n"]),
                    baseline=incumbent,
                )
                context.configure_active_censor(censor_runtime)
                context.configure_path_sample_observer(censor_runtime.observe_path_sample)
            try:
                result = context.run_trial(binding.dispatch)
            except R013OwnerError as exc:
                recovery = _record_recovery_failure(
                    stage_campaign=stage_campaign,
                    run_dir=run_dir,
                    status_path=status_path,
                    stage=stage,
                    error=exc,
                    context=context,
                )
                _write_status(status_path, {
                    "state": (
                        "recoverable_resume_pending"
                        if recovery is not None
                        and recovery.disposition is V4FailureDisposition.RECOVERABLE_RESUME
                        else "terminal_failure"
                    ),
                    "stage": stage.value,
                    "campaign": stage_campaign.status(),
                    "last_dispatch": binding.as_dict(),
                    "error": f"{type(exc).__name__}: {exc}",
                    "recovery": None if recovery is None else recovery.as_dict(),
                    "next_dispatch_permitted": bool(
                        recovery is not None and recovery.auto_dispatch_permitted
                    ),
                })
                raise RuntimeError(
                    "V4 stage fail-closed recovery requires a fresh owner epoch: "
                    + ("unknown" if recovery is None else recovery.status.value)
                ) from exc
            finally:
                if context.configure_active_censor is not None:
                    context.configure_active_censor(None)
                if context.configure_path_sample_observer is not None:
                    context.configure_path_sample_observer(None)
            censored = result.get("censored_observation")
            admission = result.get("physical_admission")
            coalescing_override, coalescing_receipt = _coalescing_trial_gate(result)
            if coalescing_override and admission is not None:
                # The mature R008 record retains the legacy 0.98 decision.
                # V4 stage accounting may accept only this independently
                # characterized coalescing-aware timing contract; all other
                # safety, motion, Home and lifecycle fields remain unchanged.
                admission = replace(
                    admission,
                    physical_eligible=True,
                    timing_gate=True,
                    trial_admission_passed=True,
                )
                result = {
                    **result,
                    "physical_admission": admission,
                    "coalescing_timing_override": coalescing_receipt,
                }
            if boundary_canary:
                # Timing-only canary artifacts stay outside optimizer-facing
                # exact accounting even when the physical lifecycle is clean.
                stage_campaign.record_ineligible("boundary_canary_observation_only")
            elif censored is not None:
                stage_campaign.record_censored(
                    float(getattr(censored, "prefix_mean_n")),
                    closed_bin_count=int(getattr(censored, "closed_bin_count")),
                    incumbent_mean_n=float(getattr(censored, "incumbent_threshold_n")),
                    partial_receipt_path=str(result.get("force_lifecycle", {}).get("receipt_path", "")),
                    home_receipt_path=str(result.get("force_lifecycle", {}).get("receipt_path", "")),
                    trigger_watermark_s=float(getattr(censored, "watermark_s")),
                    censor_baseline=stage_campaign.censor_incumbent,
                )
            elif admission is None:
                stage_campaign.record_ineligible("missing_physical_admission")
            elif bool(getattr(admission, "physical_eligible", False)):
                sealed_mae_n = float(getattr(admission, "sealed_mae_n"))
                stage_campaign.record_exact(sealed_mae_n)
                if (
                    args.stop_target_mae_n is not None
                    and sealed_mae_n <= float(args.stop_target_mae_n)
                ):
                    bounded_target_hit = True
                    bounded_target_mae_n = sealed_mae_n
            else:
                if not bool(getattr(admission, "timing_gate", True)):
                    reason = "legacy_timing_gate_failed_without_coalescing_contract"
                    lifecycle_path = str(
                        result.get("force_lifecycle", {}).get("receipt_path", "")
                    ) or None
                    # A candidate-level timing rejection is a non-trainable,
                    # sealed outcome when all physical closure gates are
                    # intact.  It counts against the bounded stage budget and
                    # never reaches GP/tell_exact; it is not the same as a
                    # freshness/coverage exception, safety fault, or partial
                    # lifecycle, which remain terminal fail-closed blockers.
                    admission_value = (
                        admission.as_dict()
                        if hasattr(admission, "as_dict")
                        else admission
                    )
                    candidate_closed_safely = bool(
                        isinstance(admission_value, Mapping)
                        and admission_value.get("sealed") is True
                        and result.get("home") is True
                        and result.get("safe_return") is True
                        and admission_value.get("motion_gate") is True
                    )
                    if candidate_closed_safely:
                        stage_campaign.record_ineligible(
                            reason,
                            partial_receipt_path=lifecycle_path,
                            home_receipt_path=lifecycle_path,
                            timing_diagnostics=coalescing_receipt,
                            diagnostic_mae_n=(
                                float(admission_value.get("sealed_mae_n"))
                                if isinstance(admission_value, Mapping)
                                and admission_value.get("sealed_mae_n") is not None
                                else None
                            ),
                        )
                        _write_status(status_path, {
                            "state": "ineligible_timing",
                            "stage": stage.value,
                            "campaign": stage_campaign.status(),
                            "last_dispatch": binding.as_dict(),
                            "last_result": result,
                            "timing_override": coalescing_receipt,
                            "next_dispatch_permitted": True,
                        })
                        return
                    recovery = _record_recovery_failure(
                        stage_campaign=stage_campaign,
                        run_dir=run_dir,
                        status_path=status_path,
                        stage=stage,
                        error=RuntimeError(f"timing gap: {reason}"),
                        context=context,
                    )
                    _write_status(status_path, {
                        "state": (
                            "recoverable_resume_pending"
                            if recovery is not None
                            and recovery.disposition is V4FailureDisposition.RECOVERABLE_RESUME
                            else "terminal_failure"
                        ),
                        "stage": stage.value,
                        "campaign": stage_campaign.status(),
                        "last_dispatch": binding.as_dict(),
                        "last_result": result,
                        "timing_override": coalescing_receipt,
                        "recovery": None if recovery is None else recovery.as_dict(),
                        "next_dispatch_permitted": bool(
                            recovery is not None and recovery.auto_dispatch_permitted
                        ),
                    })
                    raise RuntimeError(
                        "V4 stage fail-closed recovery requires a fresh owner epoch"
                    )
                stage_campaign.record_ineligible("physical_admission_failed")
            _write_status(status_path, {
                "state": "exact_sealed" if stage_campaign.best_exact else "attempt_closed",
                "stage": stage.value,
                "campaign": stage_campaign.status(),
                "last_dispatch": binding.as_dict(),
                "last_result": result,
                "bounded_stop": {
                    "target_mae_n": args.stop_target_mae_n,
                    "attempt_budget": args.stop_after_attempts,
                    "target_hit": bounded_target_hit,
                    "hit_mae_n": bounded_target_mae_n,
                },
            })

        target_attempts = args.boundary_canary_attempts
        while True:
            if boundary_canary and target_attempts is not None:
                if stage_campaign.attempt_count >= target_attempts:
                    break
            else:
                bounded_stop_reason = _bounded_stop_reason(
                    target_hit=bounded_target_hit,
                    attempt_count=stage_campaign.attempt_count,
                    stop_after_attempts=args.stop_after_attempts,
                    campaign_complete=stage_campaign.complete,
                )
                if bounded_stop_reason is not None or stage_campaign.complete:
                    break
            stage_attempt = stage_campaign.ask(candidate=seed if boundary_canary else None)
            try:
                execute_attempt(stage_attempt)
            except Exception as exc:
                _record_terminal_failure(
                    stage_campaign=stage_campaign,
                    run_dir=run_dir,
                    status_path=status_path,
                    stage=stage,
                    error=exc,
                    context=context,
                )
                raise
        if boundary_canary:
            _write_status(
                status_path,
                {
                    "state": "boundary_canary_complete",
                    "stage": stage.value,
                    "campaign": stage_campaign.status(),
                    "timing_contract": {
                        "path": str(args.timing_characterization_json.resolve()),
                        "classification": timing_contract["classification"],
                    },
                    "bo_entered": False,
                    "tell_exact_called": False,
                },
            )
            return 0
        if bounded_stop_reason == "target_achieved":
            _write_status(
                status_path,
                {
                    "state": "target_achieved",
                    "stage": stage.value,
                    "campaign": stage_campaign.status(),
                    "bounded_stop": {
                        "target_mae_n": args.stop_target_mae_n,
                        "attempt_budget": args.stop_after_attempts,
                        "target_hit": True,
                        "hit_mae_n": bounded_target_mae_n,
                    },
                    "confirmation_complete": False,
                    "next_dispatch_permitted": False,
                },
            )
            return 0
        if bounded_stop_reason is not None:
            _write_status(
                status_path,
                {
                    "state": (
                        "epoch_budget_exhausted"
                        if bounded_stop_reason == "epoch_budget_exhausted"
                        else "search_budget_exhausted"
                    ),
                    "stage": stage.value,
                    "campaign": stage_campaign.status(),
                    "bounded_stop": {
                        "target_mae_n": args.stop_target_mae_n,
                        "attempt_budget": args.stop_after_attempts,
                        "target_hit": False,
                        "hit_mae_n": None,
                    },
                    "termination_class": bounded_stop_reason,
                    "confirmation_complete": False,
                    "next_dispatch_permitted": False,
                },
            )
            return 0
        # The discovery trial counts toward confirmation.  Only the missing
        # exact repeats are run after the 100-attempt search closes.
        confirmation_budget = max(0, config.winner_total_n - 1)
        while (
            stage_campaign.best_exact is not None
            and sum(
                row.candidate_token == stage_campaign.best_exact.candidate_token
                and row.outcome.value == "exact"
                for row in stage_campaign.attempts
            ) < config.winner_total_n
            and stage_campaign.confirmation_attempt_count < confirmation_budget
        ):
            token = stage_campaign.best_exact.candidate_token
            exact_count = sum(
                row.candidate_token == token and row.outcome.value == "exact"
                for row in stage_campaign.attempts
            )
            if exact_count >= config.winner_total_n:
                break
            try:
                execute_attempt(stage_campaign.ask_confirmation())
            except Exception as exc:
                _record_terminal_failure(
                    stage_campaign=stage_campaign,
                    run_dir=run_dir,
                    status_path=status_path,
                    stage=stage,
                    error=exc,
                    context=context,
                )
                raise
        exact_count = 0 if stage_campaign.best_exact is None else sum(
            row.candidate_token == stage_campaign.best_exact.candidate_token
            and row.outcome.value == "exact"
            for row in stage_campaign.attempts
        )
        if exact_count < config.winner_total_n:
            _write_status(status_path, {
                "state": "confirmation_incomplete",
                "stage": stage.value,
                "campaign": stage_campaign.status(),
                "termination_class": "bounded_confirmation_budget",
                "confirmation_complete": False,
                "confirmation_exact_count": exact_count,
                "safety_failure": False,
            })
        else:
            _write_status(status_path, {
                "state": "search_complete",
                "stage": stage.value,
                "campaign": stage_campaign.status(),
                "confirmation_complete": True,
            })
        return 0
    finally:
        if context is not None:
            try:
                context.stop("v4_stage_complete")
            finally:
                context.close()


if __name__ == "__main__":
    raise SystemExit(main())
