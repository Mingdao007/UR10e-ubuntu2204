"""R013 live owner over the reviewed R005/R006/R008 physical writer.

This module is intentionally an adapter, not a second motion implementation:
the mature writer still owns RTDE/Kunwei, TP state, safe return, raw force
evidence, and ledger sealing.  R013 owns candidate identity, its fresh physical
sidecar, the active soft/hard tube environment, and anti-windup receipts.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import gc
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

from .live_runtime import (
    active_runtime,
    anti_windup_metrics_from_rows,
    configure_r013_figure8_candidate,
    configure_r013_handoff_policy,
    figure8_correction_receipt_from_rows,
    install_r013_runtime_patch,
    mark_r013_safe_return_transition,
    runtime_strategy_receipt_from_rows,
    uninstall_r013_runtime_patch,
)
from .runtime_strategy import RuntimeStrategyReceiptLedger, validate_runtime_strategy
from .runtime_strategy import runtime_strategy_sha256
from .campaign import Campaign, PhysicalAdmissionReceipt
from .campaign_config import controller_source_identity_sha256
from .identity import CampaignFingerprint, LEGACY_PROFILE_IDENTITY
from .contact_transient import (
    CONTACT_TRANSIENT_RECEIPT_SCHEMA,
    CONTACT_TRANSIENT_SIDECAR_NAME,
    CONTACT_TRANSIENT_SIDECAR_SCHEMA,
    ContactTransientError,
    contact_transient_receipt,
)
from .domain import KI_LATTICE_ANCHOR
from .lifecycle_trace import LifecycleTrace, attach_lifecycle_trace
from .timing_scheduler import (
    FormalTimingSchedulerLeaseV2,
    TimingSchedulerProfileV1,
)
from .handoff import (
    BLIND_RESET_V0,
    FREEZE_CARRY_V1,
    HANDOFF_POLICY_SCHEMA,
    HANDOFF_POLICY_VERSION,
    HANDOFF_SCHEMA,
)
from .feedforward import FeedforwardMode, FeedforwardProfile
from .baseline_policy import (
    MotionAdmissionProfileV1,
    R013BaselineResidualPolicyV1,
    R013BaselineTransitionProfileV1,
)
from step5d_autotune_v4_r012.path_cbf_live import (
    R012_HARD_TUBE_AXES_M,
    R012_SOFT_CBF_AXES_M,
    R012PathGuardStack,
    install_r012_guard_stack,
    is_r012_guard_bound,
)
from step5d_autotune_v4_r012.live_host import PathEarlyEndController
from .path_context import (
    FIGURE8_DURATION_S,
    FIGURE8_FORMAL_BIN_COUNT,
    FIGURE8_FORMAL_START_S,
)


R013_MOTION_PROTOCOL = 606006
R013_RUNTIME_PROTOCOL = 613013
R013_READABLE_IDENTITY = (13, R013_RUNTIME_PROTOCOL)
R013_QUAL_MIN_WRITER_HZ = 460.0
R013_QUAL_MIN_RTDE_HZ = 460.0
R013_QUAL_MIN_TP_HZ = 440.0
R013_QUAL_MIN_TP_WRITER_RATIO = 0.95
R013_QUAL_MIN_KUNWEI_HZ = 900.0
R013_PATH_ENTRY_RATE_LIMIT_ENV = "R008_PATH_ENTRY_RATE_LIMIT"


class R013OwnerError(RuntimeError):
    """R013 live admission or exact-trial evidence failed closed."""


@dataclass
class R013GCWindowReceipt:
    """In-memory proof for the bounded ARM-to-Home cyclic-GC window."""

    pre_enabled: bool
    entered: bool = False
    restored: bool = False
    post_enabled: bool | None = None
    restored_after_timing_lease: bool = False

    @classmethod
    def capture(cls) -> "R013GCWindowReceipt":
        return cls(pre_enabled=gc.isenabled())

    def enter(self) -> None:
        gc.disable()
        self.entered = True

    def restore_after_timing_lease(self) -> None:
        if self.pre_enabled:
            if not gc.isenabled():
                gc.enable()
        elif gc.isenabled():
            gc.disable()
        self.post_enabled = gc.isenabled()
        self.restored = self.post_enabled is self.pre_enabled
        self.restored_after_timing_lease = self.restored
        if not self.restored:
            raise R013OwnerError("R013 cyclic GC state was not restored")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v4/r013-gc-window-receipt-v1",
            "scope": "ARM_EXECUTE_MOTION_CONTACT_SAFE_RETURN_HOME",
            "pre_enabled": self.pre_enabled,
            "entered": self.entered,
            "restored": self.restored,
            "post_enabled": self.post_enabled,
            "restored_after_timing_lease": self.restored_after_timing_lease,
        }


def _r013_profile_binding_enabled(
    *,
    fingerprint: CampaignFingerprint,
    feedforward_profile: FeedforwardProfile,
    motion_admission_profile: MotionAdmissionProfileV1,
    baseline_transition_profile: R013BaselineTransitionProfileV1,
    baseline_residual_policy: R013BaselineResidualPolicyV1,
) -> bool:
    profile_values = (
        fingerprint.feedforward_profile_identity,
        fingerprint.motion_admission_profile_identity,
        fingerprint.baseline_transition_profile_identity,
        fingerprint.baseline_residual_policy_identity,
    )
    if all(value == LEGACY_PROFILE_IDENTITY for value in profile_values):
        if feedforward_profile.mode is FeedforwardMode.OFF:
            raise R013OwnerError(
                "R013 FF-off requires a fresh profile-bound campaign fingerprint"
            )
        return False
    expected_fingerprint = fingerprint.with_r013_profiles(
        feedforward_profile_identity=feedforward_profile.profile_id,
        motion_admission_profile_identity=motion_admission_profile.profile_id,
        baseline_transition_profile_identity=baseline_transition_profile.profile_id,
        baseline_residual_policy_identity=baseline_residual_policy.policy_id,
    )
    if fingerprint != expected_fingerprint:
        raise R013OwnerError("R013 campaign profile fingerprint differs")
    return True


def _require_current_r013_source_identity(
    *,
    fingerprint: CampaignFingerprint,
    controller_triplet_sha256: Mapping[str, Any],
) -> str:
    """Reject a prepared fingerprint after either host or TP source drifts."""

    current = controller_source_identity_sha256(controller_triplet_sha256)
    if fingerprint.source_identity != current:
        raise R013OwnerError(
            "R013 current host/controller source identity differs from the prepared campaign"
        )
    return current


@dataclass(frozen=True)
class R013PathProfileV1:
    """Typed duration/metric seam; cycloid defaults remain 60 s/550 bins."""

    profile_id: str
    path_id: str
    duration_s: float
    formal_start_s: float
    formal_end_s: float
    bin_width_s: float
    required_bins: int
    metric_id: str

    @classmethod
    def cycloid(cls) -> "R013PathProfileV1":
        return cls("cycloid_r013_default_v1", "r013_cycloid_v1", 60.0, 5.0, 60.0, 0.1, 550, "force-mae-v2-sealed")

    @classmethod
    def figure8(cls) -> "R013PathProfileV1":
        return cls(
            "figure8_r013_direct_v1",
            "r013_figure8_v1",
            FIGURE8_DURATION_S,
            FIGURE8_FORMAL_START_S,
            FIGURE8_DURATION_S,
            0.1,
            FIGURE8_FORMAL_BIN_COUNT,
            "figure8-force-mae-v1",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v4/r013-path-profile-v1",
            "version": 1,
            "profile_id": self.profile_id,
            "path_id": self.path_id,
            "duration_s": self.duration_s,
            "formal_window_s": [self.formal_start_s, self.formal_end_s],
            "bin_width_s": self.bin_width_s,
            "required_bins": self.required_bins,
            "metric_id": self.metric_id,
        }


def _r013_attempt_kind(kind: str) -> Any:
    """Map optimizer dispatch kinds onto the mature physical attempt kinds."""

    from step5d_autotune_v4_r004.wire import AttemptKind

    mapping = {
        "QUALIFICATION": AttemptKind.QUALIFICATION,
        "WARM_FIXED_KI": AttemptKind.BATCH_A,
        "WARM_SOBOL": AttemptKind.BATCH_A,
        "CONFIRMATION": AttemptKind.BATCH_A,
        "ANCHOR_RETEST": AttemptKind.RETEST,
        "LOCAL_REFINEMENT": AttemptKind.RETEST,
        "HIGH_KI_PROBE": AttemptKind.RETEST,
        "NORMAL_VELOCITY_GAIN_PROBE": AttemptKind.RETEST,
        "LOWER_P_OVER_D_PROBE": AttemptKind.RETEST,
        "PHASE_STRATEGY_CANARY": AttemptKind.RETEST,
        "BO_TRIAL": AttemptKind.BATCH_B,
    }
    try:
        return mapping[kind]
    except KeyError as exc:
        raise R013OwnerError(f"unsupported R013 physical kind: {kind}") from exc


def _bind_r012_guard_stack(
    writer: Any,
    guard_stack: R012PathGuardStack | None = None,
) -> R012PathGuardStack:
    """Bind exactly one authoritative R012 PATH stack before live open."""

    requested = guard_stack or R012PathGuardStack()
    bound = install_r012_guard_stack(writer, requested)
    if (
        bound is not requested
        or not is_r012_guard_bound(writer)
        or getattr(writer, "_r012_guard_stack", None) is not requested
        or getattr(getattr(writer, "injection", None), "_r012_guard_stack", None) is not requested
    ):
        raise R013OwnerError("R013 mature writer is not bound to the requested R012 PATH guard")
    return requested


def _require_r012_guard_stack(writer: Any, guard_stack: R012PathGuardStack) -> None:
    if (
        not is_r012_guard_bound(writer)
        or getattr(writer, "_r012_guard_stack", None) is not guard_stack
        or getattr(getattr(writer, "injection", None), "_r012_guard_stack", None) is not guard_stack
    ):
        raise R013OwnerError("R013 mature writer lost its R012 PATH guard binding")


@dataclass
class R013LiveContext:
    writer: Any
    runtime: Any
    run_trial: Callable[[Any], Mapping[str, Any]]
    physical_ledger: Any
    state20_trace: Any
    state21_trace: Any
    state25_trace: Any
    lifecycle_trace: LifecycleTrace
    register_writer: Any
    close: Callable[[], None]
    configure_handoff: Callable[[Any], Mapping[str, Any]] | None = None
    configure_safe_return_overlap: Callable[[Callable[[], None] | None], None] | None = None
    safe_return_overlap_errors: list[str] = field(default_factory=list)
    path_profile: R013PathProfileV1 = field(default_factory=R013PathProfileV1.cycloid)
    feedforward_profile: FeedforwardProfile = field(
        default_factory=FeedforwardProfile.from_value
    )
    motion_admission_profile: MotionAdmissionProfileV1 = field(
        default_factory=lambda: MotionAdmissionProfileV1.from_feedforward(
            FeedforwardProfile.from_value()
        )
    )
    path_early_end: PathEarlyEndController | None = None
    configure_path_sample_observer: Callable[[Callable[[Any], None] | None], None] | None = None
    configure_active_censor: Callable[[Any | None], None] | None = None
    timing_scheduler_profile: str = "late_control_fifo_v1"
    resident_epoch: int = 0
    resident_ready: bool = False
    safe_home_permitted: Callable[[Any], bool] | None = None
    safe_home_after_failure: Callable[[], Mapping[str, Any]] | None = None

    def stop(self, reason: str = "operator_stop") -> None:
        """Revoke the live writer through its typed safe-stop boundary."""

        self.runtime.revoke_authority(reason)

    @property
    def state25_sidecar(self) -> Path | None:
        return getattr(self.state25_trace, "sidecar_path", None)

    @property
    def state21_sidecar(self) -> Path | None:
        return getattr(self.state21_trace, "sidecar_path", None)


def _handoff_receipt_complete(receipt: Any) -> bool:
    """Validate completion using the semantics of the selected A/B limb."""

    if not isinstance(receipt, Mapping):
        return False
    common_complete = bool(
        receipt.get("schema") == HANDOFF_SCHEMA
        and receipt.get("policy_schema") == HANDOFF_POLICY_SCHEMA
        and receipt.get("policy_version") == HANDOFF_POLICY_VERSION
        and receipt.get("status") == "complete"
        and receipt.get("baseline_observed") is True
        and receipt.get("first_path_tick_observed") is True
        and receipt.get("path_clock_started_in_baseline") is False
        and receipt.get("path_clock_start_count") == 1
        and receipt.get("tangential_orientation_started_with_path") is True
    )
    if not common_complete:
        return False
    policy = receipt.get("policy")
    if policy == FREEZE_CARRY_V1:
        return bool(
            receipt.get("handoff_mode") == "freeze_carry"
            and receipt.get("reset_at_path_entry") is False
            and receipt.get("blind_reset_observed") is False
            and receipt.get("continuity_ok") is True
        )
    if policy == BLIND_RESET_V0:
        # Discontinuity is the explicitly selected A-limb behavior.  Its
        # completion proof is the observed, typed reset at the first PATH
        # tick; requiring continuity here would make that A/B limb
        # structurally unreachable.
        return bool(
            receipt.get("handoff_mode") == "blind_reset"
            and receipt.get("reset_at_path_entry") is True
            and receipt.get("blind_reset_observed") is True
            and receipt.get("continuity_ok") is False
        )
    return False


def _apply_r013_lifecycle_gate(
    result: Any,
    receipt: Mapping[str, Any],
    *,
    require_handoff: bool = False,
) -> Any:
    """Join full-trajectory and optional handoff coverage to the result."""

    metrics = dict(getattr(result, "metrics", {}) or {})
    metrics["r013_force_lifecycle"] = dict(receipt)
    lifecycle_complete = bool(receipt.get("coverage_complete"))
    runtime = active_runtime()
    handoff = getattr(runtime, "_r013_handoff", None)
    handoff_receipt = handoff.receipt() if handoff is not None else None
    handoff_complete = _handoff_receipt_complete(handoff_receipt)
    metrics["r013_handoff"] = (
        dict(handoff_receipt) if isinstance(handoff_receipt, Mapping) else None
    )
    metrics["r013_handoff_complete"] = handoff_complete
    metrics["r013_force_lifecycle_complete"] = bool(
        lifecycle_complete and (not require_handoff or handoff_complete)
    )
    if bool(metrics["r013_force_lifecycle_complete"]):
        return replace(result, metrics=metrics)
    changes: dict[str, Any] = {"metrics": metrics}
    # R005 qualification results carry qualification_passed; R006 ordinary
    # results do not.  Keep this helper compatible with both typed result
    # families without weakening either gate.
    if hasattr(result, "timing_gate"):
        changes["timing_gate"] = False
    if hasattr(result, "qualification_passed"):
        changes["qualification_passed"] = False
    return replace(result, **changes)


def _writer_tp_state(writer: Any) -> int | None:
    output = getattr(writer, "_last_output", None)
    try:
        return int(output.integer_echoes[26]) if output is not None else None
    except (TypeError, ValueError, KeyError, IndexError):
        return None


def _finalize_r013_lifecycle(
    trace: LifecycleTrace,
    writer: Any,
    *,
    result: Any | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Capture terminal output and cold-seal the current attempt."""

    output = getattr(writer, "_last_output", None)
    try:
        # safe_return has already verified READY_HOME_NEXT.  This read is
        # observation-only at the stationary Home boundary and supplies the
        # final force sample that the terminal TP frame itself does not carry.
        terminal_sensor = writer._read_sensor()
    except Exception:
        terminal_sensor = None
    observed_state = _writer_tp_state(writer)
    safe_return = bool(getattr(result, "safe_return", False)) if result is not None else False
    terminal_state = observed_state
    if terminal_state is None:
        terminal_state = 78 if safe_return else 90
    home_verified = bool(safe_return and terminal_state == 78)
    try:
        monotonic_s = float(writer._mono_clock())
    except (AttributeError, TypeError, ValueError):
        monotonic_s = None
    trace.observe_terminal(
        monotonic_s=monotonic_s,
        output=output,
        sensor=terminal_sensor,
        tp_state=terminal_state,
        packet_sequence=max(-1, int(getattr(writer, "_packet_sequence", 0)) - 1),
        consumed_packet_sequence=int(getattr(output, "consumed_packet_sequence", -1)),
    )
    receipt = trace.finalize_attempt(
        terminal_state=terminal_state,
        home_verified=home_verified,
        error=error,
    )
    # Keep the normal PATH-end handshake observable without adding any
    # filesystem work to the 500 Hz loop.  This is a terminal receipt field,
    # so a failed or late request cannot be mistaken for a controller ack.
    path_end_controller = getattr(writer, "_r013_path_early_end_controller", None)
    external_request = bool(getattr(path_end_controller, "requested", False))
    external_sequence = getattr(path_end_controller, "requested_sequence", None)
    request_receipt = {
        "schema": "step5d.autotune-v4/r013-path-end-request-receipt-v1",
        "controller_bound": path_end_controller is not None,
        "requested": bool(getattr(writer, "_r013_path_end_requested", False) or external_request),
        "sequence": (
            getattr(writer, "_r013_path_end_request_sequence", None)
            if bool(getattr(writer, "_r013_path_end_requested", False))
            else external_sequence
        ),
        "host_monotonic_s": getattr(writer, "_r013_path_end_request_mono_s", None),
        "request_register": 35,
        "ack_register": 36,
        "ack_source": (
            "host_path_clock"
            if bool(getattr(writer, "_r013_path_end_requested", False))
            else "transport_terminal_state_guard"
            if external_request
            else "none"
        ),
    }
    receipt["r013_path_end_request"] = request_receipt
    receipt_path = receipt.get("receipt_path")
    if isinstance(receipt_path, str) and receipt_path:
        try:
            destination = Path(receipt_path)
            temporary = destination.with_suffix(destination.suffix + ".part")
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(receipt, stream, sort_keys=True, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        except OSError as exc:
            receipt["r013_path_end_request_receipt_error"] = str(exc)
    return receipt


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise R013OwnerError(f"R013 artifact is unavailable or invalid: {path}") from exc
    if not isinstance(value, dict):
        raise R013OwnerError(f"R013 artifact is not an object: {path}")
    return value


def _materialize_attempt_boundary_receipt(
    *,
    run_dir: Path,
    sequence: int,
    dispatch_id: str,
    candidate_key: str,
    trace_paths: Sequence[Path],
) -> dict[str, Any]:
    """Complete all attempt-boundary work before the next ARM call.

    This receipt is intentionally written while the robot is already at
    verified Home.  Once ``arm_permitted`` is published, the live owner must
    not perform filesystem/hash/GC work until safe return.
    """

    started = time.monotonic_ns()
    gc.collect()
    trace_state = []
    for path in trace_paths:
        target = Path(path)
        trace_state.append(
            {
                "path": str(target.resolve()),
                "exists": target.exists(),
                "size_bytes": target.stat().st_size if target.exists() else 0,
            }
        )
    body = {
        "schema": "step5d.autotune-v4/r013-attempt-boundary-receipt-v1",
        "version": 1,
        "sequence": int(sequence),
        "dispatch_id": str(dispatch_id),
        "candidate_key": str(candidate_key),
        "boundary": "VERIFIED_HOME_BEFORE_ARM",
        "candidate_materialized": True,
        "trace_flush_complete": True,
        "trace_state": trace_state,
        "filesystem_hash_and_seal_deferred_until_home": True,
        "gc_complete": True,
        "arm_permitted": True,
        "started_monotonic_ns": started,
        "completed_monotonic_ns": time.monotonic_ns(),
    }
    destination = Path(run_dir) / f"attempt-boundary-{sequence:06d}.json"
    temporary = destination.with_suffix(destination.suffix + ".part")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(body, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    receipt = {**body, "receipt_path": str(destination.resolve())}
    return receipt


def _validate_resident_ready_binding(
    *,
    run_dir: Path,
    ready: Mapping[str, Any],
    launch: Mapping[str, Any],
    campaign_id: str,
    run_id: str,
    attempt_id: str,
    contract: Any,
    expected_program: str = "step5d_strict_rnn_autotune_v4_r013",
) -> dict[str, str]:
    """Require the physical RTDE READY tuple before constructing any writer."""

    if ready.get("status") != "resident_ready_no_arm":
        raise R013OwnerError("R013 resident receipt is not READY/no-ARM")
    if ready.get("program") != expected_program:
        raise R013OwnerError("R013 resident program identity differs")
    if launch.get("campaign_id") != campaign_id or launch.get("run_id") != run_id:
        raise R013OwnerError("R013 launch campaign/run binding differs")
    if (
        launch.get("contract_sha256") != contract.sha256
        or launch.get("campaign_fingerprint") != contract.campaign_fingerprint
    ):
        raise R013OwnerError("R013 launch contract/campaign binding differs")
    if launch.get("attempt_id") != attempt_id or ready.get("attempt_id") != attempt_id:
        raise R013OwnerError("R013 launch attempt binding differs")
    if ready.get("route_id") != launch.get("route_id"):
        raise R013OwnerError("R013 route binding differs")
    if ready.get("resident_session_id") != launch.get("session_id"):
        raise R013OwnerError("R013 resident session binding differs")
    if ready.get("session_epoch") != launch.get("session_epoch"):
        raise R013OwnerError("R013 resident epoch binding differs")
    if launch.get("program") != ready.get("program"):
        raise R013OwnerError("R013 launch program binding differs")
    if launch.get("controller_target") not in {None, ready.get("controller_target")}:
        raise R013OwnerError("R013 launch controller target differs")

    expected_triplet = launch.get("triplet")
    if not isinstance(expected_triplet, Mapping) or set(expected_triplet) != {"script", "txt", "urp"}:
        raise R013OwnerError("R013 launch triplet is incomplete")
    expected_triplet = {role: str(expected_triplet[role]) for role in ("script", "txt", "urp")}
    if ready.get("triplet") != expected_triplet:
        raise R013OwnerError("R013 resident triplet binding differs")
    if ready.get("contract_sha256") not in {None, contract.sha256}:
        raise R013OwnerError("R013 resident contract binding differs")
    if ready.get("campaign_fingerprint") not in {None, contract.campaign_fingerprint}:
        raise R013OwnerError("R013 resident campaign binding differs")
    if ready.get("runtime_protocol") != 606006:
        raise R013OwnerError("R013 resident runtime protocol differs")

    evidence_path = run_dir / "resident_ready_evidence.json"
    if ready.get("resident_ready_evidence") != str(evidence_path):
        raise R013OwnerError("R013 resident-ready evidence path is not canonical")
    evidence = _read_json(evidence_path)
    if evidence.get("schema") != "step5d.autotune-v4/r013-resident-ready-evidence-v2":
        raise R013OwnerError("R013 resident-ready evidence schema differs")
    if evidence.get("status") != "resident_ready_no_arm":
        raise R013OwnerError("R013 resident-ready evidence is not READY/no-ARM")
    if evidence.get("observation_source") != "fresh_rtde_observation":
        raise R013OwnerError("R013 resident-ready evidence is not a fresh RTDE observation")
    for name, expected in (
        ("route_id", ready.get("route_id")),
        ("attempt_id", ready.get("attempt_id")),
        ("resident_session_id", ready.get("resident_session_id")),
        ("session_epoch", ready.get("session_epoch")),
        ("program", ready.get("program")),
        ("controller_target", ready.get("controller_target")),
    ):
        if evidence.get(name) != expected:
            raise R013OwnerError(f"R013 resident-ready {name} binding differs")
    if evidence.get("triplet_sha256") != expected_triplet:
        raise R013OwnerError("R013 resident-ready read-back triplet differs")
    if evidence.get("contract_sha256") != contract.sha256:
        raise R013OwnerError("R013 resident-ready contract differs")
    if evidence.get("campaign_fingerprint") != contract.campaign_fingerprint:
        raise R013OwnerError("R013 resident-ready campaign differs")
    if evidence.get("runtime_output_registers") != {"32": 606006, "33": 13, "34": 613013}:
        raise R013OwnerError("R013 resident-ready physical output identity is not exact")
    if (
        evidence.get("runtime_protocol") != 606006
        or evidence.get("runtime_revision") != 13
        or evidence.get("runtime_extension_protocol") != 613013
        or evidence.get("program_running") is not True
        or evidence.get("stationary") is not True
        or evidence.get("safety_normal") is not True
        or evidence.get("no_arm") is not True
        or evidence.get("arm_dispatched") is not False
        or evidence.get("trial_dispatched") is not False
    ):
        raise R013OwnerError("R013 resident-ready physical state is incomplete")
    dashboard = evidence.get("dashboard")
    if (
        not isinstance(dashboard, Mapping)
        or dashboard.get("is in remote control", "").strip().lower() != "true"
        or "NORMAL" not in str(dashboard.get("safetymode", ""))
        or dashboard.get("get loaded program") != f"Loaded program: {ready.get('controller_target')}"
        or dashboard.get("running", "").strip().lower() != "program running: true"
    ):
        raise R013OwnerError("R013 resident-ready Dashboard binding differs")

    controller = _read_json(run_dir / "controller_receipt.json")
    controller_triplet = {
        role: controller.get(f"{role}_sha256") for role in ("script", "txt", "urp")
    }
    if controller_triplet != expected_triplet:
        raise R013OwnerError("R013 controller receipt triplet differs")
    if (
        controller.get("program") != ready.get("program")
        or controller.get("controller_target") != ready.get("controller_target")
        or controller.get("route_id") != ready.get("route_id")
        or controller.get("observation_source") != "fresh_controller_readback_and_rtde"
    ):
        raise R013OwnerError("R013 controller read-back binding differs")

    runtime = _read_json(run_dir / "runtime_evidence.json")
    projection = runtime.get("runtime_identity_projection")
    if not isinstance(projection, Mapping) or (
        projection.get("kind") != "r013_legacy_runtime_limbs"
        or projection.get("physically_read") is not False
        or projection.get("source_evidence") != "resident_ready_evidence.json"
    ):
        raise R013OwnerError("R013 runtime evidence lacks an explicit compatibility projection")
    if (
        runtime.get("program") != ready.get("program")
        or runtime.get("script_sha256") != expected_triplet["script"]
        or runtime.get("resident_session_id") != ready.get("resident_session_id")
        or runtime.get("session_epoch") != ready.get("session_epoch")
        or runtime.get("program_running") is not True
        or runtime.get("uninterrupted") is not True
        or runtime.get("observed_at_s") != evidence.get("observed_at_s")
    ):
        raise R013OwnerError("R013 runtime evidence binding differs")
    return expected_triplet


def _open_physical_ledger(
    path: Path,
    *,
    campaign_fingerprint: str,
    eoat_sha256: str,
    path_profile: R013PathProfileV1 | None = None,
) -> Any:
    """Open a fresh mature physical ledger without importing R012 rows."""

    selected = path_profile or R013PathProfileV1.cycloid()
    if selected.profile_id == R013PathProfileV1.figure8().profile_id:
        from step6_figure8_autotune_v1.physical_ledger import (
            FigureEightPhysicalLedgerV1,
        )

        return FigureEightPhysicalLedgerV1(
            Path(path),
            campaign_fingerprint=campaign_fingerprint,
            eoat_sha256=eoat_sha256,
        )

    import step5d_autotune_v4_r005.observations as observations_module
    from step5d_autotune_v4_r006.live_adapter import R006Candidate
    from step5d_autotune_v4_r008.bounded_resume_ledger import R008BoundedResumeObservationLedger

    original = observations_module.Candidate
    observations_module.Candidate = R006Candidate
    try:
        return R008BoundedResumeObservationLedger(
            Path(path), campaign_fingerprint=campaign_fingerprint, eoat_sha256=eoat_sha256
        )
    finally:
        observations_module.Candidate = original


def _r013_compat_contract(
    profile: R013PathProfileV1 | None = None,
) -> Any:
    from step5d_autotune_v4_r012.compat_identity import r012_compat_contract

    contract = r012_compat_contract()
    raw = json.loads(json.dumps(dict(contract.raw)))
    selected = profile or R013PathProfileV1.cycloid()
    program = (
        "step6_figure8_autotune_v1"
        if selected.profile_id == R013PathProfileV1.figure8().profile_id
        else "step5d_strict_rnn_autotune_v4_r013"
    )
    controller_directory = (
        "/programs/andyl/kunwei/step6"
        if selected.profile_id == R013PathProfileV1.figure8().profile_id
        else "/programs/andyl/kunwei/step5"
    )
    script2 = dict(raw.get("script2", {}))
    script2["controller_target"] = f"{controller_directory}/{program}.urp"
    raw["program"] = program
    raw["lineage_id"] = program
    raw["script2"] = script2
    return replace(contract, raw=raw)


def _run_r013_profile(runtime: Any, attempt: Any, profile: R013PathProfileV1) -> Any:
    runner = getattr(runtime, "run_profile", None)
    if callable(runner):
        return runner(attempt, profile)
    if profile == R013PathProfileV1.cycloid():
        return runtime.run_60s(attempt)
    raise R013OwnerError(
        f"R013 profile {profile.profile_id} is not_deployable through the mature runtime seam"
    )


def _run_mature_profile_path(
    writer: Any, attempt: Any, profile: R013PathProfileV1
) -> Any:
    mature_writer = writer.writer
    previous_duration_s = mature_writer._path_duration_s
    mature_writer._path_duration_s = profile.duration_s
    try:
        return writer.run_60s(attempt)
    finally:
        mature_writer._path_duration_s = previous_duration_s


def _apply_profile_candidate(runtime: Any, candidate: Mapping[str, Any], profile: R013PathProfileV1) -> None:
    """Apply a complete Figure-eight candidate only through the mature seam."""

    if profile.profile_id != R013PathProfileV1.figure8().profile_id:
        return
    if not isinstance(candidate.get("controller_path"), Mapping) or not isinstance(candidate.get("correction"), Mapping):
        raise R013OwnerError("R013 Figure-eight candidate is not complete")
    method = getattr(runtime, "apply_complete_candidate", None)
    if not callable(method):
        raise R013OwnerError("R013 Figure-eight profile is not_deployable without apply_complete_candidate")
    method(dict(candidate))


def _fresh_trial_flow_gates(
    result: Any,
    *,
    profile: R013PathProfileV1 | None = None,
    allow_timing_ineligible: bool = False,
) -> None:
    selected_profile = profile or R013PathProfileV1.cycloid()
    required = ("safe_return", "safety_gate", "contact_gate", "return_gate", "identity_gate")
    if not all(bool(getattr(result, key, False)) for key in required):
        raise R013OwnerError(
            "R013 physical result did not satisfy flow gates: "
            + ", ".join(f"{key}={getattr(result, key, None)}" for key in required)
        )
    if float(getattr(result, "duration_s", 0.0)) < selected_profile.duration_s:
        raise R013OwnerError(f"R013 exact trial duration is short: {result.duration_s}")
    metrics = getattr(result, "metrics", {})
    if not isinstance(metrics, Mapping) or not bool(
        metrics.get("r013_force_lifecycle_complete", False)
    ):
        raise R013OwnerError(
            "R013 physical result lacks a complete force lifecycle receipt"
        )
    objective = metrics.get("force_objective") if isinstance(metrics, Mapping) else None
    if not isinstance(objective, Mapping):
        objective = getattr(result, "force_objective", None)
        objective = objective.as_dict() if hasattr(objective, "as_dict") else {}
    if (
        int(objective.get("complete_bins", 0)) != selected_profile.required_bins
        or int(objective.get("required_bins", 0)) != selected_profile.required_bins
    ):
        raise R013OwnerError(
            f"R013 exact trial lacks {selected_profile.required_bins} sealed bins: {objective}"
        )
    lifecycle = metrics.get("r013_force_lifecycle")
    if isinstance(lifecycle, Mapping):
        try:
            path_duration = float(lifecycle.get("path_duration_s"))
            max_gap = float(
                lifecycle.get("max_nonterminal_gap_s", lifecycle.get("max_gap_s"))
            )
            max_rtde_gap = float(lifecycle.get("max_rtde_gap_s"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise R013OwnerError("R013 lifecycle timing fields are not finite") from exc
        if not all(math.isfinite(value) for value in (path_duration, max_gap, max_rtde_gap)):
            raise R013OwnerError("R013 lifecycle timing fields are nonfinite")
        # A complete lifecycle is not sufficient if the host/RTDE seam has
        # silently overrun the declared 60 s PATH or crossed the 20 ms
        # freshness boundary.  The terminal Home observation may be delayed
        # by the blocking return primitive; that diagnostic gap is preserved
        # separately and is not a control-loop freshness measurement. Preserve
        # the raw artifact, but do not admit it
        # to BO/tell_exact as an exact observation.
        if path_duration > selected_profile.duration_s + 0.25:
            raise R013OwnerError(
                f"R013 PATH duration exceeded profile: {path_duration:.6f}s > "
                f"{selected_profile.duration_s + 0.25:.6f}s"
            )
        if max_gap >= 0.020 or max_rtde_gap >= 0.020:
            if allow_timing_ineligible:
                return
            raise R013OwnerError(
                "R013 lifecycle freshness gap reached 20 ms: "
                f"host={max_gap:.6f}s rtde={max_rtde_gap:.6f}s"
            )


def _qualification_flow_gates(result: Any) -> None:
    required = (
        "qualification_passed", "safe_return", "binding_ok", "safety_gate",
        "contact_gate", "return_gate", "timing_gate", "identity_gate",
    )
    if not all(bool(getattr(result, key, False)) for key in required):
        metrics = (
            dict(result.metrics)
            if isinstance(getattr(result, "metrics", None), Mapping)
            else {}
        )
        timing_diagnostics = {
            key: metrics.get(key)
            for key in (
                "qualification_packet_timing_gate",
                "qualification_baseline_samples",
                "timing_evidence",
                "r008_timing_decision",
            )
            if key in metrics
        }
        raise R013OwnerError(
            "R013 owner qualification did not satisfy durable gates: "
            + ", ".join(f"{key}={getattr(result, key, None)}" for key in required)
            + f"; timing_diagnostics={json.dumps(timing_diagnostics, sort_keys=True)}"
        )
    if getattr(result, "force_objective", None) is not None:
        raise R013OwnerError("R013 owner qualification unexpectedly contains an objective")


def _r013_fresh_qualification_timing_acceptance(result: Any) -> bool:
    """Bounded rescue for healthy TP cadence below R008's legacy 460 Hz floor."""

    if bool(getattr(result, "timing_gate", False)):
        return False
    metrics = result.metrics if isinstance(getattr(result, "metrics", None), Mapping) else {}
    timing = metrics.get("timing_evidence")
    decision = metrics.get("r008_timing_decision")
    rates = timing.get("layer_rates_hz") if isinstance(timing, Mapping) else None
    if not all(isinstance(value, Mapping) for value in (timing, decision, rates)):
        return False
    if metrics.get("qualification_packet_timing_gate") is not True:
        return False
    if decision.get("eligible") is not False:
        return False
    if decision.get("failures") != ["tp_consumption_ratio_below_0p98"]:
        return False
    try:
        duration_s = float(timing["duration_s"])
        writer_hz = float(rates["writer_publishes"])
        rtde_hz = float(rates["rtde_frames"])
        tp_hz = float(rates["tp_consumed_packet_echoes"])
        kunwei_hz = float(rates["kunwei_frames"])
        feedback_p99_s = float(timing["feedback_age_p99_s"])
        max_fresh_gap_s = float(timing["max_fresh_gap_s"])
        if not all(
            math.isfinite(value)
            for value in (
                duration_s,
                writer_hz,
                rtde_hz,
                tp_hz,
                kunwei_hz,
                feedback_p99_s,
                max_fresh_gap_s,
            )
        ):
            return False
        return bool(
            duration_s >= 17.99
            and writer_hz >= R013_QUAL_MIN_WRITER_HZ
            and rtde_hz >= R013_QUAL_MIN_RTDE_HZ
            and tp_hz >= R013_QUAL_MIN_TP_HZ
            and tp_hz / writer_hz >= R013_QUAL_MIN_TP_WRITER_RATIO
            and kunwei_hz >= R013_QUAL_MIN_KUNWEI_HZ
            and feedback_p99_s <= 0.01
            and max_fresh_gap_s < 0.02
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return False


def _apply_r013_qualification_timing_acceptance(result: Any) -> Any:
    if not _r013_fresh_qualification_timing_acceptance(result):
        return result
    metrics = dict(result.metrics)
    metrics["parent_timing_gate_passed"] = False
    metrics["timing_gate_passed"] = True
    metrics["r013_qualification_timing_acceptance"] = {
        "schema": "step5d.autotune-v4/r013-qualification-timing-acceptance-v1",
        "accepted": True,
        "allowed_parent_failures": ["tp_consumption_ratio_below_0p98"],
        "minimum_writer_hz": R013_QUAL_MIN_WRITER_HZ,
        "minimum_rtde_hz": R013_QUAL_MIN_RTDE_HZ,
        "minimum_tp_hz": R013_QUAL_MIN_TP_HZ,
        "minimum_tp_writer_ratio": R013_QUAL_MIN_TP_WRITER_RATIO,
        "minimum_kunwei_hz": R013_QUAL_MIN_KUNWEI_HZ,
        "feedback_age_p99_max_s": 0.01,
        "max_fresh_gap_s": 0.02,
        "packet_timing_gate_required": True,
        "tp_stale_stop_unchanged": True,
    }
    return replace(result, timing_gate=True, metrics=metrics)


def _epoch_qualification_records(
    records: Sequence[Any], *, epoch: int
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Validate current-epoch qualification continuity on the global sequence."""

    current = tuple(record for record in records if int(record.epoch) == int(epoch))
    qualifications = tuple(record for record in current if record.kind == "QUALIFICATION")
    if any(not record.qualification_eligible for record in qualifications):
        raise R013OwnerError("R013 owner qualification ledger contains an ineligible row")
    if current and len(qualifications) < 3 and any(
        record.kind != "QUALIFICATION" for record in current
    ):
        raise R013OwnerError("R013 PATH rows precede complete owner qualification")
    prior_max = max(
        (int(record.attempt_sequence) for record in records if int(record.epoch) != int(epoch)),
        default=0,
    )
    expected = tuple(range(prior_max + 1, prior_max + len(qualifications) + 1))
    if tuple(int(record.attempt_sequence) for record in qualifications) != expected:
        raise R013OwnerError("R013 owner qualification sequence is not contiguous in epoch")
    return current, qualifications


def _strict_physical_admission_from_record(
    *,
    dispatch: Any,
    record: Any,
    qualification_records: Sequence[Any],
    campaign_fingerprint: CampaignFingerprint,
) -> PhysicalAdmissionReceipt:
    """Materialize strict fields from already cold-verified physical truth."""

    return PhysicalAdmissionReceipt.from_ledger_record(
        dispatch=dispatch,
        record=record,
        epoch_qualification_passed=len(qualification_records) == 3,
        trial_admission_passed=bool(record.eligible),
        campaign_fingerprint=campaign_fingerprint,
    )


def _rows_for_attempt(trace: Any, *, ordinal: int) -> list[dict[str, Any]]:
    # The sidecar is durable audit evidence, not the realtime data path.
    # Prefer the trace's bounded current-attempt carrier so a long campaign
    # does not rescan a multi-GB JSONL file before the next qualification.
    in_memory = getattr(trace, "attempt_rows", None)
    if callable(in_memory):
        rows = list(in_memory(ordinal))
        if rows:
            return rows
    trace.flush()
    sidecar = getattr(trace, "sidecar_path", None)
    if sidecar is None or not Path(sidecar).is_file():
        raise R013OwnerError("R013 State25 sidecar is missing")
    rows: list[dict[str, Any]] = []
    with Path(sidecar).open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and int(row.get("attempt_ordinal", -1)) == int(ordinal):
                rows.append(row)
    if not rows:
        raise R013OwnerError(f"R013 State25 sidecar has no rows for attempt {ordinal}")
    return rows


def _write_contact_transient_sidecar(
    path: Path,
    *,
    dispatch_id: str,
    attempt_sequence: int,
    receipt: Mapping[str, Any],
) -> Path:
    """Append one diagnostic receipt without joining it to campaign admission."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "schema": CONTACT_TRANSIENT_SIDECAR_SCHEMA,
        "dispatch_id": str(dispatch_id),
        "attempt_sequence": int(attempt_sequence),
        "receipt": dict(receipt),
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
    return path


def _validate_strategy_physical_bindings(
    *,
    strategy_sidecar: RuntimeStrategyReceiptLedger,
    campaign: Campaign,
    physical_records: Sequence[Any],
) -> None:
    """Cold-join composite strategy receipts back to sealed physical truth."""

    dispatches = {dispatch.dispatch_id: dispatch for dispatch in campaign.dispatches}
    physical_by_sequence = {
        int(record.attempt_sequence): record for record in physical_records
    }
    if len(physical_by_sequence) != len(physical_records):
        raise R013OwnerError("R013 physical attempt sequence is duplicated")
    for row in strategy_sidecar.rows[1:]:
        dispatch = dispatches.get(str(row.get("dispatch_id", "")))
        record = physical_by_sequence.get(int(row.get("attempt_sequence", -1)))
        if dispatch is None or record is None:
            raise R013OwnerError("R013 strategy sidecar physical join is incomplete")
        if campaign.strict_admission:
            _current_records, qualification_records = _epoch_qualification_records(
                physical_records,
                epoch=int(record.epoch),
            )
            expected = _strict_physical_admission_from_record(
                dispatch=dispatch,
                record=record,
                qualification_records=qualification_records,
                campaign_fingerprint=campaign.campaign_fingerprint,
            ).as_dict()
        else:
            expected = PhysicalAdmissionReceipt.from_ledger_record(
                dispatch=dispatch,
                record=record,
            ).as_dict()
        if expected != row.get("physical_admission"):
            raise R013OwnerError("R013 strategy sidecar physical admission differs")


def _cleanup_failed_live_build(
    *,
    adapter: Any | None,
    state20_trace: Any | None,
    state25_trace: Any | None,
    register_writer: Any,
    lifecycle_trace: LifecycleTrace | None = None,
    path_entry_rate_limit_env: tuple[bool, str | None] | None = None,
    host_hard_tube_env: tuple[bool, str | None] | None = None,
    state21_trace: Any | None = None,
) -> None:
    """Release every resource acquired before a live context can be returned."""

    errors: list[Exception] = []
    if state20_trace is not None:
        try:
            state20_trace.close()
        except Exception as exc:
            errors.append(exc)
    if state21_trace is not None:
        try:
            state21_trace.close()
        except Exception as exc:
            errors.append(exc)
    if state25_trace is not None:
        try:
            state25_trace.flush()
        except Exception as exc:
            errors.append(exc)
    if lifecycle_trace is not None:
        try:
            lifecycle_trace.close()
        except Exception as exc:
            errors.append(exc)
    if adapter is not None:
        try:
            # Mark the mature writer failed before close so its canonical
            # authority receipt records reason=failed, never completed.
            adapter.revoke_authority("r013_live_context_build_failed")
        except Exception as exc:
            errors.append(exc)
        try:
            # The mature writer owns STOP/HOLD, transport shutdown, and the
            # canonical authority revoke.  A failed context build must cross
            # this same typed boundary even though no R013LiveContext exists.
            adapter.close()
        except Exception as exc:
            errors.append(exc)
    try:
        register_writer.close()
    except Exception as exc:
        errors.append(exc)
    try:
        uninstall_r013_runtime_patch()
    except Exception as exc:
        errors.append(exc)
    if path_entry_rate_limit_env is not None:
        was_present, prior_value = path_entry_rate_limit_env
        try:
            if was_present:
                assert prior_value is not None
                os.environ[R013_PATH_ENTRY_RATE_LIMIT_ENV] = prior_value
            else:
                os.environ.pop(R013_PATH_ENTRY_RATE_LIMIT_ENV, None)
        except Exception as exc:
            errors.append(exc)
    if host_hard_tube_env is not None:
        was_present, prior_value = host_hard_tube_env
        try:
            if was_present:
                assert prior_value is not None
                os.environ["R008_HOST_HARD_TUBE"] = prior_value
            else:
                os.environ.pop("R008_HOST_HARD_TUBE", None)
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise R013OwnerError("R013 failed-build cleanup did not complete") from errors[0]


def build_r013_live_context(
    *,
    run_dir: Path,
    controller_host: str,
    kunwei_host: str,
    kunwei_port: int,
    launch_profile: Path,
    campaign_id: str,
    run_id: str,
    attempt_id: str,
    runtime_strategy: Mapping[str, Any] | None = None,
    campaign: Campaign | None = None,
    path_profile: R013PathProfileV1 | None = None,
    path_fingerprint_sha256: str | None = None,
    handoff_policy: Any | None = None,
    run_epoch_qualifications: bool = True,
    feedforward_mode: FeedforwardMode | str | None = None,
    solver_profile: Any = None,
    timing_scheduler_profile: str = "late_control_fifo_v1",
) -> R013LiveContext:
    run_dir = Path(run_dir).resolve()
    selected_timing_scheduler_profile = TimingSchedulerProfileV1.from_id(
        timing_scheduler_profile
    )
    if solver_profile is None:
        from step5d_autotune_v4_r014.solver_profile import LEGACY_R1

        solver_profile = LEGACY_R1
    selected_path_profile = path_profile or R013PathProfileV1.cycloid()
    figure8_profile = (
        selected_path_profile.profile_id == R013PathProfileV1.figure8().profile_id
    )
    if figure8_profile:
        if (
            type(path_fingerprint_sha256) is not str
            or len(path_fingerprint_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in path_fingerprint_sha256
            )
        ):
            raise R013OwnerError("R013 Figure-eight fingerprint is missing or invalid")
        if campaign is not None:
            raise R013OwnerError(
                "R013 Figure-eight direct campaign cannot reuse the cycloid Campaign ledger"
            )
        if handoff_policy is None:
            raise R013OwnerError(
                "R013 Figure-eight context requires an explicit typed handoff arm"
            )
    elif not run_epoch_qualifications:
        raise R013OwnerError(
            "qualification-free owner contexts are limited to the Figure-eight V5 seam"
        )
    ready = _read_json(run_dir / "r013_live_owner_ready.json")
    launch = _read_json(run_dir / "launch_context.json")
    parsed_runtime_strategy = validate_runtime_strategy(runtime_strategy)
    feedforward_profile = FeedforwardProfile.from_value(feedforward_mode)
    motion_admission_profile = MotionAdmissionProfileV1.from_feedforward(
        feedforward_profile
    )
    baseline_transition_profile = R013BaselineTransitionProfileV1()
    baseline_residual_policy = R013BaselineResidualPolicyV1()
    r013_profile_bound = False
    strategy_identity = runtime_strategy_sha256(parsed_runtime_strategy)
    binding_campaign = campaign
    if binding_campaign is None and parsed_runtime_strategy.get("enabled") is True:
        binding_campaign = Campaign.resume(run_dir / "r013_ledger.jsonl")
    if binding_campaign is not None:
        if (
            str(binding_campaign.ledger.header["campaign_id"]) != campaign_id
            or str(binding_campaign.ledger.header["run_id"]) != run_id
            or str(binding_campaign.ledger.header["attempt_id"]) != attempt_id
        ):
            raise R013OwnerError("R013 live owner campaign identity differs")
        r013_profile_bound = _r013_profile_binding_enabled(
            fingerprint=binding_campaign.campaign_fingerprint,
            feedforward_profile=feedforward_profile,
            motion_admission_profile=motion_admission_profile,
            baseline_transition_profile=baseline_transition_profile,
            baseline_residual_policy=baseline_residual_policy,
        )
    legacy_disabled = parsed_runtime_strategy["enabled"] is False
    if (
        ready.get("runtime_strategy_sha256", strategy_identity if legacy_disabled else None)
        != strategy_identity
        or launch.get("runtime_strategy_sha256", strategy_identity if legacy_disabled else None)
        != strategy_identity
        or ready.get("runtime_strategy_enabled", False) is not bool(parsed_runtime_strategy["enabled"])
        or launch.get("runtime_strategy_enabled", False) is not bool(parsed_runtime_strategy["enabled"])
    ):
        raise R013OwnerError("R013 resident runtime strategy binding differs")
    contract = _r013_compat_contract(selected_path_profile)
    expected_program = (
        "step6_figure8_autotune_v1"
        if figure8_profile
        else "step5d_strict_rnn_autotune_v4_r013"
    )
    expected_triplet = _validate_resident_ready_binding(
        run_dir=run_dir,
        ready=ready,
        launch=launch,
        campaign_id=campaign_id,
        run_id=run_id,
        attempt_id=attempt_id,
        contract=contract,
        expected_program=expected_program,
    )
    if r013_profile_bound:
        assert binding_campaign is not None
        _require_current_r013_source_identity(
            fingerprint=binding_campaign.campaign_fingerprint,
            controller_triplet_sha256=expected_triplet,
        )
    physical_fingerprint = (
        str(path_fingerprint_sha256)
        if figure8_profile
        else contract.campaign_fingerprint
    )
    if figure8_profile and (
        ready.get("figure8_campaign_fingerprint_sha256")
        != physical_fingerprint
        or launch.get("figure8_campaign_fingerprint_sha256")
        != physical_fingerprint
    ):
        raise R013OwnerError("R013 Figure-eight launch fingerprint binding differs")

    from step5d_autotune_v4_r005.live_adapter import (
        R005LiveInputs,
        R005LiveRuntimePort,
        R005_LIVE_ACK,
    )
    from step5d_autotune_v4_r005.runtime import Attempt, AttemptResult
    from step5d_autotune_v4_r006.live_adapter import (
        R006Candidate,
        R006LiveInputs,
        R006MatureWriter,
        build_verified_mature_r006_writer,
    )
    from step5d_autotune_v4_r006.parent import load_frozen_r005_contract
    from step5d_autotune_v4_r008.live_adapter import (
        R008LiveWriterAdapter,
        R008MatureWriter,
    )
    from step5d_autotune_v4_r008.timing import r008_timing_scope
    from step5d_autotune_v4_r004.wire import AttemptKind
    from step5d_autotune_v4_r004_live_writer import LIVE_ACK as R004_LIVE_ACK
    from step5d_autotune_v4_r012.register_transport import R012LiveRTDETransport

    # The mature writer is the sole owner of the RTDE lifecycle.  Construct
    # the extension here, but let ``R004LiveWriter.open`` open it exactly once
    # after all admission checks; pre-opening it would make the writer's own
    # open boundary fail with "transport is already open".
    register_writer = R012LiveRTDETransport(controller_host)
    path_early_end = PathEarlyEndController(writer=register_writer)
    published_parent, admission_parent = load_frozen_r005_contract()
    parent_inputs = R005LiveInputs(
        controller_receipt=run_dir / "controller_receipt.json",
        script1_receipt=run_dir / "script1_receipt.json",
        runtime_evidence=run_dir / "runtime_evidence.json",
        runtime_attestation=run_dir / "runtime_evidence.json",
        optimizer_pointer=run_dir / "runtime_evidence.json",
        launch_profile_path=Path(launch_profile),
        release_manifest_sha256="0" * 64,
        baseline_ledger=run_dir / "software_baseline.json",
        software_baseline_receipt=run_dir / "software_baseline.json",
        ledger_path=Path(ready["ledger_path"]),
        queue_root=Path(ready["queue_root"]),
        authority_root=Path(ready["authority_root"]),
        controller_host=controller_host,
        kunwei_host=kunwei_host,
        kunwei_port=int(kunwei_port),
        route_id=str(ready["route_id"]),
        attempt_id=str(ready["attempt_id"]),
        resident_session_id=str(ready["resident_session_id"]),
        session_epoch=int(ready["session_epoch"]),
        expected_triplet=expected_triplet,
        eoat_sha256=str(_read_json(run_dir / "controller_receipt.json")["eoat_identity_sha256"]),
        campaign_fingerprint=contract.campaign_fingerprint,
        contract_sha256=contract.sha256,
    )
    inputs = R006LiveInputs(
        parent=parent_inputs,
        thresholds_receipt=run_dir / "thresholds_receipt.json",
        route_id=parent_inputs.route_id,
        attempt_id=parent_inputs.attempt_id,
        contract_sha256=contract.sha256,
        campaign_fingerprint=contract.campaign_fingerprint,
        expected_triplet=expected_triplet,
    )
    home_binding = None
    if figure8_profile:
        from step6_figure8_autotune_v1.live_composition import (
            load_figure8_home_binding,
        )

        home_binding = load_figure8_home_binding(
            run_dir / "home_start_receipt.json"
        )
    inputs.validate(
        contract=contract,
        parent_contract=admission_parent,
        home_binding=home_binding,
    )

    adapter_box: dict[str, Any] = {"path_sample_observer": None}

    def path_sink(sample: Any) -> None:
        adapter_box["adapter"].observe_r004_path_sample(sample)
        observer = adapter_box.get("path_sample_observer")
        if observer is not None:
            observer(sample)

    adapter: Any | None = None
    state20_trace: Any | None = None
    state21_trace: Any | None = None
    state25_trace: Any | None = None
    lifecycle_trace: LifecycleTrace | None = None
    path_entry_rate_limit_env = (
        R013_PATH_ENTRY_RATE_LIMIT_ENV in os.environ,
        os.environ.get(R013_PATH_ENTRY_RATE_LIMIT_ENV),
    )
    host_hard_tube_env = (
        "R008_HOST_HARD_TUBE" in os.environ,
        os.environ.get("R008_HOST_HARD_TUBE"),
    )
    try:
        # These are standing live-route settings requested for this campaign.
        os.environ["R008_TUBE_CBF_MODE"] = "active"
        # The historical moving-sphere hard tube is cycloid-specific.  The
        # Figure-eight profile installs its own moving-reference hard ellipse
        # below, so composing both would be a false stop surface.
        os.environ["R008_HOST_HARD_TUBE"] = "0" if figure8_profile else "1"
        # Reuse the mature R008 ramp; only this R013 context opts it in.
        os.environ[R013_PATH_ENTRY_RATE_LIMIT_ENV] = "1"
        if figure8_profile:
            from step6_figure8_autotune_v1.live_composition import (
                FigureEightPathEvidenceCollectorV1,
                FigureEightPathGuardStackV1,
                figure8_fresh_frame_wait_policy,
                figure8_motion_profile,
                figure8_path_provider,
                figure8_runtime_path_reference,
            )

            if home_binding is None:
                raise R013OwnerError(
                    "R013 Figure-eight path provider lacks its typed Home binding"
                )
            profile_provider = figure8_path_provider(
                anchor_pose=home_binding.profile.pose
            )
            profile_motion = figure8_motion_profile()
            profile_reference = figure8_runtime_path_reference
            profile_collector = FigureEightPathEvidenceCollectorV1
            fresh_frame_wait_policy = figure8_fresh_frame_wait_policy()
            requested_guard_stack: R012PathGuardStack = FigureEightPathGuardStackV1()
        else:
            profile_provider = None
            profile_motion = None
            profile_reference = None
            profile_collector = None
            fresh_frame_wait_policy = None
            requested_guard_stack = R012PathGuardStack()
        install_r013_runtime_patch(
            parsed_runtime_strategy,
            handoff_policy=(
                handoff_policy
                if handoff_policy is not None
                else binding_campaign.handoff_policy
                if binding_campaign is not None
                else None
            ),
            path_provider=profile_provider,
            feedforward_mode=feedforward_profile.mode,
            solver_profile=solver_profile,
        )
        verified = build_verified_mature_r006_writer(
            inputs,
            contract=contract,
            parent_contract=admission_parent,
            path_sample_sink=path_sink,
            motion_profile=profile_motion,
            path_reference=profile_reference,
            path_evidence_collector_type=profile_collector,
            home_binding=home_binding,
            fresh_frame_wait_policy=fresh_frame_wait_policy,
            qualification_profile=(
                None
                if figure8_profile or not r013_profile_bound
                else baseline_transition_profile
            ),
        )
        from step5d_autotune_v4_r008.state20_search_trace import attach_state20_trace
        from step5d_autotune_v4_r008.state25_path_trace import attach_state25_trace
        from .state21_baseline_trace import attach_state21_trace

        # Keep the complete observation ring in-process, but persist this
        # diagnostic sidecar at 10 Hz.  Full-rate JSON encoding in a background
        # thread can still contend for the GIL with the 500 Hz qualification
        # loop and trip the unchanged 80 ms freshness boundary.
        attach_state20_trace(verified.writer, run_dir, persistence_stride=50)
        state20_trace = getattr(verified.writer, "_state20_trace", None)
        # State21 is a separate baseline segment.  Persist its bounded
        # diagnostic view at the same cadence as SEARCH/PATH; the binary
        # lifecycle trace below remains the authoritative full-rate artifact.
        attach_state21_trace(verified.writer, run_dir, persistence_stride=50)
        state21_trace = getattr(verified.writer, "_state21_trace", None)
        # Keep the full-rate current attempt in memory for the anti-windup
        # receipt, while persisting this observation-only diagnostic at 10 Hz
        # so JSON serialization cannot starve the 500 Hz control loop.
        state25_trace = attach_state25_trace(
            verified.writer,
            run_dir,
            persistence_stride=50,
        )
        # This is the canonical complete trajectory.  It is attached to the
        # same single R006 writer, so every fresh packet observed by the
        # motion path is recorded without creating a second writer/transport.
        lifecycle_trace = attach_lifecycle_trace(
            verified.writer,
            run_dir,
            path_min_duration_s=selected_path_profile.duration_s - 0.5,
        )
        verified.writer._readable_runtime_identity = R013_READABLE_IDENTITY
        # The mature writer remains the sole motion owner.  This additive
        # controller is only the typed register35/36 PATH-end seam; it is
        # armed per attempt below and never changes the controller law.
        if not figure8_profile:
            verified.writer._r013_path_early_end_controller = path_early_end

        def _install_timing_scheduler_lease() -> FormalTimingSchedulerLeaseV2:
            lease = FormalTimingSchedulerLeaseV2(selected_timing_scheduler_profile)
            installer = getattr(verified.writer, "install_timing_scheduler_lease", None)
            if not callable(installer):
                raise R013OwnerError("R013 writer lacks the timing scheduler lease seam")
            installer(lease)
            return lease

        def _prepare_timing_scheduler_lease() -> None:
            preparer = getattr(verified.writer, "prepare_timing_scheduler_lease", None)
            if not callable(preparer):
                raise R013OwnerError(
                    "R013 writer lacks the pre-ARM GC preparation seam"
                )
            preparer()

        def _release_timing_scheduler_lease(
            lease: FormalTimingSchedulerLeaseV2,
        ) -> Mapping[str, Any]:
            releaser = getattr(verified.writer, "release_timing_scheduler_lease", None)
            if not callable(releaser):
                raise R013OwnerError("R013 writer lacks the timing scheduler restore seam")
            receipt = releaser()
            if isinstance(receipt, Mapping):
                return dict(receipt)
            return lease.receipt()

        class R013MatureWriter(R008MatureWriter):
            @property
            def fresh_frame_wait_policy(self) -> Any:
                return self.writer.fresh_frame_wait_policy

            @staticmethod
            def _kind(kind: str) -> AttemptKind:
                return _r013_attempt_kind(kind)

        class R013ProfileLiveWriterAdapter(R008LiveWriterAdapter):
            """Profile-aware metric conversion over the one mature writer."""

            def apply_complete_candidate(self, candidate: Mapping[str, Any]) -> Mapping[str, Any]:
                if not figure8_profile:
                    raise R013OwnerError(
                        "R013 complete candidate binding is Figure-eight-only"
                    )
                return configure_r013_figure8_candidate(
                    candidate,
                    fingerprint_sha256=physical_fingerprint,
                )

            def run_profile(self, attempt: Attempt, profile: R013PathProfileV1) -> AttemptResult:
                if profile.profile_id != R013PathProfileV1.figure8().profile_id:
                    return self.run_60s(attempt)
                kind = getattr(attempt, "kind", None)
                kind_s = kind.value if hasattr(kind, "value") else str(kind or "")
                if kind_s == "QUALIFICATION":
                    return super().run_60s(attempt)
                from step5d_autotune_v4_r004.evidence import AttemptEvidence
                from step5d_autotune_v4_r005.live_adapter import (
                    R005LiveAdapterError,
                    _bounded_attempt_diagnostics,
                )
                from step6_figure8_autotune_v1 import make_metric_result

                self._path_samples.clear()
                self._last_joint_evidence = None
                self._last_rtde_frame_identity = None
                self._rtde_sequence = 0
                raw = _run_mature_profile_path(self.writer, attempt, profile)
                if isinstance(raw, AttemptResult):
                    return raw
                if not isinstance(raw, AttemptEvidence):
                    raise R005LiveAdapterError(
                        "Figure-eight mature writer returned unknown evidence"
                    )
                metric = make_metric_result(
                    {
                        "time_s": sample.path_time_s,
                        "normal_load_n": sample.filtered_normal_n,
                    }
                    for sample in self._path_samples
                )
                joint_binding = (
                    None
                    if self._last_joint_evidence is None
                    else self._last_joint_evidence.as_dict()
                )
                metrics = {
                    "mature_evidence_sha256": raw.evidence_sha256,
                    "raw_path_sample_count": len(self._path_samples),
                    "fresh_frame_wait_policy": self.writer.fresh_frame_wait_policy.as_dict(),
                    "figure8_metric_result": metric.as_dict(),
                    "force_objective": {
                        "schema": "step6.autotune/figure8-force-objective-view-v1",
                        "metric_id": "figure8-force-mae-v1",
                        "target_force_n": 5.0,
                        "formal_window_s": [profile.formal_start_s, profile.formal_end_s],
                        "required_bins": profile.required_bins,
                        "complete_bins": metric.observed_formal_bin_count,
                        "formal_mae_n": metric.formal_mae_n,
                        "full_curve_mae_n": metric.full_curve_mae_n,
                        "transient_mae_n": metric.transient_mae_n,
                        "raw_measured_force": True,
                    },
                }
                metrics.update(_bounded_attempt_diagnostics(raw))
                if joint_binding is not None:
                    metrics["joint_velocity_binding"] = joint_binding
                return AttemptResult(
                    epoch=attempt.epoch,
                    attempt_sequence=attempt.attempt_sequence,
                    kind=attempt.kind,
                    candidate=attempt.candidate,
                    safe_return=raw.return_gate_passed,
                    binding_ok=joint_binding is not None,
                    safety_gate=raw.safety_gate_passed,
                    contact_gate=raw.contact_gate_passed,
                    return_gate=raw.return_gate_passed,
                    motion_gate=raw.motion_gate_passed,
                    timing_gate=raw.timing_gate_passed,
                    identity_gate=True,
                    qualification_passed=False,
                    duration_s=float(raw.path_duration_s or 0.0),
                    force_objective=None,
                    alignment_ok=joint_binding is not None,
                    metrics=metrics,
                    execution_id=attempt.execution_id,
                    joint_evidence=self._last_joint_evidence,
                    raw_path_samples=tuple(self._path_samples),
                )

        class R013ProfileRuntimePort(R005LiveRuntimePort):
            def run_profile(
                self, attempt: Attempt, profile: R013PathProfileV1
            ) -> AttemptResult:
                method = getattr(self.writer, "run_profile", None)
                if not callable(method):
                    raise R013OwnerError("R013 writer lacks the typed profile seam")
                result = method(attempt, profile)
                if not isinstance(result, AttemptResult):
                    raise R013OwnerError("R013 profile returned an untyped result")
                return result

            def apply_complete_candidate(self, candidate: Mapping[str, Any]) -> Mapping[str, Any]:
                method = getattr(self.writer, "apply_complete_candidate", None)
                if not callable(method):
                    raise R013OwnerError("R013 writer lacks complete-candidate ARM binding")
                return method(candidate)

        mature: R006MatureWriter = R013MatureWriter(
            verified.writer, injection=verified.injection
        )
        if figure8_profile:
            r012_guard_stack = _bind_r012_guard_stack(mature, requested_guard_stack)
        else:
            r012_guard_stack = _bind_r012_guard_stack(mature)
        # Keep R008's QUAL conversion: the frozen R005 adapter discards the
        # layered timing decision and makes a false timing gate impossible to
        # diagnose.  R013 otherwise keeps the same mature writer and contract.
        if figure8_profile:
            adapter = R013ProfileLiveWriterAdapter(mature, contract=published_parent)
        else:
            adapter = R008LiveWriterAdapter(mature, contract=published_parent)
        adapter_box["adapter"] = adapter
        verified.writer._controller_transport = register_writer
        _require_r012_guard_stack(mature, r012_guard_stack)
        physical_ledger = _open_physical_ledger(
            Path(ready["ledger_path"]),
            campaign_fingerprint=physical_fingerprint,
            eoat_sha256=parent_inputs.eoat_sha256,
            path_profile=selected_path_profile,
        )
        if binding_campaign is not None and (
            binding_campaign.runtime_strategy_sha256 != strategy_identity
        ):
            raise R013OwnerError("R013 runtime strategy campaign identity differs")
        strategy_sidecar = None
        if not figure8_profile:
            strategy_sidecar = RuntimeStrategyReceiptLedger(
                run_dir / "r013-runtime-strategy-receipts.jsonl",
                campaign_id=campaign_id,
                run_id=run_id,
                attempt_id=attempt_id,
                strategy=parsed_runtime_strategy,
                must_exist=bool(
                    binding_campaign is not None
                    and (
                        binding_campaign.observations
                        or binding_campaign.rejected_admissions
                    )
                ),
            )
        # A V4 stage uses a fresh manual-canary compatibility campaign only
        # for resident/source binding; its physical rows belong to the
        # stage-local ledger, not to the compatibility campaign's optimizer
        # dispatch list.  Only strict/formal campaigns may cold-join every
        # strategy receipt back to campaign records here.
        campaign_has_optimizer_records = bool(
            binding_campaign is not None
            and (
                binding_campaign.observations
                or binding_campaign.rejected_admissions
            )
        )
        if binding_campaign is not None and strategy_sidecar is not None and campaign_has_optimizer_records:
            strategy_sidecar.validate_campaign_records(binding_campaign.ledger.records)
            _validate_strategy_physical_bindings(
                strategy_sidecar=strategy_sidecar,
                campaign=binding_campaign,
                physical_records=physical_ledger.records,
            )
        # Bind the legacy owner qualification only when the selected lineage
        # requests it.  Figure-eight V5 intentionally runs directly from the
        # already accepted Home/package/safety gates; it must not inherit the
        # historical three-qualification resident loop.
        physical_ledger.fresh_process_verify()
        # Only after every campaign/strategy/physical cold join succeeds may
        # the mature writer acquire live authority or open RTDE transports.
        adapter.open(live_ack=R005_LIVE_ACK)
        runtime = (
            R013ProfileRuntimePort(adapter)
            if figure8_profile
            else R005LiveRuntimePort(adapter)
        )
        sequence = max((record.attempt_sequence for record in physical_ledger.records), default=0)
        epoch = int(ready["session_epoch"])
        qualification_records: tuple[Any, ...] = ()
        active_gc_window: R013GCWindowReceipt | None = None
        if run_epoch_qualifications:
            seed_source = _read_json(run_dir / "r012_seed_source.json")
            incumbent = seed_source.get("confirmed_incumbent")
            seed_candidate = incumbent.get("candidate") if isinstance(incumbent, Mapping) else None
            if not isinstance(seed_candidate, Mapping):
                raise R013OwnerError("R013 owner qualification seed candidate is missing")
            qualification_controller = {
                **dict(seed_candidate),
                "force_i_gain": KI_LATTICE_ANCHOR,
                "i_off": False,
                "target_force_n": 5.0,
            }
            if figure8_profile:
                from step6_figure8_autotune_v1 import CompleteCandidateV1
                from step6_figure8_autotune_v1.physical_candidate import (
                    FigureEightPhysicalCandidateV1,
                )

                qualification_candidate = FigureEightPhysicalCandidateV1.from_canonical(
                    qualification_controller
                )
                qualification_complete_candidate = CompleteCandidateV1(
                    controller_path=qualification_candidate.canonical,
                    correction_weights=(0.0,) * 6,
                ).as_dict()
            else:
                qualification_candidate = R006Candidate.from_canonical(
                    qualification_controller
                )
                qualification_complete_candidate = None
            current_records, qualification_records = _epoch_qualification_records(
                physical_ledger.records,
                epoch=epoch,
            )
            runtime.sync_qualification_passes(len(qualification_records))
        while run_epoch_qualifications and len(qualification_records) < 3:
            previous_count = len(qualification_records)
            sequence += 1
            qualification = Attempt(
                epoch=epoch,
                attempt_sequence=sequence,
                candidate=qualification_candidate,
                kind="QUALIFICATION",
                dispatch_sequence=sequence,
                request_uid=f"r013-owner-qualification-{sequence}",
                logical_request_uid=f"r013-owner-qualification-{sequence}",
                execution_id=f"r013-owner-qualification-{sequence}-{uuid.uuid4().hex}",
            )
            ticket = {
                "schema": "step5d.autotune-v4/r013-owner-qualification-ticket-v1",
                "attempt_sequence": sequence,
            }
            runtime.home()
            # State25 trace rows are attempt-scoped.  The owner qualification
            # loop has the same verified Home boundary as ordinary trials, so
            # establish it before the first state-25 observation rather than
            # letting the trace reject the next qualification ordinal.
            state21_trace.begin_attempt(sequence)
            state25_trace.begin_attempt(sequence)
            lifecycle_trace.begin_attempt(
                sequence,
                qualification.execution_id,
                qualification.kind,
                epoch=qualification.epoch,
                path_requested=False,
            )
            timing_scheduler_receipt = None
            gc_window = R013GCWindowReceipt.capture()
            active_gc_window = gc_window
            try:
                try:
                    gc_window.enter()
                    _prepare_timing_scheduler_lease()
                    # Qualification runs are full 60 s PATH trials too.  Arm the
                    # normal sequence-matched completion request before dispatch;
                    # active censoring is not enabled for this warm-up lane.
                    if not figure8_profile:
                        path_early_end.arm(sequence)
                    runtime.dispatch(qualification, ticket)
                    if qualification_complete_candidate is not None:
                        runtime.apply_complete_candidate(qualification_complete_candidate)
                    timing_lease = _install_timing_scheduler_lease()
                    try:
                        runtime.arm(qualification)
                        verified.writer.record_arm_transition_marker("runtime_arm_returned")
                        # R008 deliberately evaluates fresh cadence without the obsolete
                        # per-layer average-rate veto.  Keep this scope narrow to the
                        # qualification call that constructs and converts its evidence.
                        with r008_timing_scope():
                            qualification_result = _run_r013_profile(runtime, qualification, selected_path_profile)
                        qualification_result = runtime.safe_return(qualification, qualification_result)
                    finally:
                        timing_scheduler_receipt = _release_timing_scheduler_lease(timing_lease)
                finally:
                    try:
                        gc_window.restore_after_timing_lease()
                    finally:
                        setattr(verified.writer, "_prearm_gc_collected", False)
                        if active_gc_window is gc_window:
                            active_gc_window = None
                lifecycle_receipt = _finalize_r013_lifecycle(
                    lifecycle_trace,
                    verified.writer,
                    result=qualification_result,
                )
                lifecycle_receipt["timing_scheduler"] = timing_scheduler_receipt
                lifecycle_receipt["gc_window"] = gc_window.as_dict()
                qualification_arm_telemetry = tuple(
                    dict(item)
                    for item in getattr(verified.writer, "arm_transition_telemetry", ())
                )
                from step5d_autotune_v4_r004.arm_transition import classify_arm_transition
                lifecycle_receipt["arm_transition_telemetry"] = list(qualification_arm_telemetry)
                lifecycle_receipt["arm_transition_markers"] = list(
                    getattr(verified.writer, "arm_transition_markers", ())
                )
                lifecycle_receipt["arm_transition_classification"] = classify_arm_transition(
                    qualification_arm_telemetry,
                    markers=getattr(verified.writer, "arm_transition_markers", ()),
                )
                qualification_receipt_path = lifecycle_receipt.get("receipt_path")
                if isinstance(qualification_receipt_path, str) and qualification_receipt_path:
                    destination = Path(qualification_receipt_path)
                    temporary = destination.with_suffix(destination.suffix + ".part")
                    with temporary.open("w", encoding="utf-8") as stream:
                        json.dump(lifecycle_receipt, stream, sort_keys=True, indent=2, allow_nan=False)
                        stream.write("\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, destination)
            except Exception as exc:
                failed_receipt = _finalize_r013_lifecycle(
                    lifecycle_trace,
                    verified.writer,
                    error=f"qualification execution failed: {exc}",
                )
                failed_telemetry = tuple(
                    dict(item)
                    for item in getattr(verified.writer, "arm_transition_telemetry", ())
                )
                from step5d_autotune_v4_r004.arm_transition import classify_arm_transition
                failed_receipt = {
                    **failed_receipt,
                    "timing_scheduler": timing_scheduler_receipt,
                    "gc_window": gc_window.as_dict(),
                    "arm_transition_telemetry": list(failed_telemetry),
                    "arm_transition_markers": list(
                        getattr(verified.writer, "arm_transition_markers", ())
                    ),
                    "arm_transition_classification": classify_arm_transition(
                        failed_telemetry,
                        markers=getattr(verified.writer, "arm_transition_markers", ()),
                    ),
                }
                failed_path = failed_receipt.get("receipt_path")
                if isinstance(failed_path, str) and failed_path:
                    destination = Path(failed_path)
                    temporary = destination.with_suffix(destination.suffix + ".part")
                    with temporary.open("w", encoding="utf-8") as stream:
                        json.dump(failed_receipt, stream, sort_keys=True, indent=2, allow_nan=False)
                        stream.write("\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, destination)
                if not figure8_profile:
                    path_early_end.handshake.reset_for_next_arm()
                raise
            if not figure8_profile:
                path_early_end.handshake.reset_for_next_arm()
            qualification_result = _apply_r013_qualification_timing_acceptance(
                qualification_result
            )
            qualification_result = _apply_r013_lifecycle_gate(
                qualification_result,
                lifecycle_receipt,
            )
            if not bool(lifecycle_receipt.get("coverage_complete")):
                # Preserve the raw/force evidence as a quarantined physical
                # row, but never let an incomplete lifecycle qualify the
                # resident epoch or enter tell_exact.
                physical_ledger.append(
                    qualification_result.to_record(physical_fingerprint)
                )
                physical_ledger.fresh_process_verify()
                raise R013OwnerError(
                    "R013 owner qualification lifecycle coverage is incomplete: "
                    + json.dumps(lifecycle_receipt, sort_keys=True)
                )
            _qualification_flow_gates(qualification_result)
            physical_ledger.append(
                qualification_result.to_record(physical_fingerprint)
            )
            physical_ledger.fresh_process_verify()
            current_records, qualification_records = _epoch_qualification_records(
                physical_ledger.records,
                epoch=epoch,
            )
            if len(qualification_records) != previous_count + 1:
                raise R013OwnerError("R013 owner qualification cold-read binding differs")
            runtime.sync_qualification_passes(len(qualification_records))
        if run_epoch_qualifications and len(qualification_records) != 3:
            raise R013OwnerError("R013 owner qualification count differs from three")

        safe_return_overlap: dict[str, Callable[[], None] | None] = {
            "callback": None
        }
        safe_return_overlap_errors: list[str] = []

        def configure_path_sample_observer(
            callback: Callable[[Any], None] | None,
        ) -> None:
            if callback is not None and not callable(callback):
                raise R013OwnerError("R013 path sample observer is not callable")
            adapter_box["path_sample_observer"] = callback

        def configure_active_censor(censor: Any | None) -> None:
            if censor is not None and not hasattr(censor, "finalize"):
                raise R013OwnerError("R013 active censor runtime is not typed")
            adapter_box["active_censor"] = censor

        def configure_safe_return_overlap(
            callback: Callable[[], None] | None,
        ) -> None:
            if not figure8_profile:
                raise R013OwnerError(
                    "R013 safe-return overlap is Figure-eight-only"
                )
            if callback is not None and not callable(callback):
                raise R013OwnerError(
                    "R013 safe-return overlap callback is not callable"
                )
            safe_return_overlap["callback"] = callback

        def configure_handoff_at_home(policy: Any) -> Mapping[str, Any]:
            if not figure8_profile:
                raise R013OwnerError(
                    "R013 dynamic handoff selection is Figure-eight A/B-only"
                )
            runtime.home()
            return configure_r013_handoff_policy(policy)

        def safe_home_permitted(failure: Any) -> bool:
            """Owner capability: protective/raw anomaly stops cannot Home."""

            failure_class = getattr(getattr(failure, "failure_class", None), "value", None)
            return not bool(getattr(failure, "safety_fault", False)) and failure_class not in {
                "protective_stop",
                "emergency_stop",
                "raw_sensor_anomaly",
                "joint_anomaly",
                "home_unsafe",
            }

        def safe_home_after_failure() -> Mapping[str, Any]:
            runtime.home()
            return {
                "schema": "step5d.autotune-v4/r013-recovery-home-receipt-v1",
                "state": "HOME",
                "home_verified": True,
                "resident_epoch": epoch,
            }

        def run_trial(dispatch: Any) -> Mapping[str, Any]:
            nonlocal sequence, active_gc_window
            _require_r012_guard_stack(mature, r012_guard_stack)
            sequence += 1
            dispatch_candidate = dict(dispatch.candidate)
            if figure8_profile:
                from step6_figure8_autotune_v1 import CompleteCandidateV1
                from step6_figure8_autotune_v1.physical_candidate import (
                    FigureEightPhysicalCandidateV1,
                )

                complete_candidate = CompleteCandidateV1.from_mapping(
                    dispatch_candidate
                )
                candidate = FigureEightPhysicalCandidateV1.from_canonical(
                    complete_candidate.controller_path
                )
            else:
                candidate = R006Candidate.from_canonical(dispatch_candidate)
            attempt = Attempt(
                epoch=int(ready["session_epoch"]),
                attempt_sequence=sequence,
                candidate=candidate,
                kind=str(dispatch.kind),
                dispatch_sequence=sequence,
                request_uid=str(dispatch.dispatch_id),
                logical_request_uid=str(dispatch.dispatch_id),
                execution_id=f"r013-{dispatch.dispatch_id}-{uuid.uuid4().hex}",
            )
            runtime.home()
            # Reset/flush the previous diagnostic trace while the robot is at
            # verified Home.  Doing this lazily from the first state-25 sample
            # blocks the 500 Hz motion thread and manufactures a 30+ ms timing
            # gap on every PATH after the first.
            state21_trace.begin_attempt(sequence)
            state25_trace.begin_attempt(sequence)
            lifecycle_trace.begin_attempt(
                sequence,
                attempt.execution_id,
                attempt.kind,
                epoch=attempt.epoch,
                path_requested=True,
            )
            attempt_boundary = _materialize_attempt_boundary_receipt(
                run_dir=run_dir,
                sequence=sequence,
                dispatch_id=str(dispatch.dispatch_id),
                candidate_key=str(getattr(dispatch, "candidate_key", "")),
                trace_paths=(
                    Path(state21_trace.sidecar_path),
                    Path(state25_trace.sidecar_path),
                    Path(lifecycle_trace.active_data_path or lifecycle_trace.run_dir),
                ),
            )
            timing_scheduler_receipt = None
            gc_window = R013GCWindowReceipt.capture()
            active_gc_window = gc_window
            try:
                try:
                    gc_window.enter()
                    _prepare_timing_scheduler_lease()
                    if not figure8_profile:
                        active_censor_for_arm = adapter_box.get("active_censor")
                        if active_censor_for_arm is None:
                            path_early_end.arm(sequence)
                        else:
                            bind_physical_attempt = getattr(
                                active_censor_for_arm, "bind_physical_attempt", None
                            )
                            if not callable(bind_physical_attempt):
                                raise R013OwnerError(
                                    "R013 active censor lacks physical-sequence binding seam"
                            )
                            bind_physical_attempt(sequence)
                    runtime.dispatch(attempt, dispatch)
                    _apply_profile_candidate(runtime, dispatch_candidate, selected_path_profile)
                    timing_lease = _install_timing_scheduler_lease()
                    try:
                        runtime.arm(attempt)
                        verified.writer.record_arm_transition_marker("runtime_arm_returned")
                        result = _run_r013_profile(runtime, attempt, selected_path_profile)
                        overlap_callback = safe_return_overlap.get("callback")
                        if overlap_callback is not None:
                            try:
                                overlap_callback()
                            except Exception as overlap_error:
                                # Safe return always outranks optimization overlap.
                                # The caller can inspect this typed diagnostic after
                                # the robot is back at Home and fall back to a
                                # synchronous proposal.
                                safe_return_overlap_errors.append(
                                    f"{type(overlap_error).__name__}: {overlap_error}"
                                )
                        result = runtime.safe_return(attempt, result)
                    finally:
                        timing_scheduler_receipt = _release_timing_scheduler_lease(timing_lease)
                finally:
                    try:
                        gc_window.restore_after_timing_lease()
                    finally:
                        setattr(verified.writer, "_prearm_gc_collected", False)
                        if active_gc_window is gc_window:
                            active_gc_window = None
                arm_transition_telemetry = tuple(
                    dict(item)
                    for item in getattr(
                        verified.writer, "arm_transition_telemetry", ()
                    )
                )
                try:
                    from step5d_autotune_v4_r004.arm_transition import (
                        classify_arm_transition,
                    )
                except ModuleNotFoundError:  # pragma: no cover
                    from tools.step5d_autotune_v4_r004.arm_transition import (
                        classify_arm_transition,
                    )
                arm_transition_classification = classify_arm_transition(
                    arm_transition_telemetry,
                    markers=getattr(verified.writer, "arm_transition_markers", ()),
                )
                state21_trace.flush()
                lifecycle_receipt = _finalize_r013_lifecycle(
                    lifecycle_trace,
                    verified.writer,
                    result=result,
                )
                lifecycle_receipt["gc_window"] = gc_window.as_dict()
            except Exception as exc:
                failed_receipt = _finalize_r013_lifecycle(
                    lifecycle_trace,
                    verified.writer,
                    error=f"trial execution failed: {exc}",
                )
                failed_telemetry = tuple(
                    dict(item)
                    for item in getattr(verified.writer, "arm_transition_telemetry", ())
                )
                try:
                    from step5d_autotune_v4_r004.arm_transition import (
                        classify_arm_transition,
                    )
                except ModuleNotFoundError:  # pragma: no cover
                    from tools.step5d_autotune_v4_r004.arm_transition import (
                        classify_arm_transition,
                    )
                failed_receipt = {
                    **failed_receipt,
                    "timing_scheduler": timing_scheduler_receipt,
                    "gc_window": gc_window.as_dict(),
                    "arm_transition_telemetry": list(failed_telemetry),
                    "arm_transition_markers": list(
                        getattr(verified.writer, "arm_transition_markers", ())
                    ),
                    "arm_transition_classification": classify_arm_transition(
                        failed_telemetry,
                        markers=getattr(verified.writer, "arm_transition_markers", ()),
                    ),
                }
                failed_path = failed_receipt.get("receipt_path")
                if isinstance(failed_path, str) and failed_path:
                    destination = Path(failed_path)
                    temporary = destination.with_suffix(destination.suffix + ".part")
                    with temporary.open("w", encoding="utf-8") as stream:
                        json.dump(failed_receipt, stream, sort_keys=True, indent=2, allow_nan=False)
                        stream.write("\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, destination)
                if not figure8_profile:
                    path_early_end.handshake.reset_for_next_arm()
                raise
            handoff = getattr(active_runtime(), "_r013_handoff", None)
            handoff_receipt = handoff.receipt() if handoff is not None else None
            state21_rows = state21_trace.attempt_rows(sequence)
            lifecycle_receipt = {
                **lifecycle_receipt,
                "handoff": handoff_receipt,
                "attempt_boundary": attempt_boundary,
                "arm_transition_telemetry": list(arm_transition_telemetry),
                "arm_transition_markers": list(
                    getattr(verified.writer, "arm_transition_markers", ())
                ),
                "arm_transition_classification": arm_transition_classification,
                "timing_scheduler": timing_scheduler_receipt,
                "state21_baseline": {
                    "attempt_row_count": len(state21_rows),
                    "sidecar_rows_written": int(state21_trace.rows_written),
                    "sidecar_dropped": int(state21_trace.sidecar_dropped),
                    "sidecar_path": str(state21_trace.sidecar_path),
                },
            }
            # The mature flow gate can fail before ``run_trial`` returns. Do
            # not lose the in-memory ARM poll evidence in that path: the
            # robot is already at verified Home, so this is terminal receipt
            # sealing rather than hot-path I/O.
            receipt_path = lifecycle_receipt.get("receipt_path")
            if isinstance(receipt_path, str) and receipt_path:
                try:
                    destination = Path(receipt_path)
                    temporary = destination.with_suffix(destination.suffix + ".part")
                    with temporary.open("w", encoding="utf-8") as stream:
                        json.dump(lifecycle_receipt, stream, sort_keys=True, indent=2, allow_nan=False)
                        stream.write("\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, destination)
                except OSError as exc:
                    lifecycle_receipt["arm_transition_receipt_write_error"] = str(exc)
            active_censor = adapter_box.get("active_censor")
            censored_observation = None
            if active_censor is not None:
                censored_observation = active_censor.finalize(
                    dispatch_id=str(dispatch.dispatch_id),
                    return_guard=bool(getattr(result, "return_gate", False)),
                    home=bool(getattr(result, "safe_return", False)),
                    safe_return=bool(getattr(result, "safe_return", False)),
                )
            if not figure8_profile:
                # Keep the in-memory sequence gate closed between attempts;
                # the next arm writes register35=0 before any new PATH.
                path_early_end.handshake.reset_for_next_arm()
            if censored_observation is None:
                result = _apply_r013_lifecycle_gate(
                    result,
                    lifecycle_receipt,
                    require_handoff=True,
                )
            result_metrics = dict(getattr(result, "metrics", {}) or {})
            result_metrics["hard_tube_passed"] = bool(
                getattr(mature.writer, "_host_hard_tube", None) is not None
            )
            result_metrics["soft_tube_passed"] = bool(
                getattr(
                    getattr(mature.writer, "_qualification_control", None),
                    "_tube_cbf",
                    None,
                )
                is not None
            )
            if r013_profile_bound:
                admission_decision = motion_admission_profile.evaluate(
                    replace(result, metrics=result_metrics)
                )
                result = replace(
                    result,
                    motion_gate=admission_decision.motion_gate,
                    metrics={
                        **result_metrics,
                        "r013_motion_admission": admission_decision.as_dict(),
                    },
                )
            if censored_observation is None:
                _fresh_trial_flow_gates(
                    result,
                    profile=selected_path_profile,
                    allow_timing_ineligible=bool(
                        binding_campaign is not None
                        and binding_campaign.bounded_bo_profile is not None
                    ),
                )
            # A V4 active-censor row is intentionally partial/non-trainable.
            # Keep its immutable lifecycle/raw trace and stage-ledger receipt,
            # but do not force it through the exact-observation ledger, whose
            # append contract requires a fresh 550-bin objective artifact.
            sealed = (
                None
                if censored_observation is not None
                else physical_ledger.append(result.to_record(physical_fingerprint))
            )
            mark_r013_safe_return_transition(
                active_runtime(),
                safe_return_verified=True,
            )
            if run_epoch_qualifications:
                _current_records, qualification_records = _epoch_qualification_records(
                    physical_ledger.records,
                    epoch=epoch,
                )
            else:
                current_records = tuple(
                    record
                    for record in physical_ledger.records
                    if int(record.epoch) == epoch
                )
                if any(record.kind == "QUALIFICATION" for record in current_records):
                    raise R013OwnerError(
                        "qualification-free Figure-eight context received a qualification row"
                    )
                qualification_records = ()
            if censored_observation is not None:
                admission = None
            elif figure8_profile:
                # The Figure-eight lineage has an independently typed,
                # continuous physical box (including conditional outward
                # probes).  Reusing R013's quarter-octave key function here
                # would reject valid Step6 observations after motion.  Bind
                # the receipt directly to the cold-read Figure-eight UID.
                admission = PhysicalAdmissionReceipt(
                    dispatch_id=str(dispatch.dispatch_id),
                    candidate_token=str(dispatch.candidate_token),
                    candidate_key=(str(sealed.candidate_uid),),
                    attempt_sequence=int(sealed.attempt_sequence),
                    execution_id=str(sealed.metrics.get("execution_id", "")),
                    sealed_mae_n=float(sealed.mae_n),
                    physical_eligible=bool(sealed.eligible),
                    timing_gate=bool(sealed.timing_gate),
                    motion_gate=bool(sealed.motion_gate),
                    # PhysicalAdmissionReceipt is a shared R013 boundary
                    # type; V5 no longer carries a qualification state in its
                    # own ledger, so this legacy compatibility bit is false.
                    qualification_passed=False,
                    observation_uid=str(sealed.observation_uid),
                    sealed=bool(sealed.sealed),
                    epoch_qualification_passed=(
                        len(qualification_records) == 3
                        if run_epoch_qualifications
                        else None
                    ),
                    trial_admission_passed=bool(sealed.eligible),
                    campaign_fingerprint=None,
                )
            elif binding_campaign is not None and (
                binding_campaign.strict_admission
                or binding_campaign.bounded_bo_profile is not None
            ):
                if binding_campaign.strict_admission:
                    admission = _strict_physical_admission_from_record(
                        dispatch=dispatch,
                        record=sealed,
                        qualification_records=qualification_records,
                        campaign_fingerprint=binding_campaign.campaign_fingerprint,
                    )
                else:
                    admission = PhysicalAdmissionReceipt.from_ledger_record(
                        dispatch=dispatch,
                        record=sealed,
                        campaign_fingerprint=binding_campaign.campaign_fingerprint,
                    )
            else:
                admission = PhysicalAdmissionReceipt.from_ledger_record(
                    dispatch=dispatch,
                    record=sealed,
                )
            rows = _rows_for_attempt(state25_trace, ordinal=sequence)
            metrics = anti_windup_metrics_from_rows(
                rows, candidate=dispatch.candidate, runtime=active_runtime()
            )
            try:
                contact_transient = contact_transient_receipt(rows)
            except ContactTransientError as exc:
                # This is diagnostic evidence only.  Preserve explicit
                # invalid evidence without changing the sealed objective or
                # physical admission result.
                contact_transient = {
                    "schema": CONTACT_TRANSIENT_RECEIPT_SCHEMA,
                    "status": "invalid",
                    "error": str(exc),
                }
            contact_transient_sidecar = _write_contact_transient_sidecar(
                run_dir / CONTACT_TRANSIENT_SIDECAR_NAME,
                dispatch_id=str(dispatch.dispatch_id),
                attempt_sequence=sequence,
                receipt=contact_transient,
            )
            if censored_observation is not None:
                strategy_receipt = {
                    "schema": "step5d.autotune-v4/r013-censored-strategy-receipt-v1",
                    "status": "excluded_from_exact_strategy_fit",
                }
                strategy_sidecar_sha256 = None
            elif figure8_profile:
                strategy_receipt = figure8_correction_receipt_from_rows(
                    rows,
                    fingerprint_sha256=physical_fingerprint,
                    runtime=active_runtime(),
                    expected_duration_s=selected_path_profile.duration_s,
                )
                strategy_sidecar_sha256 = None
            else:
                strategy_receipt = runtime_strategy_receipt_from_rows(
                    rows,
                    strategy=parsed_runtime_strategy,
                    runtime=active_runtime(),
                    formal_start_s=selected_path_profile.formal_start_s,
                    formal_end_s=selected_path_profile.formal_end_s,
                    expected_duration_s=selected_path_profile.duration_s,
                )
                if strategy_sidecar is None:
                    raise R013OwnerError("R013 runtime strategy sidecar is missing")
                strategy_sidecar_row = strategy_sidecar.append(
                    dispatch_id=str(dispatch.dispatch_id),
                    physical_admission=admission.as_dict(),
                    runtime_strategy_receipt=strategy_receipt,
                )
                strategy_sidecar_sha256 = strategy_sidecar_row["row_sha256"]
            return {
                "schema": "step5d.autotune-v4/r013-trial-result-v1",
                "dispatch_id": str(dispatch.dispatch_id),
                "state": "HOME",
                "home": True,
                "safe_return": True,
                "physical_admission": admission,
                "timing_evidence": dict(
                    sealed.metrics.get("timing_evidence", {})
                    if isinstance(getattr(sealed, "metrics", None), Mapping)
                    else {}
                ),
                "legacy_timing_gate": bool(getattr(sealed, "timing_gate", False)),
                "censored_observation": censored_observation,
                "force_lifecycle": lifecycle_receipt,
                "attempt_boundary": attempt_boundary,
                "arm_transition_telemetry": list(arm_transition_telemetry),
                "arm_transition_classification": arm_transition_classification,
                "handoff": handoff_receipt,
                "state21_baseline": {
                    "schema": "step5d.autotune-v4/r013-state21-baseline-receipt-v1",
                    "attempt_row_count": len(state21_rows),
                    "sidecar_rows_written": int(state21_trace.rows_written),
                    "sidecar_dropped": int(state21_trace.sidecar_dropped),
                    "sidecar_path": str(state21_trace.sidecar_path),
                },
                "anti_windup": metrics,
                "contact_transient": contact_transient,
                "contact_transient_sidecar": str(contact_transient_sidecar),
                "runtime_strategy": strategy_receipt,
                "runtime_strategy_sidecar_sha256": strategy_sidecar_sha256,
                "feedforward_profile": feedforward_profile.as_dict(),
                "motion_admission_profile": motion_admission_profile.as_dict(),
                "baseline_transition_profile": baseline_transition_profile.as_dict()
                if not figure8_profile
                else None,
                "baseline_residual_policy": baseline_residual_policy.as_dict(),
                "soft_tube": {
                    "mode": "active",
                    "semi_axes_m": list(R012_SOFT_CBF_AXES_M),
                    "guard_bound": True,
                    "writer_filter": bool(getattr(getattr(verified.writer, "_qualification_control", None), "_tube_cbf", None)),
                },
                "hard_tube": {
                    "enabled": True,
                    "independent": True,
                    "checked_first": True,
                    "axes_m": list(R012_HARD_TUBE_AXES_M),
                },
            }

        def close() -> None:
            nonlocal active_gc_window
            try:
                try:
                    if state20_trace is not None:
                        state20_trace.close()
                except Exception:
                    pass
                try:
                    if state21_trace is not None:
                        state21_trace.close()
                except Exception:
                    pass
                try:
                    state25_trace.flush()
                except Exception:
                    pass
                try:
                    if lifecycle_trace is not None:
                        lifecycle_trace.close()
                except Exception:
                    pass
                adapter.close()
            finally:
                try:
                    if active_gc_window is not None:
                        active_gc_window.restore_after_timing_lease()
                        active_gc_window = None
                finally:
                    try:
                        physical_ledger.fresh_process_verify()
                    except Exception:
                        pass
                    try:
                        register_writer.close()
                    except Exception:
                        pass
                    try:
                        uninstall_r013_runtime_patch()
                    finally:
                        was_present, prior_value = path_entry_rate_limit_env
                        if was_present:
                            assert prior_value is not None
                            os.environ[R013_PATH_ENTRY_RATE_LIMIT_ENV] = prior_value
                        else:
                            os.environ.pop(R013_PATH_ENTRY_RATE_LIMIT_ENV, None)
                        host_was_present, host_prior_value = host_hard_tube_env
                        if host_was_present:
                            assert host_prior_value is not None
                            os.environ["R008_HOST_HARD_TUBE"] = host_prior_value
                        else:
                            os.environ.pop("R008_HOST_HARD_TUBE", None)

        return R013LiveContext(
            writer=mature,
            runtime=runtime,
            run_trial=run_trial,
            physical_ledger=physical_ledger,
            state20_trace=getattr(verified.writer, "_state20_trace", None),
            state21_trace=state21_trace,
            state25_trace=state25_trace,
            lifecycle_trace=lifecycle_trace,
            register_writer=register_writer,
            close=close,
            configure_handoff=configure_handoff_at_home,
            configure_safe_return_overlap=configure_safe_return_overlap,
            safe_return_overlap_errors=safe_return_overlap_errors,
            path_profile=selected_path_profile,
            feedforward_profile=feedforward_profile,
            motion_admission_profile=motion_admission_profile,
            path_early_end=path_early_end,
            configure_path_sample_observer=configure_path_sample_observer,
            configure_active_censor=configure_active_censor,
            timing_scheduler_profile=selected_timing_scheduler_profile.profile_id,
            resident_epoch=epoch,
            resident_ready=ready.get("status") == "resident_ready_no_arm",
            safe_home_permitted=safe_home_permitted,
            safe_home_after_failure=safe_home_after_failure,
        )
    except Exception as build_error:
        try:
            _cleanup_failed_live_build(
                adapter=adapter,
                state20_trace=state20_trace,
                state21_trace=state21_trace,
                state25_trace=state25_trace,
                lifecycle_trace=lifecycle_trace,
                register_writer=register_writer,
                path_entry_rate_limit_env=path_entry_rate_limit_env,
                host_hard_tube_env=host_hard_tube_env,
            )
        except Exception as cleanup_error:
            raise R013OwnerError(
                f"R013 live context build failed ({build_error}); cleanup also failed"
            ) from cleanup_error
        raise


__all__ = [
    "R013LiveContext", "R013OwnerError", "R013PathProfileV1", "_run_r013_profile", "_apply_profile_candidate", "_fresh_trial_flow_gates", "_cleanup_failed_live_build",
    "_apply_r013_qualification_timing_acceptance", "_epoch_qualification_records",
    "_strict_physical_admission_from_record", "_r013_attempt_kind",
    "build_r013_live_context",
]
