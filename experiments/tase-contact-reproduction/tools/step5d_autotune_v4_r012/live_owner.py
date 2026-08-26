"""Single R012 owner seam over the sibling R004/R006/R008 motion stack.

The caller supplies the already-admitted mature physical writer and its
one-trial runner through the stack context.  This module only binds the
current R012 PATH guard and protocol-612012 register surface; motion, TP,
seal, and Home remain owned by the mature runner.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping
import uuid

from .live_adapter import (
    R012LiveAdapterError,
    R012TrialExecutor,
    R012TrialResult,
    resolve_register_writer,
)
from .live_host import AutotuneRunMode
from .path_cbf_live import R012PathGuardStack, is_r012_guard_bound
from .serial_queue import QueueItem
from .compat_identity import r012_compat_contract
from .register_transport import R012LiveRTDETransport


ROOT = Path(__file__).resolve().parents[2]

R012_MOTION_PROTOCOL = 606006
R012_REGISTER_PROTOCOL = 612012
R012_REQUEST_REGISTER = 35
R012_ACK_REGISTER = 36
R012_MIN_CAPTURE_RATE_HZ = 300.0


class R012OwnerError(R012LiveAdapterError):
    """The direct R012 B3 owner context is incomplete."""


@dataclass(frozen=True)
class R012B3OwnerContext:
    """Already-admitted mature writer/runner pair for one physical trial."""

    writer: Any
    runner: Callable[[QueueItem], Mapping[str, Any] | R012TrialResult]
    register_writer: Any | None = None


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise R012OwnerError(f"R012 owner artifact is not an object: {path}")
    return value


def _open_r006_physical_ledger(
    path: Path,
    *,
    campaign_fingerprint: str,
    eoat_sha256: str,
) -> Any:
    """Cold-read the mature physical ledger with its native candidate type."""

    import step5d_autotune_v4_r005.observations as observations_module
    from step5d_autotune_v4_r008.bounded_resume_ledger import R008BoundedResumeObservationLedger
    from step5d_autotune_v4_r006.live_adapter import R006Candidate

    parent_candidate = observations_module.Candidate
    observations_module.Candidate = R006Candidate
    try:
        return R008BoundedResumeObservationLedger(
            path,
            campaign_fingerprint=campaign_fingerprint,
            eoat_sha256=eoat_sha256,
        )
    finally:
        observations_module.Candidate = parent_candidate


def _r012_fresh_timing_acceptance(result: Any) -> bool:
    """Accept complete fresh evidence despite dropped duplicate RTDE frames."""

    metrics = result.metrics if isinstance(getattr(result, "metrics", None), Mapping) else {}
    timing = metrics.get("timing_evidence")
    objective = metrics.get("force_objective")
    rates = timing.get("layer_rates_hz") if isinstance(timing, Mapping) else None
    if not all(isinstance(value, Mapping) for value in (timing, objective, rates)):
        return False
    try:
        return bool(
            float(result.duration_s) >= 60.0
            and int(objective["complete_bins"]) == 550
            and int(objective["required_bins"]) == 550
            and float(metrics["path_observed_span_s"]) >= 59.99
            and float(metrics["path_cadence_hz"]) >= 490.0
            and float(timing["feedback_age_p99_s"]) <= 0.01
            and float(timing["max_fresh_gap_s"]) <= 0.02
            and float(rates["writer_publishes"]) >= R012_MIN_CAPTURE_RATE_HZ
            and float(rates["rtde_frames"]) >= R012_MIN_CAPTURE_RATE_HZ
            and float(rates["tp_consumed_packet_echoes"]) >= R012_MIN_CAPTURE_RATE_HZ
            and float(rates["kunwei_frames"]) >= 900.0
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _apply_r012_timing_acceptance(result: Any) -> Any:
    if bool(result.timing_gate) or not _r012_fresh_timing_acceptance(result):
        return result
    metrics = dict(result.metrics)
    metrics["parent_timing_gate_passed"] = False
    metrics["timing_gate_passed"] = True
    metrics["r012_timing_acceptance"] = {
        "schema": "step5d.autotune-v4/r012-fresh-timing-acceptance-v1",
        "accepted": True,
        "minimum_capture_rate_hz": R012_MIN_CAPTURE_RATE_HZ,
        "requires_full_550_bins": True,
        "feedback_age_p99_max_s": 0.01,
        "max_fresh_gap_s": 0.02,
        "tp_stale_stop_unchanged": True,
    }
    return replace(result, timing_gate=True, metrics=metrics)


def _r012_force_objective_flow_gates_pass(result: Any) -> bool:
    """Keep motion/timing diagnostic while preserving physical safety gates."""

    return bool(
        result.safe_return
        and result.safety_gate
        and result.contact_gate
        and result.return_gate
        and result.identity_gate
    )


def _build_live_context(
    *,
    run_dir: Path,
    controller_host: str,
    kunwei_host: str,
    kunwei_port: int,
    launch_profile: Path,
    campaign_id: str,
    run_id: str,
    r012_attempt_id: str,
) -> R012B3OwnerContext:
    """Construct the mature physical writer from the current resident receipts."""

    register_writer = R012LiveRTDETransport(controller_host)
    register_writer.open()

    from step5d_autotune_v4_r005.live_adapter import (
        R005LiveInputs,
        R005LiveRuntimePort,
        R005LiveWriterAdapter,
        R005_LIVE_ACK,
    )
    from step5d_autotune_v4_r005.runtime import Attempt
    from step5d_autotune_v4_r006.live_adapter import (
        R006Candidate,
        R006LiveInputs,
        R006MatureWriter,
        build_verified_mature_r006_writer,
    )
    from step5d_autotune_v4_r006.parent import load_frozen_r005_contract
    from step5d_autotune_v4_r008.live_adapter import R008MatureWriter
    from step5d_autotune_v4_r004.wire import AttemptKind as R004AttemptKind
    from step5d_autotune_v4_r004_live_writer import LIVE_ACK as R004_LIVE_ACK

    ready = _read_json(run_dir / "r012_live_owner_ready.json")
    launch = _read_json(run_dir / "launch_context.json")
    if ready.get("status") != "resident_ready_no_arm":
        raise R012OwnerError("R012 resident is not READY/no-ARM")
    contract = r012_compat_contract()
    published_parent, admission_parent = load_frozen_r005_contract()
    expected_triplet = dict(launch["triplet"])
    parent_inputs = R005LiveInputs(
        controller_receipt=run_dir / "controller_receipt.json",
        script1_receipt=run_dir / "script1_receipt.json",
        runtime_evidence=run_dir / "runtime_evidence.json",
        runtime_attestation=run_dir / "runtime_evidence.json",
        optimizer_pointer=run_dir / "runtime_evidence.json",
        launch_profile_path=launch_profile,
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
    inputs.validate(contract=contract, parent_contract=admission_parent)

    adapter_box: dict[str, Any] = {}

    def path_sink(sample: Any) -> None:
        adapter_box["adapter"].observe_r004_path_sample(sample)

    verified = build_verified_mature_r006_writer(
        inputs,
        contract=contract,
        parent_contract=admission_parent,
        path_sample_sink=path_sink,
    )
    # Preserve the mature R008 raw diagnostic sidecars in the direct R012
    # composition.  They are observation-only and make any stop-dominant
    # packet reconstructable without adding a second bench reader/writer.
    from step5d_autotune_v4_r008.state20_search_trace import attach_state20_trace
    from step5d_autotune_v4_r008.state25_path_trace import attach_state25_trace

    attach_state20_trace(verified.writer, run_dir)
    # The raw force artifact remains full-rate.  Persist this observation-only
    # diagnostic at 10 Hz so JSON encoding cannot starve the 500 Hz writer.
    attach_state25_trace(verified.writer, run_dir, persistence_stride=50)
    # R012 publishes readable identity registers (revision, extension protocol)
    # instead of SHA-derived limbs.  The mature session gate still consumes its
    # admitted receipt identity; this explicit adapter controls only live echo
    # interpretation for the R012 writer instance.
    verified.writer._readable_runtime_identity = (12, R012_REGISTER_PROTOCOL)

    class R012MatureWriter(R008MatureWriter):
        @staticmethod
        def _kind(kind: str) -> R004AttemptKind:
            mapping = {
                "REFERENCE": R004AttemptKind.BATCH_A,
                "CHALLENGER_CONFIRM": R004AttemptKind.BATCH_A,
                "INCUMBENT_RETEST": R004AttemptKind.RETEST,
                "BO_TRIAL": R004AttemptKind.BATCH_B,
                "NOVEL_BO": R004AttemptKind.BATCH_B,
            }
            try:
                return mapping[kind]
            except KeyError as exc:
                raise R012OwnerError(f"unsupported R012 physical kind: {kind}") from exc

    mature: R006MatureWriter = R012MatureWriter(
        verified.writer,
        injection=verified.injection,
    )
    adapter = R005LiveWriterAdapter(mature, contract=published_parent)
    adapter_box["adapter"] = adapter
    verified.writer._controller_transport = register_writer
    # Open transport/session before activating the R006 control-class injection;
    # the injection is needed only from ARM onward.
    verified.writer.open(live_ack=R004_LIVE_ACK)
    verified.injection.activate()
    runtime = R005LiveRuntimePort(adapter)
    runtime.sync_qualification_passes(3)
    physical_ledger = _open_r006_physical_ledger(
        Path(ready["ledger_path"]),
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256=parent_inputs.eoat_sha256,
    )
    sequence = max(
        (record.attempt_sequence for record in physical_ledger.records),
        default=0,
    )

    def run_trial(item: QueueItem) -> Mapping[str, Any]:
        nonlocal sequence
        sequence += 1
        candidate = R006Candidate.from_canonical(item.candidate)
        attempt = Attempt(
            epoch=int(ready["session_epoch"]),
            attempt_sequence=sequence,
            candidate=candidate,
            kind=item.kind,
            dispatch_sequence=sequence,
            request_uid=item.dispatch_id,
            logical_request_uid=item.dispatch_id,
            execution_id=f"r012-{item.dispatch_id}-{uuid.uuid4().hex}",
        )
        runtime.home()
        runtime.dispatch(attempt, item)
        runtime.arm(attempt)
        result = runtime.safe_return(attempt, runtime.run_60s(attempt))
        result = _apply_r012_timing_acceptance(result)
        if not _r012_force_objective_flow_gates_pass(result):
            raise R012OwnerError(
                "R012 physical attempt returned without all required gates: "
                f"safe_return={result.safe_return}; safety_gate={result.safety_gate}; "
                f"contact_gate={result.contact_gate}; return_gate={result.return_gate}; "
                f"motion_gate={result.motion_gate}; timing_gate={result.timing_gate}; "
                f"identity_gate={result.identity_gate}; duration_s={result.duration_s}; "
                f"mae_n={result.mae_n}; metrics={dict(result.metrics)}"
            )
        # Match the mature R008 route: motion quality remains a diagnostic,
        # while the force objective becomes exact only after a fresh rebuild
        # from this attempt's raw PATH artifact.
        sealed = physical_ledger.append(
            result.to_record(contract.campaign_fingerprint)
        )
        mae_n = sealed.mae_n
        if mae_n is None or not math.isfinite(float(mae_n)) or float(mae_n) < 0.0:
            raise R012OwnerError(
                "R012 physical attempt has no sealed exact MAE: "
                f"duration_s={result.duration_s}; metrics={dict(result.metrics)}"
            )
        return {
            "schema": "step5d.autotune-v4/r012-trial-result-v1",
            "dispatch_id": item.dispatch_id,
            "campaign_id": campaign_id,
            "run_id": run_id,
            "attempt_id": r012_attempt_id,
            "state": "HOME",
            "safe_return": True,
            "home": True,
            "return_guard": True,
            "sealed_mae_n": float(mae_n),
            "censored_observation": None,
        }

    return R012B3OwnerContext(mature, run_trial, register_writer)


def _context_from_stack(stack: Mapping[str, Any]) -> R012B3OwnerContext:
    supplied = stack.get("r012_owner_context")
    if supplied is not None:
        if not isinstance(supplied, R012B3OwnerContext):
            raise R012OwnerError("R012 owner context has the wrong type")
        return supplied
    writer = stack.get("mature_writer")
    runner = stack.get("mature_runner")
    if writer is None or not callable(runner):
        raise R012OwnerError(
            "R012 B3 owner requires the already-admitted mature_writer and mature_runner"
        )
    return R012B3OwnerContext(
        writer=writer,
        runner=runner,
        register_writer=stack.get("register_writer"),
    )


def build_r012_b3_owner(
    *,
    stack: Mapping[str, Any],
    mode: AutotuneRunMode | str | None = None,
    run_dir: Path | None = None,
    controller_host: str = "192.168.1.18",
    kunwei_host: str = "192.168.50.25",
    kunwei_port: int = 5152,
    launch_profile: Path | None = None,
) -> R012TrialExecutor:
    """Build the sole R012 executor using the current host stack guard."""

    _ = AutotuneRunMode.parse(mode.value if isinstance(mode, AutotuneRunMode) else mode)
    guard_stack = stack.get("path_guard_stack")
    if not isinstance(guard_stack, R012PathGuardStack):
        raise R012OwnerError("R012 owner stack is missing its current PATH guard stack")
    if run_dir is not None:
        context = _build_live_context(
            run_dir=Path(run_dir),
            controller_host=controller_host,
            kunwei_host=kunwei_host,
            kunwei_port=kunwei_port,
            launch_profile=Path(launch_profile or ROOT / "config/step5/step5d_autotune_v4_r012_launch_profile.json"),
            campaign_id=str(stack["campaign_id"]),
            run_id=str(stack["run_id"]),
            r012_attempt_id=str(stack["attempt_id"]),
        )
    else:
        context = _context_from_stack(stack)
    if not callable(context.runner):
        raise R012OwnerError("R012 mature runner is not callable")

    path_early_end = stack.get("path_early_end")
    if path_early_end is None:
        raise R012OwnerError("R012 owner stack is missing its PATH early-end controller")
    if path_early_end.runtime_protocol != R012_REGISTER_PROTOCOL:
        raise R012OwnerError("R012 PATH register protocol differs")
    if path_early_end.request_register != R012_REQUEST_REGISTER or path_early_end.ack_register != R012_ACK_REGISTER:
        raise R012OwnerError("R012 PATH register mapping differs")

    register_writer = context.register_writer
    if register_writer is None:
        register_writer = stack.get("register_writer")
    if register_writer is None:
        register_writer = getattr(context.writer, "register_writer", None)
    if register_writer is None:
        raise R012OwnerError("R012 owner has no actual register writer for protocol 612012")
    path_early_end.writer = resolve_register_writer(register_writer)

    executor = R012TrialExecutor(
        owner=context.writer,
        runner=context.runner,
        guard_stack=guard_stack,
    )
    if not is_r012_guard_bound(context.writer):
        raise R012OwnerError("R012 mature physical writer is not bound to the current PATH guard")
    return executor


__all__ = [
    "R012_ACK_REGISTER",
    "R012B3OwnerContext",
    "R012_MOTION_PROTOCOL",
    "R012OwnerError",
    "R012_REGISTER_PROTOCOL",
    "build_r012_b3_owner",
]
