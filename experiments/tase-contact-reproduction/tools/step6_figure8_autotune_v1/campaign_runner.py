"""Production owner for the independent Figure-eight BO campaign.

This module composes the reviewed R013 physical writer; it does not create a
second motion implementation.  It owns the durable campaign decisions around
that writer: matched handoff A/B, immutable fingerprint materialization,
trial-admission scheduling, sentinels/repeats, checkpoint release, and final
Home/ownership release.
"""

from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import time
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

from step5d_autotune_v4_r013.floor_coordinator import HandoffABPlanV1
from step5d_autotune_v4_r013.campaign import PhysicalAdmissionReceipt
from step5d_autotune_v4_r013.handoff import HandoffPolicy
from step5d_autotune_v4_r013.lifecycle_trace import iter_lifecycle_rows
from step5d_autotune_v4_r013.live_owner import (
    R013LiveContext,
    R013PathProfileV1,
    build_r013_live_context,
)
from step5d_autotune_v4_r013.prepare_live import (
    _dashboard_running,
    _loaded_target,
    _observe_dashboard,
    _require_remote_normal,
    _state_sample,
    _wait_dashboard,
)
from step5d_autotune_v4_r013.runtime_strategy import DISABLED_RUNTIME_STRATEGY
from step5d_remote_startup import RemoteDashboardWriter
from step5d_autotune_v4_r004.path_reference import PATH_STAGE_ID
from step5d_autotune_v4_r004.contracts import (
    HOME_ORIENTATION_TOLERANCE_RAD,
    HOME_POSITION_TOLERANCE_M,
)

from .core import (
    CompleteCandidateV1,
    EXACT_NOVEL_TARGET,
    FigureEightCensoredReceiptV1,
    FigureEightCampaignFingerprintV1,
    FigureEightError,
    FigureEightLaunchReceiptV1,
    FigureEightSchedulerV1,
    OperationalSegmentBudgetV1,
    PersistedSobolCursorV1,
    TrialPlanV1,
    build_campaign_fingerprint,
    build_frozen_campaign_fingerprint,
    load_campaign_config,
)
from .live_composition import (
    FIGURE8_FINAL_HOME_PROFILE_ID,
    FIGURE8_HOME_POSE,
    derive_figure8_home_calibration_receipt,
    figure8_runtime_path_reference,
    load_figure8_home_calibration_receipt,
)
from .physical_censor import (
    FigureEightCensorRuntimeError,
    FigureEightPhysicalCensorSeamV1,
    next_cold_attempt_sequence,
)
from .optimizer_bridge import SubprocessFigureEightProposalProviderV1
from .prepare_live import (
    CONTROLLER_TARGET,
    PROGRAM,
    prepare_figure8_live_run,
    verify_controller_readback,
)
from .source_identity import require_source_identity


RUNNER_SCHEMA = "step6.autotune/figure8-production-runner-state-v1"
TRIAL_RECEIPT_SCHEMA = "step6.autotune/figure8-physical-trial-receipt-v1"
DEFAULT_ROBOT_HOST = "192.168.1.18"
DEFAULT_KUNWEI_HOST = "192.168.50.25"
DEFAULT_KUNWEI_PORT = 5152
DEFAULT_OPTIMIZER_PYTHON = Path(
    "/home/andy/.local/share/step5d-autotune-v3/runtimes/"
    "8f980abdd3d9a4ebef460be259f6cd7c0c802015ee553201d3a888be83252475/"
    "optimizer/bin/python"
)


class FigureEightCampaignRunnerError(RuntimeError):
    """A production campaign transition could not be proven safe/exact."""


class SafeReturnProposalOverlapV1:
    """One-worker compute-only overlap; it never owns physical authority."""

    SCHEMA = "step6.autotune/figure8-safe-return-proposal-overlap-v1"

    def __init__(
        self,
        *,
        scheduler: FigureEightSchedulerV1,
        artifact_dir: Path,
    ) -> None:
        self.scheduler = scheduler
        self.artifact_dir = Path(artifact_dir)
        self.executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="figure8-safe-return-proposal",
        )
        self.future: Future[dict[str, Any]] | None = None
        self.plan: TrialPlanV1 | None = None
        self.started_at_s: float | None = None
        self.serial = 0
        self.error_count_before = 0

    def arm(self, *, context: R013LiveContext, plan: TrialPlanV1) -> None:
        if context.configure_safe_return_overlap is None:
            raise FigureEightCampaignRunnerError(
                "Figure-eight live context lacks the safe-return overlap seam"
            )
        if self.future is not None:
            raise FigureEightCampaignRunnerError(
                "Figure-eight safe-return overlap is already in flight"
            )
        self.plan = plan
        self.started_at_s = None
        self.error_count_before = len(context.safe_return_overlap_errors)

        def work() -> dict[str, Any]:
            try:
                prefetched = self.scheduler.prefetch_next_for_safe_return()
                return {
                    "passed": True,
                    "proposal_prefetched": prefetched is not None,
                    "prefetched_plan": FigureEightSchedulerV1._plan_dict(prefetched),
                    "error": None,
                }
            except Exception as exc:
                return {
                    "passed": False,
                    "proposal_prefetched": False,
                    "prefetched_plan": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }

        def launch() -> None:
            if self.future is not None:
                raise FigureEightCampaignRunnerError(
                    "Figure-eight safe-return overlap callback ran more than once"
                )
            self.started_at_s = time.time()
            self.future = self.executor.submit(work)

        context.configure_safe_return_overlap(launch)

    def finish_after_home(self, *, context: R013LiveContext) -> dict[str, Any]:
        if context.configure_safe_return_overlap is not None:
            context.configure_safe_return_overlap(None)
        plan = self.plan
        if plan is None:
            raise FigureEightCampaignRunnerError(
                "Figure-eight safe-return overlap lacks its trial plan"
            )
        if self.future is None:
            new_errors = context.safe_return_overlap_errors[
                self.error_count_before :
            ]
            result = {
                "passed": not new_errors,
                "proposal_prefetched": False,
                "prefetched_plan": None,
                "error": (
                    None
                    if not new_errors
                    else new_errors[-1]
                ),
            }
        else:
            result = dict(self.future.result())
        self.serial += 1
        body = {
            "schema": self.SCHEMA,
            "version": 1,
            "trial_plan": FigureEightSchedulerV1._plan_dict(plan),
            "overlap_phase": "safe_return" if self.started_at_s is not None else "not_reached",
            "started_at_s": self.started_at_s,
            "finished_at_s": time.time(),
            "physical_writer_count": 1,
            "physical_q": 1,
            "arm_authorized": False,
            "next_arm_requires_current_home_and_admission": True,
            **result,
        }
        body["receipt_sha256"] = _sha256_value(body)
        path = self.artifact_dir / f"overlap-{self.serial:06d}.json"
        _atomic_json(path, body)
        receipt = {**body, "path": str(path)}
        self.future = None
        self.plan = None
        self.started_at_s = None
        return receipt

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_value(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(dict(value), stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FigureEightCampaignRunnerError(f"artifact is not an object: {path}")
    return value


def provisional_anchor_candidate() -> CompleteCandidateV1:
    """The cycloid n=3 center, explicitly provisional and zero-correction."""

    return CompleteCandidateV1(
        controller_path={
            "force_p_gain": 0.019027313840405524,
            "force_damping": 188.36079701683204,
            "force_i_gain": 0.008610779292198037,
            "i_off": False,
            "normal_filter_tau_s": 0.04375,
            "orientation_ko": 0.05,
            "motion_kp": 2.5226892457611436,
            "target_force_n": 5.0,
        },
        correction_weights=(0.0,) * 6,
    )


def _safe_json(value: Any) -> Any:
    if hasattr(value, "as_dict") and callable(value.as_dict):
        return _safe_json(value.as_dict())
    if isinstance(value, Mapping):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_safe_json(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _raw_artifact(context: R013LiveContext, record: Any) -> tuple[Path, dict[str, Any]]:
    binding = getattr(record, "raw_artifact", None)
    if not isinstance(binding, Mapping):
        raise FigureEightCampaignRunnerError("physical record lacks raw path artifact")
    relative = Path(str(binding.get("relative_path", "")))
    if relative.is_absolute() or ".." in relative.parts:
        raise FigureEightCampaignRunnerError("raw path artifact escapes the run directory")
    path = (Path(context.physical_ledger.path).parent / relative).resolve()
    root = Path(context.physical_ledger.path).parent.resolve()
    if root not in path.parents or path.is_symlink() or not path.is_file():
        raise FigureEightCampaignRunnerError("raw path artifact is unavailable")
    if _sha256_file(path) != binding.get("byte_sha256"):
        raise FigureEightCampaignRunnerError("raw path artifact hash differs")
    value = _read_json(path)
    return path, value


def _settling_time_s(samples: Sequence[Mapping[str, Any]]) -> float:
    """First transient time followed by one full second within +/-0.5 N."""

    transient = [
        (float(row["path_time_s"]), abs(float(row["filtered_normal_n"]) - 5.0))
        for row in samples
        if 0.0 <= float(row["path_time_s"]) < 5.0
    ]
    for index, (time_s, _error) in enumerate(transient):
        window = transient[index : index + 10]
        if len(window) == 10 and all(error <= 0.5 for _time, error in window):
            return time_s
    return 5.0


def handoff_evidence_from_result(
    *,
    context: R013LiveContext,
    result: Mapping[str, Any],
    record: Any,
) -> dict[str, Any]:
    """Derive one matched-A/B row from immutable raw/lifecycle evidence."""

    admission = result.get("physical_admission")
    if admission is None or getattr(admission, "admitted_exact", False) is not True:
        raise FigureEightCampaignRunnerError("handoff A/B trial was not trial-admitted")
    metric = getattr(record, "metric_result", None)
    if not isinstance(metric, Mapping) or metric.get("exact") is not True:
        raise FigureEightCampaignRunnerError("handoff A/B trial lacks exact metric")
    lifecycle = result.get("force_lifecycle")
    if not isinstance(lifecycle, Mapping) or lifecycle.get("coverage_complete") is not True:
        raise FigureEightCampaignRunnerError("handoff A/B lifecycle is incomplete")
    lifecycle_path = Path(str(lifecycle.get("artifact_path", "")))
    lifecycle_rows = list(iter_lifecycle_rows(lifecycle_path))
    path_rows = [row for row in lifecycle_rows if int(row.get("phase_code", 0)) == 25]
    if not path_rows:
        raise FigureEightCampaignRunnerError("handoff A/B lifecycle has no PATH rows")
    path_start = float(path_rows[0]["monotonic_s"])
    position_errors: list[float] = []
    orientation_errors: list[float] = []
    for row in path_rows:
        pose = tuple(float(value) for value in row["pose"])
        reference = figure8_runtime_path_reference(
            PATH_STAGE_ID,
            pose[:2],
            max(0.0, float(row["monotonic_s"]) - path_start),
        )
        desired_xy = tuple(float(value) for value in reference["desired_xy"])
        position_errors.append(math.dist(pose[:2], desired_xy))
        orientation_errors.append(math.dist(pose[3:6], FIGURE8_HOME_POSE[3:6]))
    raw_path, raw = _raw_artifact(context, record)
    samples = raw.get("samples")
    if not isinstance(samples, list) or not samples:
        raise FigureEightCampaignRunnerError("handoff A/B raw curve is empty")
    forces = [float(row["filtered_normal_n"]) for row in samples]
    handoff = result.get("handoff")
    if not isinstance(handoff, Mapping):
        raise FigureEightCampaignRunnerError("handoff A/B runtime receipt is missing")
    handoff_receipt_id = _sha256_value(handoff)
    gaps: list[dict[str, Any]] = []
    if int(lifecycle.get("artifact_sample_index_gap_count", 0)):
        gaps.append({
            "kind": "lifecycle_sample_index",
            "count": int(lifecycle["artifact_sample_index_gap_count"]),
        })
    if int(lifecycle.get("packet_sequence_gap_count", 0)):
        gaps.append({
            "kind": "packet_sequence",
            "count": int(lifecycle["packet_sequence_gap_count"]),
        })
    return {
        "sealed": True,
        "sealed_mae_n": float(metric["formal_mae_n"]),
        "gaps": gaps,
        "fmin_n": min(forces),
        "fmax_n": max(forces),
        "pose_error_m": max(position_errors),
        "orientation_error_rad": max(orientation_errors),
        "carry_reset_receipt_ids": [handoff_receipt_id],
        "tmae5_n": float(metric["transient_mae_n"]),
        "settling_time_s": _settling_time_s(samples),
        "raw_evidence_refs": [
            str(raw_path),
            str(lifecycle_path),
            str(lifecycle.get("receipt_path")),
        ],
        "full_curve_mae_n": float(metric["full_curve_mae_n"]),
        "lifecycle_max_gap_s": float(lifecycle.get("max_gap_s", 0.0)),
        "physical_observation_uid": str(getattr(record, "observation_uid", "")),
    }


def _dispatch(plan: TrialPlanV1, *, serial: int, prefix: str) -> SimpleNamespace:
    dispatch_id = f"{prefix}-{serial:06d}-{time.time_ns()}"
    kind = {
        "novel": "BO_TRIAL",
        "sentinel": "SENTINEL",
        "phase_boundary_repeat": "BOUNDARY_CONFIRMATION",
        "terminal_repeat": "TOP3_CONFIRMATION",
        "matched_ab": "HANDOFF_AB",
    }.get(plan.kind, "REPEAT")
    if plan.kind == "novel" and plan.probe_axis is not None:
        kind = "BOUNDARY_PROBE"
    return SimpleNamespace(
        candidate=CompleteCandidateV1.from_mapping(plan.candidate).as_dict(),
        kind=kind,
        dispatch_id=dispatch_id,
        candidate_token=plan.candidate_key,
    )


def execute_handoff_ab(
    *,
    context: R013LiveContext,
    state_path: Path,
) -> HandoffABPlanV1:
    """Run A-B-B-A-A-B plus two winner repeats, all outside BO budget."""

    if Path(state_path).is_file():
        plan = HandoffABPlanV1.from_mapping(_read_json(state_path))
    else:
        plan = HandoffABPlanV1()
    anchor = provisional_anchor_candidate().as_dict()
    serial = len(plan.evidence)
    while not plan.complete:
        arm = plan.ask()
        if arm is None:
            raise FigureEightCampaignRunnerError("handoff A/B stopped before n=5")
        policy = plan.arm_policy[arm]
        if context.configure_handoff is None:
            raise FigureEightCampaignRunnerError("live owner lacks dynamic handoff seam")
        handoff_binding = context.configure_handoff(HandoffPolicy(policy=policy))
        serial += 1
        dispatch = SimpleNamespace(
            candidate=anchor,
            kind="BO_TRIAL",
            dispatch_id=f"figure8-handoff-{serial:02d}-{time.time_ns()}",
            candidate_token=CompleteCandidateV1.from_mapping(anchor).candidate_key,
        )
        result = context.run_trial(dispatch)
        record = context.physical_ledger.records[-1]
        evidence = handoff_evidence_from_result(
            context=context,
            result=result,
            record=record,
        )
        evidence["handoff_binding"] = dict(handoff_binding)
        plan.tell(arm, evidence)
        _atomic_json(state_path, plan.as_dict())
    return plan


def _stop_exact_resident(
    *,
    robot_host: str,
    expected_home_pose: Sequence[float] = FIGURE8_HOME_POSE,
) -> dict[str, Any]:
    """Release only the exact Step6 resident, then prove stationary Home."""

    observed = dict(_observe_dashboard(robot_host))
    _require_remote_normal(observed)
    loaded = _loaded_target(observed)
    running = _dashboard_running(observed)
    if running and loaded != CONTROLLER_TARGET:
        raise FigureEightCampaignRunnerError(
            "refusing to stop a running program outside the Figure-eight lineage"
        )
    if running:
        RemoteDashboardWriter(
            robot_host,
            load_target=CONTROLLER_TARGET,
            timeout_s=3.0,
        ).write("stop")
    dashboard = _wait_dashboard(
        robot_host,
        CONTROLLER_TARGET,
        running=False,
        observer=_observe_dashboard,
        sleeper=time.sleep,
    )
    sample = dict(_state_sample(robot_host))
    pose = tuple(float(value) for value in sample["actual_TCP_pose"])
    speed = tuple(float(value) for value in sample["actual_TCP_speed"])
    position_error = math.dist(pose[:3], tuple(expected_home_pose)[:3])
    orientation_error = math.dist(pose[3:6], tuple(expected_home_pose)[3:6])
    linear_speed = math.sqrt(math.fsum(value * value for value in speed[:3]))
    angular_speed = math.sqrt(math.fsum(value * value for value in speed[3:]))
    passed = bool(
        position_error <= HOME_POSITION_TOLERANCE_M
        and orientation_error <= HOME_ORIENTATION_TOLERANCE_RAD
        and linear_speed <= 0.0005
        and angular_speed <= 0.005
        and sample.get("safety_mode") in (1, "NORMAL")
    )
    if not passed:
        raise FigureEightCampaignRunnerError("released Figure-eight resident is not stationary Home/Safety NORMAL")
    return {
        "schema": "step6.autotune/figure8-writer-release-receipt-v1",
        "passed": True,
        "program": PROGRAM,
        "controller_target": CONTROLLER_TARGET,
        "dashboard": dashboard,
        "actual_pose": list(pose),
        "expected_home_pose": list(tuple(float(value) for value in expected_home_pose)),
        "position_error_m": position_error,
        "orientation_error_rad": orientation_error,
        "linear_speed_m_s": linear_speed,
        "angular_speed_rad_s": angular_speed,
        "safety_mode": sample.get("safety_mode"),
        "writer_released": True,
        "captured_at_s": time.time(),
    }


def _passed_artifact(path: Path, payload: Mapping[str, Any]) -> Path:
    value = {"passed": True, **dict(payload)}
    _atomic_json(path, value)
    return Path(path)


def derive_launch_evidence(
    *,
    root: Path,
    state_root: Path,
    run_dir: Path,
    readback_dir: Path,
    canary_dir: Path,
    fingerprint: FigureEightCampaignFingerprintV1,
    source_sha256: str,
    package_paths: Mapping[str, Path],
    context: R013LiveContext,
) -> FigureEightLaunchReceiptV1:
    """Normalize immutable sources, then invoke the derived-only launch gate."""

    evidence_root = Path(state_root) / "launch_evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    readback_manifest, readback_triplet = verify_controller_readback(
        root=root,
        readback_dir=readback_dir,
    )
    frame_source = _read_json(Path(canary_dir) / "frame_receipt_v2.json")
    canary_source = _read_json(Path(canary_dir) / "evidence_receipt_v2.json")
    frozen_config = load_campaign_config(
        Path(root) / "config" / "step6" / "r013_figure8_direct_campaign_v1.json"
    )
    configured_frame = dict(frozen_config.raw["home_frame"])
    controller = _read_json(Path(run_dir) / "controller_receipt.json")
    expected_eoat = {
        "eoat_identity_sha256": controller["eoat_identity_sha256"],
        "payload_kg": controller["readback"]["payload_kg"],
        "payload_cog_m": controller["readback"]["payload_cog_m"],
        "tcp_offset_m_rad": controller["readback"]["tcp_offset_m_rad"],
    }
    bound_eoat = dict(fingerprint.eoat_tcp_payload)
    if (
        bound_eoat.get("eoat_profile_sha256")
        != expected_eoat["eoat_identity_sha256"]
        or float(bound_eoat.get("payload_kg", float("nan")))
        != float(expected_eoat["payload_kg"])
        or list(bound_eoat.get("payload_cog_m", ()))
        != list(expected_eoat["payload_cog_m"])
        or list(bound_eoat.get("tcp_offset_m_rad", ()))
        != list(expected_eoat["tcp_offset_m_rad"])
    ):
        raise FigureEightCampaignRunnerError(
            "fresh EOAT/TCP/payload read-back differs from the campaign fingerprint"
        )
    if (
        frame_source.get("live_canary_passed") is not True
        or list(frame_source.get("home_pose", ())) != list(fingerprint.home_pose)
        or list(frame_source.get("along_base", ()))
        != list(configured_frame.get("along_xy", ()))
        or list(frame_source.get("lateral_base", ()))
        != list(configured_frame.get("lateral_xy", ()))
        or frame_source.get("frame_sha256")
        != configured_frame.get("source_frame_sha256")
        or _sha256_value(configured_frame) != fingerprint.home_frame_sha256
    ):
        raise FigureEightCampaignRunnerError(
            "frozen no-contact Home/frame differs from the campaign fingerprint"
        )
    home_frame_sha = fingerprint.home_frame_sha256
    controller_identity_sha = _sha256_value({
        "program": PROGRAM,
        "controller_target": CONTROLLER_TARGET,
        "triplet_sha256": readback_triplet,
        "runtime_protocol": controller["runtime_protocol"],
        "runtime_digest_hi": controller["runtime_digest_hi"],
        "runtime_digest_lo": controller["runtime_digest_lo"],
    })
    eoat_tcp_payload_sha = _sha256_value(expected_eoat)
    admission_policy_sha = _sha256_value({
        "schema": "step6.autotune/figure8-trial-admission-v2",
        "trial_admission": "motion_timing_exact_550_bin_seal",
        "rejected_reaches_gp": False,
    })
    evidence_paths = {
        "package_readback": _passed_artifact(
            evidence_root / "package_readback.json",
            {
                "schema": "step6.autotune/figure8-package-readback-evidence-v1",
                "package_triplet_sha256": readback_triplet,
                "source_manifest": str(Path(readback_dir) / "manifest.json"),
                "source_manifest_sha256": _sha256_file(Path(readback_dir) / "manifest.json"),
                "source_state": readback_manifest["state"],
            },
        ),
        "no_contact_canary": _passed_artifact(
            evidence_root / "no_contact_canary.json",
            {
                "schema": "step6.autotune/figure8-no-contact-evidence-v1",
                "contact_detected": False,
                "force_control_executed": canary_source.get("force_control_executed"),
                "source": str(Path(canary_dir) / "evidence_receipt_v2.json"),
                "source_sha256": _sha256_file(Path(canary_dir) / "evidence_receipt_v2.json"),
            },
        ),
        "home_frame": _passed_artifact(
            evidence_root / "home_frame.json",
            {
                "schema": "step6.autotune/figure8-home-frame-evidence-v1",
                "home_frame_sha256": home_frame_sha,
                "source_frame_sha256": frame_source.get("frame_sha256"),
                "source": str(Path(canary_dir) / "frame_receipt_v2.json"),
            },
        ),
        "controller_identity": _passed_artifact(
            evidence_root / "controller_identity.json",
            {
                "schema": "step6.autotune/figure8-controller-identity-evidence-v1",
                "controller_identity_sha256": controller_identity_sha,
                "identity": controller,
            },
        ),
        "source_identity": _passed_artifact(
            evidence_root / "source_identity.json",
            {
                "schema": "step6.autotune/figure8-source-identity-evidence-v1",
                "source_sha256": source_sha256,
            },
        ),
        "eoat_tcp_payload": _passed_artifact(
            evidence_root / "eoat_tcp_payload.json",
            {
                "schema": "step6.autotune/figure8-eoat-tcp-payload-evidence-v1",
                "eoat_tcp_payload_sha256": eoat_tcp_payload_sha,
                "observed": expected_eoat,
                "writes_performed": False,
            },
        ),
        "admission_policy": _passed_artifact(
            evidence_root / "admission_policy.json",
            {
                "schema": "step6.autotune/figure8-admission-policy-evidence-v1",
                "admission_policy_sha256": admission_policy_sha,
                "rejected_reaches_tell_exact": False,
                "required_formal_bins": 550,
            },
        ),
        "final_campaign_fingerprint": _passed_artifact(
            evidence_root / "final_campaign_fingerprint.json",
            {
                "schema": "step6.autotune/figure8-final-fingerprint-evidence-v1",
                "fingerprint_sha256": fingerprint.sha256,
                "fingerprint": fingerprint.as_dict(),
            },
        ),
    }
    receipt = FigureEightLaunchReceiptV1.derive(
        evidence_paths=evidence_paths,
        final_fingerprint=fingerprint,
        package_paths=package_paths,
        expected_source_sha256=source_sha256,
        expected_home_frame_sha256=home_frame_sha,
        expected_controller_identity_sha256=controller_identity_sha,
        expected_eoat_tcp_payload_sha256=eoat_tcp_payload_sha,
        expected_admission_policy_sha256=admission_policy_sha,
    )
    _atomic_json(evidence_root / "figure8_launch_receipt.json", receipt.as_dict())
    return receipt


def _trial_receipt(
    *,
    plan: TrialPlanV1,
    result: Mapping[str, Any],
    record: Any,
    context: R013LiveContext,
    async_overlap: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    raw_path, _raw = _raw_artifact(context, record)
    admission = result["physical_admission"]
    return {
        "schema": TRIAL_RECEIPT_SCHEMA,
        "version": 1,
        "captured_at_s": time.time(),
        "plan": FigureEightSchedulerV1._plan_dict(plan),
        "physical_admission": _safe_json(admission),
        "physical_record": _safe_json(record.payload()),
        "raw_artifact_path": str(raw_path),
        "raw_artifact_sha256": _sha256_file(raw_path),
        "force_lifecycle": _safe_json(result.get("force_lifecycle")),
        "handoff": _safe_json(result.get("handoff")),
        "anti_windup": _safe_json(result.get("anti_windup")),
        "correction_runtime": _safe_json(result.get("runtime_strategy")),
        "safe_return": result.get("safe_return") is True,
        "home": result.get("home") is True,
        "safe_return_async_overlap": (
            None if async_overlap is None else dict(async_overlap)
        ),
    }


def _failure_signature(admission: Any) -> str:
    gates = {
        "trial": getattr(admission, "trial_admission_passed", None),
        "motion": getattr(admission, "motion_gate", None),
        "timing": getattr(admission, "timing_gate", None),
        "sealed": getattr(admission, "sealed", None),
    }
    return "trial_admission_rejection:" + _sha256_value(gates)[:16]


def _update_sentinel_drift(scheduler: FigureEightSchedulerV1) -> bool:
    if scheduler.fixed_sentinel_candidate is None:
        return False
    key = CompleteCandidateV1.from_mapping(
        scheduler.fixed_sentinel_candidate
    ).candidate_key
    values = [
        float(row["mae_n"])
        for row in scheduler.accepted_observations
        if row.get("kind") == "sentinel" and row.get("candidate_key") == key
    ]
    if not values:
        return False
    baseline = values[0]
    pooled_values = [
        float(group["pooled_same_fingerprint_variance_n2"])
        for group in scheduler.observation_groups.values()
        if group.get("pooled_same_fingerprint_variance_n2") is not None
    ]
    pooled_variance = statistics.median(pooled_values) if pooled_values else 1e-4
    threshold = max(0.05, 3.0 * math.sqrt(max(0.0, pooled_variance)))
    exceed = [abs(value - baseline) > threshold for value in values]
    pause = len(exceed) >= 2 and exceed[-1] and exceed[-2]
    scheduler.drift_state = {
        "schema": "step6.autotune/figure8-sentinel-drift-state-v1",
        "fixed_sentinel_candidate_key": key,
        "baseline_mae_n": baseline,
        "values_n": values,
        "pooled_variance_n2": pooled_variance,
        "threshold_n": threshold,
        "last_two_exceed": pause,
        "pause_required": pause,
    }
    scheduler._persist()
    return pause


@dataclass(frozen=True)
class ProductionCampaignInputsV1:
    root: Path
    config_path: Path
    state_root: Path
    controller_readback_dir: Path
    canary_dir: Path
    home_calibration_receipt: Path
    optimizer_python: Path = DEFAULT_OPTIMIZER_PYTHON
    robot_host: str = DEFAULT_ROBOT_HOST
    kunwei_host: str = DEFAULT_KUNWEI_HOST
    kunwei_port: int = DEFAULT_KUNWEI_PORT


@dataclass(frozen=True)
class HomeCalibrationInputsV1:
    root: Path
    config_path: Path
    state_root: Path
    controller_readback_dir: Path
    canary_dir: Path
    robot_host: str = DEFAULT_ROBOT_HOST
    kunwei_host: str = DEFAULT_KUNWEI_HOST
    kunwei_port: int = DEFAULT_KUNWEI_PORT


def execute_figure8_home_calibration_v1(
    inputs: HomeCalibrationInputsV1,
) -> dict[str, Any]:
    """Run exactly the three non-BO contact acquisitions used to derive final Home.

    This is a separate, calibration-only lineage.  It never schedules a BO
    candidate and its fingerprint is never trainable.
    """

    root = Path(inputs.root).resolve()
    state_root = Path(inputs.state_root).resolve()
    if state_root.exists():
        raise FigureEightCampaignRunnerError(
            "Figure-eight Home calibration requires a fresh state directory"
        )
    state_root.mkdir(parents=True, exist_ok=False)
    config = load_campaign_config(Path(inputs.config_path))
    source_sha = str(config.raw["controller"]["source_parent_sha256"])
    require_source_identity(root, source_sha)
    package_paths = {
        role: root / "programs" / "step6" / f"{PROGRAM}.{role}"
        for role in ("script", "txt", "urp")
    }
    triplet = {role: _sha256_file(path) for role, path in package_paths.items()}
    script = package_paths["script"].read_text(encoding="utf-8")
    if (
        "STEP6_HOME_CALIBRATION_RECEIPT_SHA256: pending" not in script
        or "calibration-only Home; campaign launch forbidden" not in script
    ):
        raise FigureEightCampaignRunnerError(
            "Home calibration requires the calibration-only Step6 package"
        )
    pending = build_campaign_fingerprint(
        config=config,
        source_sha256=source_sha,
        controller_triplet_sha256=triplet,
    )
    run_dir = state_root / "home-calibration-resident-001"
    context: R013LiveContext | None = None
    try:
        prepare_figure8_live_run(
            run_dir,
            root=root,
            robot_host=inputs.robot_host,
            kunwei_host=inputs.kunwei_host,
            kunwei_port=inputs.kunwei_port,
            controller_readback_dir=inputs.controller_readback_dir,
            canary_dir=inputs.canary_dir,
            home_calibration_receipt_path=None,
            fingerprint=pending,
            campaign_id="r013-figure8-home-calibration-v1",
            run_id=run_dir.name,
            attempt_id="r006-figure8-home-calibration",
        )
        context = build_r013_live_context(
            run_dir=run_dir,
            controller_host=inputs.robot_host,
            kunwei_host=inputs.kunwei_host,
            kunwei_port=inputs.kunwei_port,
            launch_profile=run_dir / "figure8_launch_profile.json",
            campaign_id="r013-figure8-home-calibration-v1",
            run_id=run_dir.name,
            attempt_id="r006-figure8-home-calibration",
            runtime_strategy=DISABLED_RUNTIME_STRATEGY,
            path_profile=R013PathProfileV1.figure8(),
            path_fingerprint_sha256=pending.sha256,
            handoff_policy=HandoffPolicy(policy="blind_reset_v0"),
        )
        records = tuple(context.physical_ledger.records)
        if (
            len(records) != 3
            or any(record.kind != "QUALIFICATION" for record in records)
            or any(not record.qualification_eligible for record in records)
        ):
            raise FigureEightCampaignRunnerError(
                "Home calibration did not close exactly three sealed non-BO contacts"
            )
    finally:
        close_error: Exception | None = None
        if context is not None:
            try:
                context.close()
            except Exception as exc:
                close_error = exc
        observed = dict(_observe_dashboard(inputs.robot_host))
        if _dashboard_running(observed):
            if _loaded_target(observed) != CONTROLLER_TARGET:
                raise FigureEightCampaignRunnerError(
                    "Home calibration ended while a different program was running"
                )
            release = _stop_exact_resident(robot_host=inputs.robot_host)
            _atomic_json(state_root / "writer_release.json", release)
        if close_error is not None:
            raise FigureEightCampaignRunnerError(
                "Home calibration resource close failed"
            ) from close_error
    receipt = derive_figure8_home_calibration_receipt(
        calibration_run_dir=run_dir
    )
    receipt_path = state_root / "home_calibration_receipt.json"
    _atomic_json(receipt_path, receipt)
    return {
        "schema": "step6.autotune/figure8-home-calibration-result-v1",
        "passed": True,
        "state_root": str(state_root),
        "calibration_run_dir": str(run_dir),
        "home_calibration_receipt": str(receipt_path),
        "home_calibration_receipt_sha256": receipt["receipt_sha256"],
        "contact_confirm_z_m": receipt["contact_confirm_z_m"],
        "final_home_pose": receipt["final_home_pose"],
        "bo_dispatched": False,
        "writer_released": True,
        "next_gate": "rebuild_and_readback_final_package_then_final_no_contact_canary",
    }


class FigureEightProductionCampaignV1:
    """One autonomous, durable campaign with one live writer at a time."""

    def __init__(
        self,
        inputs: ProductionCampaignInputsV1,
        *,
        checkpoint_callback: Callable[[Path, int, Mapping[str, Any]], None] | None = None,
        segment_clock: Callable[[], float] = time.monotonic,
        segment_attempt_limit: int = 320,
        segment_duration_s: float = 10.0 * 60.0 * 60.0,
    ) -> None:
        self.inputs = inputs
        self.root = Path(inputs.root).resolve()
        self.state_root = Path(inputs.state_root).resolve()
        self.config = load_campaign_config(Path(inputs.config_path))
        self.home_materialization_path = Path(
            inputs.home_calibration_receipt
        ).resolve()
        self.home_materialization = load_figure8_home_calibration_receipt(
            self.home_materialization_path
        )
        self.checkpoint_callback = checkpoint_callback
        self.segment_clock = segment_clock
        self.segment_attempt_limit = int(segment_attempt_limit)
        self.segment_duration_s = float(segment_duration_s)
        self.state_path = self.state_root / "production_runner_state.json"
        self.source_sha256 = str(self.config.raw["controller"]["source_parent_sha256"])
        self.source_identity = require_source_identity(
            self.root,
            self.source_sha256,
        )
        self.package_paths = {
            role: self.root / "programs" / "step6" / f"{PROGRAM}.{role}"
            for role in ("script", "txt", "urp")
        }
        self.triplet = {role: _sha256_file(path) for role, path in self.package_paths.items()}
        optimizer_launcher = Path(
            os.path.abspath(os.fspath(inputs.optimizer_python))
        )
        optimizer_target = optimizer_launcher.resolve(strict=True)
        optimizer_pyvenv = optimizer_launcher.parent.parent / "pyvenv.cfg"
        if optimizer_pyvenv.is_symlink() or not optimizer_pyvenv.is_file():
            raise FigureEightCampaignRunnerError(
                "optimizer launcher is not bound to a regular pyvenv.cfg"
            )
        self.runner_input_identity = {
            "schema": "step6.autotune/figure8-runner-input-identity-v1",
            "version": 1,
            "root": str(self.root),
            "config_path": str(Path(inputs.config_path).resolve()),
            "config_sha256": _sha256_file(Path(inputs.config_path)),
            "source_sha256": self.source_sha256,
            "source_manifest_sha256": _sha256_value(self.source_identity),
            "controller_triplet_sha256": dict(self.triplet),
            "controller_readback_manifest": str(
                Path(inputs.controller_readback_dir).resolve() / "manifest.json"
            ),
            "controller_readback_manifest_sha256": _sha256_file(
                Path(inputs.controller_readback_dir) / "manifest.json"
            ),
            "canary_evidence_sha256": _sha256_file(
                Path(inputs.canary_dir) / "evidence_receipt_v2.json"
            ),
            "canary_frame_sha256": _sha256_file(
                Path(inputs.canary_dir) / "frame_receipt_v2.json"
            ),
            "canary_raw_trace_sha256": _sha256_file(
                Path(inputs.canary_dir) / "raw_rtde_trace.jsonl"
            ),
            "home_calibration_receipt": str(self.home_materialization_path),
            "home_calibration_receipt_sha256": _sha256_file(
                self.home_materialization_path
            ),
            "home_calibration_evidence_sha256": str(
                self.home_materialization["receipt_sha256"]
            ),
            "optimizer_python_launcher": str(optimizer_launcher),
            "optimizer_python_resolved_target": str(optimizer_target),
            "optimizer_python_target_sha256": _sha256_file(optimizer_target),
            "optimizer_pyvenv_cfg_sha256": _sha256_file(optimizer_pyvenv),
            "robot_host": str(inputs.robot_host),
            "kunwei_host": str(inputs.kunwei_host),
            "kunwei_port": int(inputs.kunwei_port),
        }
        self.runner_input_identity_sha256 = _sha256_value(
            self.runner_input_identity
        )
        self._trial_serial = 0

    def _require_resume_identity(self, state: Mapping[str, Any]) -> None:
        if (
            state.get("runner_input_identity_sha256")
            != self.runner_input_identity_sha256
            or state.get("runner_input_identity") != self.runner_input_identity
        ):
            raise FigureEightCampaignRunnerError(
                "resume source/package/readback/canary/runtime identity differs; "
                "start a new fingerprint/state root"
            )

    def _release_interrupted_exact_resident(self) -> None:
        """On resume, release only our exact resident; never touch another program."""

        observed = dict(_observe_dashboard(self.inputs.robot_host))
        _require_remote_normal(observed)
        if not _dashboard_running(observed):
            return
        if _loaded_target(observed) != CONTROLLER_TARGET:
            raise FigureEightCampaignRunnerError(
                "resume found a different running controller program"
            )
        receipt = _stop_exact_resident(
            robot_host=self.inputs.robot_host,
            expected_home_pose=tuple(
                float(value)
                for value in self.home_materialization["final_home_pose"]
            ),
        )
        _atomic_json(
            self.state_root / f"resume_writer_release_{time.time_ns()}.json",
            receipt,
        )

    def _write_state(self, **updates: Any) -> None:
        previous = _read_json(self.state_path) if self.state_path.is_file() else {}
        value = {
            "schema": RUNNER_SCHEMA,
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **previous,
            **updates,
        }
        _atomic_json(self.state_path, value)

    def _censor_closure(self, context: R013LiveContext) -> Mapping[str, Any]:
        """Read fresh return/Home closure from the existing writer output."""

        output = getattr(context.writer, "_last_output", None)
        echoes = getattr(output, "integer_echoes", {}) if output is not None else {}

        def echo(index: int, default: int = -1) -> int:
            try:
                return int(echoes[index])
            except (KeyError, IndexError, TypeError, ValueError):
                return default

        home_binding = getattr(getattr(context.writer, "injection", None), "home_binding", None)
        profile = getattr(home_binding, "profile", None)
        pose = tuple(getattr(profile, "pose", ()))
        calibration_sha = getattr(
            profile, "home_calibration_receipt_sha256", None
        )
        calibrated = bool(
            getattr(profile, "home_profile_id", None)
            == FIGURE8_FINAL_HOME_PROFILE_ID
            and calibration_sha == self.home_materialization["receipt_sha256"]
            and tuple(self.home_materialization["final_home_pose"]) == pose
        )
        fresh = dict(_state_sample(self.inputs.robot_host))
        actual_pose = tuple(float(value) for value in fresh.get("actual_TCP_pose", ()))
        actual_speed = tuple(float(value) for value in fresh.get("actual_TCP_speed", ()))
        position_error = (
            math.dist(actual_pose[:3], pose[:3])
            if len(actual_pose) == len(pose) == 6
            else math.inf
        )
        orientation_error = (
            math.dist(actual_pose[3:], pose[3:])
            if len(actual_pose) == len(pose) == 6
            else math.inf
        )
        linear_speed = (
            math.sqrt(math.fsum(value * value for value in actual_speed[:3]))
            if len(actual_speed) == 6
            else math.inf
        )
        angular_speed = (
            math.sqrt(math.fsum(value * value for value in actual_speed[3:]))
            if len(actual_speed) == 6
            else math.inf
        )
        safety_normal = fresh.get("safety_mode") in (1, "NORMAL")
        stationary = linear_speed <= 0.0005 and angular_speed <= 0.005
        pose_closed = position_error <= 0.001 and orientation_error <= 0.010
        home_state = echo(26)
        return {
            "return_guard_value": echo(31),
            "return_guard_closed": echo(31) == 123,
            "home_closed": home_state == 78 and pose_closed,
            "safe_return_closed": home_state == 78 and pose_closed and stationary and safety_normal,
            "home_calibrated": calibrated,
            "home_profile_id": str(getattr(profile, "home_profile_id", "")),
            "home_calibration_receipt_sha256": str(calibration_sha or ""),
            "home_observation": {
                "fresh": True,
                "observed_at_s": float(fresh.get("observed_at_s", 0.0)),
                "actual_pose": list(actual_pose),
                "expected_pose": list(pose),
                "actual_speed": list(actual_speed),
                "position_error_m": position_error,
                "orientation_error_rad": orientation_error,
                "linear_speed_m_s": linear_speed,
                "angular_speed_rad_s": angular_speed,
                "safety_mode": fresh.get("safety_mode"),
                "safety_normal": safety_normal,
                "stationary": stationary,
                "pose_closed": pose_closed,
            },
        }

    def _censor_seam(self, context: R013LiveContext) -> FigureEightPhysicalCensorSeamV1:
        return FigureEightPhysicalCensorSeamV1(
            sink_owner=context.writer,
            register_writer=context.register_writer,
            closure_provider=lambda: self._censor_closure(context),
        )

    def _prepare_context(
        self,
        *,
        directory_name: str,
        fingerprint: FigureEightCampaignFingerprintV1,
        handoff_policy: str,
    ) -> tuple[Path, R013LiveContext]:
        run_dir = self.state_root / directory_name
        campaign_id = "r013-figure8-direct-campaign-v1"
        run_id = directory_name
        attempt_id = f"r006-{directory_name}"
        try:
            prepare_figure8_live_run(
                run_dir,
                root=self.root,
                robot_host=self.inputs.robot_host,
                kunwei_host=self.inputs.kunwei_host,
                kunwei_port=self.inputs.kunwei_port,
                controller_readback_dir=self.inputs.controller_readback_dir,
                canary_dir=self.inputs.canary_dir,
                home_calibration_receipt_path=self.home_materialization_path,
                fingerprint=fingerprint,
                campaign_id=campaign_id,
                run_id=run_id,
                attempt_id=attempt_id,
            )
            context = build_r013_live_context(
                run_dir=run_dir,
                controller_host=self.inputs.robot_host,
                kunwei_host=self.inputs.kunwei_host,
                kunwei_port=self.inputs.kunwei_port,
                launch_profile=run_dir / "figure8_launch_profile.json",
                campaign_id=campaign_id,
                run_id=run_id,
                attempt_id=attempt_id,
                runtime_strategy=DISABLED_RUNTIME_STRATEGY,
                path_profile=R013PathProfileV1.figure8(),
                path_fingerprint_sha256=fingerprint.sha256,
                handoff_policy=HandoffPolicy(policy=handoff_policy),
                run_epoch_qualifications=False,
            )
            return run_dir, context
        except Exception as build_error:
            # Lower layers perform typed cleanup.  This final observation
            # closes the only remaining ambiguity: an exact Step6 resident
            # must not survive failed composition.
            observed = dict(_observe_dashboard(self.inputs.robot_host))
            if _dashboard_running(observed):
                if _loaded_target(observed) != CONTROLLER_TARGET:
                    raise FigureEightCampaignRunnerError(
                        "context build failed while a different program became the live writer"
                    ) from build_error
                try:
                    receipt = _stop_exact_resident(
                        robot_host=self.inputs.robot_host,
                        expected_home_pose=tuple(
                            float(value)
                            for value in self.home_materialization["final_home_pose"]
                        ),
                    )
                    _atomic_json(
                        self.state_root
                        / f"{directory_name}_failed_build_writer_release.json",
                        receipt,
                    )
                except Exception as release_error:
                    raise FigureEightCampaignRunnerError(
                        f"context build failed ({build_error}); exact resident release also failed"
                    ) from release_error
            raise

    def _close_context(self, context: R013LiveContext, *, receipt_name: str) -> dict[str, Any]:
        close_error: Exception | None = None
        try:
            context.close()
        except Exception as exc:
            close_error = exc
        receipt = _stop_exact_resident(
            robot_host=self.inputs.robot_host,
            expected_home_pose=tuple(
                float(value)
                for value in self.home_materialization["final_home_pose"]
            ),
        )
        _atomic_json(self.state_root / receipt_name, receipt)
        if close_error is not None:
            raise FigureEightCampaignRunnerError(
                "live context resource close failed after the exact resident was released"
            ) from close_error
        return receipt

    def _materialize_handoff(self) -> tuple[FigureEightCampaignFingerprintV1, float]:
        pending = build_campaign_fingerprint(
            config=self.config,
            source_sha256=self.source_sha256,
            controller_triplet_sha256=self.triplet,
            home_materialization=self.home_materialization,
        )
        handoff_state_path = self.state_root / "handoff_ab_plan.json"
        plan = (
            HandoffABPlanV1.from_mapping(_read_json(handoff_state_path))
            if handoff_state_path.is_file()
            else HandoffABPlanV1()
        )
        if not plan.complete:
            handoff_index = 1
            while (self.state_root / f"handoff-resident-{handoff_index:03d}").exists():
                handoff_index += 1
            handoff_dir, context = self._prepare_context(
                directory_name=f"handoff-resident-{handoff_index:03d}",
                fingerprint=pending,
                handoff_policy="blind_reset_v0",
            )
            try:
                plan = execute_handoff_ab(
                    context=context,
                    state_path=handoff_state_path,
                )
            finally:
                self._close_context(
                    context,
                    receipt_name=f"handoff_writer_release_{handoff_index:03d}.json",
                )
        policy = plan.materialize_handoff_policy()
        selection = plan.selection_receipt()
        if selection is None:
            raise FigureEightCampaignRunnerError("handoff A/B selection receipt is missing")
        arm_fingerprints = {
            arm: _sha256_value({
                "pending_fingerprint_sha256": pending.sha256,
                "arm": arm,
                "policy": plan.arm_policy[arm],
                "selection_evidence_sha256": selection.evidence_sha256,
            })
            for arm in ("A", "B")
        }
        winner_arm = "A" if policy == "blind_reset_v0" else "B"
        matched_ab = {
            "schema": "step6.autotune/figure8-matched-handoff-ab-v1",
            "version": 1,
            "winner": True,
            "winner_handoff_policy": policy,
            "winner_n": plan.selected_repeat_count,
            "same_function_fingerprint": True,
            "arm_fingerprints": arm_fingerprints,
            "winner_fingerprint_sha256": arm_fingerprints[winner_arm],
            "selection_receipt": selection.as_dict(),
            "source_run_dirs": sorted({
                str(Path(str(row["raw_evidence_refs"][0])).resolve().parents[1])
                for row in plan.evidence
            }),
        }
        matched_path = self.state_root / "matched_handoff_ab.json"
        if matched_path.is_file():
            if _read_json(matched_path) != matched_ab:
                raise FigureEightCampaignRunnerError(
                    "resumed matched handoff A/B artifact differs"
                )
        else:
            _atomic_json(matched_path, matched_ab)
        frozen = build_frozen_campaign_fingerprint(
            config=self.config,
            source_sha256=self.source_sha256,
            controller_triplet_sha256=self.triplet,
            matched_ab=matched_ab,
            home_materialization=self.home_materialization,
        )
        _atomic_json(self.state_root / "frozen_campaign_fingerprint.json", {
            "passed": True,
            "fingerprint_sha256": frozen.sha256,
            "fingerprint": frozen.as_dict(),
            "matched_ab_sha256": _sha256_file(matched_path),
        })
        winner_rows = [row for row in plan.evidence if row["policy"] == policy]
        anchor_mean = math.fsum(float(row["sealed_mae_n"]) for row in winner_rows) / len(winner_rows)
        self._write_state(
            stage="handoff_complete",
            handoff_policy=policy,
            anchor_mean_n=anchor_mean,
            frozen_campaign_fingerprint_sha256=frozen.sha256,
        )
        return frozen, anchor_mean

    def _checkpoint(
        self,
        *,
        scheduler: FigureEightSchedulerV1,
        novel: int,
        final: bool,
    ) -> None:
        payload = {
            "schema": "step6.autotune/figure8-campaign-checkpoint-v1",
            "version": 1,
            "novel_count": scheduler.novel_count,
            "requested_checkpoint": novel,
            "final": bool(final),
            "single_minimum_n": min(
                (float(row["mae_n"]) for row in scheduler.accepted_observations),
                default=None,
            ),
            "top_candidates": list(scheduler.top_candidates),
            "observation_group_count": len(scheduler.observation_groups),
            "accepted_physical_observation_count": len(scheduler.accepted_observations),
            "sentinels": list(scheduler.sentinels),
            "drift_state": dict(scheduler.drift_state),
            "convergence_check_count": len(scheduler.convergence_checks),
            "scheduler_state": str(scheduler.state_path),
            "captured_at_s": time.time(),
        }
        path = self.state_root / "checkpoints" / f"novel-{novel:03d}.json"
        _atomic_json(path, payload)

    def _segment_checkpoint(
        self,
        *,
        scheduler: FigureEightSchedulerV1,
        segment: int,
        reason: str,
        budget: OperationalSegmentBudgetV1,
        release_receipt: Mapping[str, Any],
    ) -> Path:
        payload = {
            "schema": "step6.autotune/figure8-safe-segment-release-receipt-v1",
            "version": 1,
            "segment": segment,
            "reason": reason,
            "safe_between_trial_boundary": scheduler.in_flight is None,
            "campaign_complete": False,
            "campaign_fingerprint_sha256": scheduler.campaign_fingerprint_sha256,
            "novel_count": scheduler.novel_count,
            "exact_novel_count": scheduler.exact_novel_count,
            "novel_dispatch_count": scheduler.novel_dispatch_count,
            "sobol_cursor": scheduler.cursor.snapshot(),
            "segment_budget": budget.snapshot(),
            "writer_release": dict(release_receipt),
            "captured_at_s": time.time(),
        }
        path = self.state_root / "segment_checkpoints" / f"segment-{segment:03d}.json"
        _atomic_json(path, payload)
        return path

    def _reconcile_interrupted_plan(
        self,
        scheduler: FigureEightSchedulerV1,
        *,
        fingerprint: FigureEightCampaignFingerprintV1,
    ) -> None:
        """Cold-reconcile an in-flight plan without inventing an observation."""

        plan = scheduler.in_flight
        if plan is None:
            return
        exact_receipt: dict[str, Any] | None = None
        for path in sorted((self.state_root / "trial_receipts").glob("trial-*.json")):
            receipt = _read_json(path)
            receipt_plan = receipt.get("plan")
            if not isinstance(receipt_plan, Mapping):
                continue
            receipt_candidate = receipt_plan.get("candidate")
            if not isinstance(receipt_candidate, Mapping):
                continue
            if (
                CompleteCandidateV1.from_mapping(receipt_candidate).candidate_key
                == plan.candidate_key
                and int(receipt_plan.get("novel_ordinal", -1)) == plan.novel_ordinal
                and receipt_plan.get("kind") == plan.kind
            ):
                exact_receipt = receipt
        if exact_receipt is None:
            scheduler.complete(
                plan,
                accepted=False,
                failure_signature="resume_interrupted_without_atomic_exact_receipt",
            )
            self._write_state(
                recovery="in_flight_rejected_no_atomic_exact_receipt",
                recovery_candidate_key=plan.candidate_key,
            )
            return
        admission = PhysicalAdmissionReceipt.from_mapping(
            exact_receipt["physical_admission"]
        )
        accepted = admission.admitted_exact
        scheduler.complete(
            plan,
            accepted=accepted,
            mae_n=admission.sealed_mae_n if accepted else None,
            failure_signature=None if accepted else _failure_signature(admission),
            fingerprint_sha256=fingerprint.sha256 if accepted else None,
        )
        self._write_state(
            recovery=(
                "in_flight_admitted_from_atomic_exact_receipt"
                if accepted
                else "in_flight_rejected_from_atomic_receipt"
            ),
            recovery_candidate_key=plan.candidate_key,
        )

    def _execute_loop(
        self,
        *,
        fingerprint: FigureEightCampaignFingerprintV1,
        handoff_policy: str,
        anchor_mean_n: float,
    ) -> FigureEightSchedulerV1:
        optimizer = SubprocessFigureEightProposalProviderV1(
            optimizer_python=self.inputs.optimizer_python,
            state_dir=self.state_root / "optimizer",
        )
        cursor = PersistedSobolCursorV1(self.state_root / "sobol_cursor.json")
        scheduler = FigureEightSchedulerV1(
            cursor,
            state_path=self.state_root / "scheduler_state.json",
            proposal_provider=optimizer,
            campaign_fingerprint_sha256=fingerprint.sha256,
            fixed_sentinel_candidate=provisional_anchor_candidate().as_dict(),
        )
        self._reconcile_interrupted_plan(
            scheduler,
            fingerprint=fingerprint,
        )
        overlap = SafeReturnProposalOverlapV1(
            scheduler=scheduler,
            artifact_dir=self.state_root / "safe_return_overlap",
        )
        checkpointed = {
            value
            for value in (24, 80, 120, 160, EXACT_NOVEL_TARGET)
            if (self.state_root / "checkpoints" / f"novel-{value:03d}.json").is_file()
        }
        segment = 1
        while (self.state_root / f"campaign-segment-{segment:03d}").exists():
            segment += 1
        run_dir, context = self._prepare_context(
            directory_name=f"campaign-segment-{segment:03d}",
            fingerprint=fingerprint,
            handoff_policy=handoff_policy,
        )
        censor_seam = self._censor_seam(context)
        segment_budget = OperationalSegmentBudgetV1(
            max_attempts=self.segment_attempt_limit,
            max_duration_s=self.segment_duration_s,
            now=self.segment_clock,
        )
        segment_budget.start()
        segment_budget.attempts = 0
        try:
            if not (self.state_root / "launch_evidence" / "figure8_launch_receipt.json").is_file():
                derive_launch_evidence(
                    root=self.root,
                    state_root=self.state_root,
                    run_dir=run_dir,
                    readback_dir=self.inputs.controller_readback_dir,
                    canary_dir=self.inputs.canary_dir,
                    fingerprint=fingerprint,
                    source_sha256=self.source_sha256,
                    package_paths=self.package_paths,
                    context=context,
                )
            while True:
                terminal_repeat_due = bool(
                    scheduler.novel_count >= EXACT_NOVEL_TARGET
                )
                if terminal_repeat_due:
                    scheduler.schedule_terminal_repeats()
                if scheduler.should_stop():
                    break
                if segment_budget.due():
                    if scheduler.in_flight is not None:
                        raise FigureEightCampaignRunnerError(
                            "segment limit reached while a physical plan is in flight"
                        )
                    censor_seam.close()
                    release = self._close_context(
                        context,
                        receipt_name=f"segment-{segment:03d}-writer-release.json",
                    )
                    self._segment_checkpoint(
                        scheduler=scheduler,
                        segment=segment,
                        reason="operational_segment_limit",
                        budget=segment_budget,
                        release_receipt=release,
                    )
                    segment += 1
                    run_dir, context = self._prepare_context(
                        directory_name=f"campaign-segment-{segment:03d}",
                        fingerprint=fingerprint,
                        handoff_policy=handoff_policy,
                    )
                    censor_seam = self._censor_seam(context)
                    segment_budget.start()
                    segment_budget.attempts = 0
                    continue
                plan = scheduler.ask()
                if plan is None:
                    scheduler.schedule_terminal_repeats()
                    plan = scheduler.ask()
                    if plan is None:
                        if scheduler.should_stop():
                            break
                        raise FigureEightCampaignRunnerError(
                            "scheduler exhausted novel budget before top-three n=5"
                        )
                self._trial_serial += 1
                segment_budget.record_attempt()
                if (
                    plan.kind == "novel"
                    and plan.novel_ordinal > 100
                    and plan.acquisition in {"qlognei", "qlognei_local"}
                ):
                    scheduler.record_convergence_proposal_batch(
                        optimizer.last_scored_pool
                    )
                dispatch = _dispatch(
                    plan,
                    serial=self._trial_serial,
                    prefix="figure8-campaign",
                )
                incumbent = (
                    scheduler.confirmed_incumbent_for_censor()
                    if plan.kind == "novel"
                    else None
                )
                attempt_sequence = next_cold_attempt_sequence(
                    context.physical_ledger,
                    self.state_root / "censored_receipts.jsonl",
                    run_id=run_dir.name,
                )
                censor_seam.arm(
                    candidate=dispatch.candidate,
                    campaign_fingerprint_sha256=fingerprint.sha256,
                    campaign_id="r013-figure8-direct-campaign-v1",
                    run_id=run_dir.name,
                    attempt_id=f"{run_dir.name}-attempt-{attempt_sequence}",
                    trial_id=str(dispatch.dispatch_id),
                    run_kind=str(dispatch.kind),
                    confirmed_incumbent_mean_n=None if incumbent is None else incumbent[0],
                    attempt_sequence=attempt_sequence,
                )
                overlap.arm(context=context, plan=plan)
                try:
                    result = context.run_trial(dispatch)
                except Exception as exc:
                    overlap_receipt = overlap.finish_after_home(context=context)
                    try:
                        censored = censor_seam.seal_short_exception(exc)
                    except Exception:
                        scheduler.complete(
                            plan,
                            accepted=False,
                            failure_signature=f"execution_exception:{type(exc).__name__}",
                        )
                        self._write_state(
                            stage="paused_live_exception",
                            novel_count=scheduler.novel_count,
                            novel_dispatch_count=scheduler.novel_dispatch_count,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        raise
                    if censored is not None:
                        runtime = censor_seam.runtime
                        if runtime is None:
                            raise FigureEightCensorRuntimeError(
                                "Figure-eight censored closure lost its raw prefix runtime"
                            )
                        raw_prefix_path = (
                            self.state_root
                            / "censored_raw"
                            / f"censored-{self._trial_serial:06d}.json"
                        )
                        _atomic_json(
                            raw_prefix_path,
                            {
                                "schema": "step6.autotune/figure8-censored-raw-prefix-v1",
                                "version": 1,
                                "candidate_key": censored.candidate_key,
                                "campaign_fingerprint_sha256": fingerprint.sha256,
                                "trial_id": censored.trial_id,
                                "watermark_s": censored.watermark_s,
                                "closed_bin_count": censored.closed_bin_count,
                                "samples": [_safe_json(row) for row in runtime.raw_samples],
                                "raw_gaps": [dict(item) for item in censored.raw_gaps],
                            },
                        )
                        trial_path = (
                            self.state_root
                            / "trial_receipts"
                            / f"trial-{self._trial_serial:06d}-{plan.kind}.json"
                        )
                        _atomic_json(
                            trial_path,
                            {
                                "schema": TRIAL_RECEIPT_SCHEMA,
                                "version": 1,
                                "captured_at_s": time.time(),
                                "dispatch_id": str(dispatch.dispatch_id),
                                "plan": FigureEightSchedulerV1._plan_dict(plan),
                                "state": "HOME",
                                "censored": True,
                                "censored_receipt": censored.as_dict(),
                                "raw_prefix_artifact_path": str(raw_prefix_path),
                                "raw_prefix_artifact_sha256": _sha256_file(
                                    raw_prefix_path
                                ),
                                "safe_return_async_overlap": overlap_receipt,
                            },
                        )
                        with (self.state_root / "censored_receipts.jsonl").open("a", encoding="utf-8") as stream:
                            stream.write(json.dumps({"censored_receipt": censored.as_dict()}, sort_keys=True) + "\n")
                        scheduler.complete_censored(plan, censored)
                        if plan.kind == "novel":
                            scheduler.schedule_sentinel_repeats(
                                optimizer.last_observed_posterior_probability
                            )
                        self._write_state(
                            stage="campaign_running",
                            novel_count=scheduler.novel_count,
                            novel_dispatch_count=scheduler.novel_dispatch_count,
                            accepted_observation_count=len(scheduler.accepted_observations),
                            current_segment=str(run_dir),
                            top_candidates=list(scheduler.top_candidates),
                        )
                        continue
                    scheduler.complete(
                        plan,
                        accepted=False,
                        failure_signature=f"execution_exception:{type(exc).__name__}",
                    )
                    self._write_state(
                        stage="paused_live_exception",
                        novel_count=scheduler.novel_count,
                        novel_dispatch_count=scheduler.novel_dispatch_count,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    # Unknown execution exceptions are not automatically
                    # retried: the owner may have latched a protective/hard
                    # guard.  Evidence and ownership are released below.
                    raise
                overlap_receipt = overlap.finish_after_home(context=context)
                if censor_seam.runtime is not None and censor_seam.runtime.requested:
                    raise FigureEightCensorRuntimeError(
                        "Figure-eight active censor requested early end but run_trial returned without a short gate"
                    )
                record = context.physical_ledger.records[-1]
                receipt = _trial_receipt(
                    plan=plan,
                    result=result,
                    record=record,
                    context=context,
                    async_overlap=overlap_receipt,
                )
                trial_path = (
                    self.state_root
                    / "trial_receipts"
                    / f"trial-{self._trial_serial:06d}-{plan.kind}.json"
                )
                _atomic_json(trial_path, receipt)
                admission = result["physical_admission"]
                accepted = bool(getattr(admission, "admitted_exact", False))
                scheduler.complete(
                    plan,
                    accepted=accepted,
                    mae_n=float(admission.sealed_mae_n) if accepted else None,
                    failure_signature=None if accepted else _failure_signature(admission),
                    fingerprint_sha256=fingerprint.sha256 if accepted else None,
                )
                if scheduler.failure_pause_required:
                    raise FigureEightCampaignRunnerError(
                        "three consecutive trials share one failure signature"
                    )
                if accepted and plan.probe_axis is not None:
                    if len(scheduler.probe_results.get(plan.probe_axis, ())) >= 3:
                        scheduler.open_probe_bound(
                            plan.probe_axis,
                            handoff_anchor_mean_n=anchor_mean_n,
                            strict_gates=True,
                        )
                if accepted and plan.acquisition in {"qlognei", "qlognei_local"}:
                    scheduler.schedule_top_repeats(
                        optimizer.last_observed_posterior_probability
                    )
                if plan.kind == "novel":
                    scheduler.schedule_sentinel_repeats(
                        optimizer.last_observed_posterior_probability
                    )
                if (
                    accepted
                    and scheduler.novel_count == 100
                    and scheduler.frozen_controller_path is None
                ):
                    # Phase C must optimize around a repeated controller
                    # winner, never a lucky single observation.
                    scheduler.schedule_controller_freeze_repeats()
                if (
                    accepted
                    and plan.kind == "phase_boundary_repeat"
                    and scheduler.novel_count == 100
                    and scheduler.frozen_controller_path is None
                ):
                    scheduler.schedule_controller_freeze_repeats()
                if accepted and plan.kind == "sentinel" and _update_sentinel_drift(scheduler):
                    raise FigureEightCampaignRunnerError(
                        "two consecutive fixed sentinels exceed the drift threshold"
                    )
                for marker in (24, 80, 120, 160, EXACT_NOVEL_TARGET):
                    if scheduler.novel_count >= marker and marker not in checkpointed:
                        self._checkpoint(
                            scheduler=scheduler,
                            novel=marker,
                            final=False,
                        )
                        checkpointed.add(marker)
                if (
                    scheduler.novel_count == 80
                    and 80 not in checkpointed
                ):
                    self._checkpoint(scheduler=scheduler, novel=80, final=False)
                    checkpointed.add(80)
                if scheduler.novel_count == 80 and 80 in checkpointed and 81_000 not in checkpointed:
                    # A checkpoint boundary is a real writer-release boundary.
                    censor_seam.close()
                    self._close_context(
                        context,
                        receipt_name="checkpoint_080_writer_release.json",
                    )
                    context = None  # type: ignore[assignment]
                    if self.checkpoint_callback is not None:
                        self.checkpoint_callback(
                            self.state_root,
                            80,
                            _read_json(
                                self.state_root
                                / "checkpoints"
                                / "novel-080.json"
                            ),
                        )
                    segment += 1
                    run_dir, context = self._prepare_context(
                        directory_name=f"campaign-segment-{segment:03d}",
                        fingerprint=fingerprint,
                        handoff_policy=handoff_policy,
                    )
                    censor_seam = self._censor_seam(context)
                    segment_budget.start()
                    segment_budget.attempts = 0
                    # Avoid releasing/restarting again while novel_count stays
                    # 80 for queued repeats after the boundary.
                    checkpointed.add(81_000)
                self._write_state(
                    stage="campaign_running",
                    novel_count=scheduler.novel_count,
                    novel_dispatch_count=scheduler.novel_dispatch_count,
                    accepted_observation_count=len(scheduler.accepted_observations),
                    current_segment=str(run_dir),
                    top_candidates=list(scheduler.top_candidates),
                )
        finally:
            overlap.close()
            if context is not None:
                censor_seam.close()
                self._close_context(context, receipt_name="final_writer_release.json")
        final_release_path = self.state_root / "final_writer_release.json"
        final_release = _read_json(final_release_path)
        if (
            final_release.get("passed") is not True
            or final_release.get("writer_released") is not True
            or tuple(final_release.get("expected_home_pose", ()))
            != tuple(self.home_materialization["final_home_pose"])
            or final_release.get("safety_mode") not in (1, "NORMAL")
        ):
            raise FigureEightCampaignRunnerError(
                "final writer release is not cold-bound to calibrated Home/Safety NORMAL"
            )
        if scheduler.repeat_queue or not scheduler.should_stop():
            raise FigureEightCampaignRunnerError(
                "campaign exited without terminal top-three n=5 stop condition"
            )
        self._checkpoint(
            scheduler=scheduler,
            novel=scheduler.novel_count,
            final=True,
        )
        if self.checkpoint_callback is not None:
            self.checkpoint_callback(
                self.state_root,
                scheduler.novel_count,
                _read_json(
                    self.state_root
                    / "checkpoints"
                    / f"novel-{scheduler.novel_count:03d}.json"
                ),
            )
        self._write_state(
            stage="campaign_complete",
            novel_count=scheduler.novel_count,
            top_candidates=list(scheduler.top_candidates),
            final_writer_released=True,
            final_writer_release_path=str(final_release_path),
            final_writer_release_sha256=_sha256_file(final_release_path),
        )
        return scheduler

    def run(self) -> dict[str, Any]:
        if self.state_root.exists():
            state = _read_json(self.state_path)
            if state.get("schema") != RUNNER_SCHEMA:
                raise FigureEightCampaignRunnerError(
                    "resume state root is not the Figure-eight production campaign"
                )
            self._require_resume_identity(state)
            if state.get("stage") == "campaign_complete":
                final_release_path = Path(
                    str(state.get("final_writer_release_path", ""))
                ).resolve()
                if (
                    not final_release_path.is_file()
                    or _sha256_file(final_release_path)
                    != state.get("final_writer_release_sha256")
                ):
                    raise FigureEightCampaignRunnerError(
                        "completed campaign final writer-release evidence differs"
                    )
                final_release = _read_json(final_release_path)
                if (
                    final_release.get("passed") is not True
                    or tuple(final_release.get("expected_home_pose", ()))
                    != tuple(self.home_materialization["final_home_pose"])
                    or final_release.get("safety_mode") not in (1, "NORMAL")
                ):
                    raise FigureEightCampaignRunnerError(
                        "completed campaign is not cold-bound to calibrated Home/Safety NORMAL"
                    )
                scheduler = FigureEightSchedulerV1(
                    PersistedSobolCursorV1(self.state_root / "sobol_cursor.json"),
                    state_path=self.state_root / "scheduler_state.json",
                    campaign_fingerprint_sha256=str(
                        state["frozen_campaign_fingerprint_sha256"]
                    ),
                    fixed_sentinel_candidate=provisional_anchor_candidate().as_dict(),
                )
                return {
                    "schema": "step6.autotune/figure8-production-campaign-result-v1",
                    "passed": True,
                    "state_root": str(self.state_root),
                    "campaign_fingerprint_sha256": state[
                        "frozen_campaign_fingerprint_sha256"
                    ],
                    "handoff_policy": state["handoff_policy"],
                    "novel_count": scheduler.novel_count,
                    "top_candidates": list(scheduler.top_candidates),
                    "single_minimum_n": min(
                        float(row["mae_n"])
                        for row in scheduler.accepted_observations
                    ),
                    "writer_released": True,
                    "final_safety_normal_home": True,
                    "final_writer_release": str(final_release_path),
                    "resumed_completed_campaign": True,
                }
            self._release_interrupted_exact_resident()
            self._trial_serial = len(list((self.state_root / "trial_receipts").glob("trial-*.json")))
        else:
            self.state_root.mkdir(parents=True, exist_ok=False)
            self._write_state(
                stage="created",
                root=str(self.root),
                config=str(self.inputs.config_path),
                runner_input_identity=self.runner_input_identity,
                runner_input_identity_sha256=self.runner_input_identity_sha256,
                min_novel=80,
                max_novel=EXACT_NOVEL_TARGET,
                exact_novel_target=EXACT_NOVEL_TARGET,
                campaign_target_stop=False,
                trial_censor_active=True,
            )
            _atomic_json(
                self.state_root / "source_identity_manifest.json",
                self.source_identity,
            )
        fingerprint, anchor_mean = self._materialize_handoff()
        scheduler = self._execute_loop(
            fingerprint=fingerprint,
            handoff_policy=fingerprint.handoff_policy,
            anchor_mean_n=anchor_mean,
        )
        return {
            "schema": "step6.autotune/figure8-production-campaign-result-v1",
            "passed": True,
            "state_root": str(self.state_root),
            "campaign_fingerprint_sha256": fingerprint.sha256,
            "handoff_policy": fingerprint.handoff_policy,
            "novel_count": scheduler.novel_count,
            "top_candidates": list(scheduler.top_candidates),
            "single_minimum_n": min(
                float(row["mae_n"]) for row in scheduler.accepted_observations
            ),
            "writer_released": True,
            "final_safety_normal_home": True,
            "final_writer_release": str(
                self.state_root / "final_writer_release.json"
            ),
        }


__all__ = [
    "DEFAULT_KUNWEI_HOST",
    "DEFAULT_KUNWEI_PORT",
    "DEFAULT_OPTIMIZER_PYTHON",
    "DEFAULT_ROBOT_HOST",
    "FigureEightCampaignRunnerError",
    "FigureEightProductionCampaignV1",
    "ProductionCampaignInputsV1",
    "derive_launch_evidence",
    "execute_handoff_ab",
    "handoff_evidence_from_result",
    "provisional_anchor_candidate",
]
