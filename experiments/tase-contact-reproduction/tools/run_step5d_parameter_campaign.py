#!/usr/bin/env python3
"""Run V3 from an independent, unbounded parameter receiver."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import time
from typing import Any, Callable, Mapping

from step5d_campaign_identity import campaign_spec
from step5d_autotune_contract import (
    ForceCandidate,
    NORMAL_FILTER_PROFILES,
    TrialSpec,
    TrialTransition,
    TrialTransitionKind,
)
from step5d_autotune_live_driver import AtomicCommandMailbox
from step5d_autotune_runtime_contract import PreparedFingerprint, PreparedTrial
from step5d_autotune_state_machine import HostCommand, HostPacket, TpLoopState
from step5d_autotune_v3.profile import canonical_json_bytes
from step5d_autotune_v3.runtime_gate import load_campaign_lease, process_starttime
from step5d_autotune_v3.runtime_profile import (
    load_launch_profile,
    normalized_overlay_sha256,
)
from step5d_autotune_v3.state import atomic_json
from step5d_parameter_manifest import seed_initial_manifest
from step5d_parameter_outbox import enqueue_postprocess_task
from step5d_parameter_queue import (
    bind_home,
    finish_dispatch,
    load_state,
    prepare_next_dispatch,
    record_dispatch_consumed,
    reconcile_not_consumed,
    status as receiver_status,
)
from step5d_production_csv import BridgeCsvFollower, BridgeCsvTimeout


STATUS_SCHEMA = "step5d.parameter-receiver/live-status-v1"
READY_HOME = int(TpLoopState.READY_HOME)
READY_HOME_NEXT = int(TpLoopState.READY_HOME_NEXT)
UR_RUNTIME_PLAYING = 2
RUNNER_STATES = frozenset(
    {
        "RUNNING",
        "WAITING_FOR_PARAMETERS",
        "WAITING_FOR_HOME",
        "WAITING_FOR_HARDWARE",
        "RECOVERING",
        "SHUTDOWN",
    }
)
RECOVERY_BACKOFF_S = (0.1, 0.25, 0.5, 1.0)
OBSERVATION_POLL_S = 0.5
INFLIGHT_OBSERVATION_POLL_S = 0.5
INFLIGHT_RECOVERY_SLEEP_S = 0.1
EXTERNAL_HARDWARE_TERMINAL_REASONS = frozenset({2, 3, 17})
PARAMETER_GUARD_TERMINAL_REASONS = frozenset({4, 5, 6, 7, 8, 10, 12, 14})


class ParameterCampaignError(RuntimeError):
    pass


class HardwareRecoveryRequired(ParameterCampaignError):
    """The receiver lost the evidence needed to continue a live trial."""


class ExplicitShutdown(ParameterCampaignError):
    """The operator explicitly interrupted the receiver."""


class TrialOutcomeError(ParameterCampaignError):
    status = "FAILED"

    def __init__(
        self,
        failure_class: str,
        detail: str,
        *,
        observed: Mapping[str, int] | None = None,
    ) -> None:
        self.failure_class = failure_class
        self.detail = detail
        self.observed = None if observed is None else dict(observed)
        super().__init__(detail)


def _strict_object(path: Path, role: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ParameterCampaignError(f"{role} must be a real regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ParameterCampaignError(f"{role} is invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ParameterCampaignError(f"{role} must be a JSON object")
    return payload


def _integer(row: Mapping[str, str], name: str) -> int:
    try:
        value = float(row[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise ParameterCampaignError(f"bridge row lacks {name}") from exc
    if not math.isfinite(value) or not value.is_integer():
        raise ParameterCampaignError(f"bridge row {name} is not an integer")
    return int(value)


def _tp_observation(row: Mapping[str, str]) -> dict[str, int]:
    return {
        "campaign_epoch": _integer(row, "ur_output_int_register_24"),
        "trial_id": _integer(row, "ur_output_int_register_25"),
        "state": _integer(row, "ur_output_int_register_26"),
        "candidate_token": _integer(row, "ur_output_int_register_27"),
        "terminal_reason": _integer(row, "ur_output_int_register_28"),
        "execution_profile_id": _integer(row, "ur_output_int_register_29"),
        "consumed_command_seq": _integer(row, "ur_output_int_register_30"),
        "logical_batch_sequence": _integer(row, "ur_output_int_register_34"),
        "batch_row_index": _integer(row, "ur_output_int_register_31"),
        "safety_mode": _integer(row, "ur_safety_mode"),
        "controller_state": _integer(row, "step4e_controller_state"),
    }


def _terminal_identity(observation: Mapping[str, int]) -> dict[str, int]:
    return {
        key: observation[key]
        for key in (
            "campaign_epoch",
            "trial_id",
            "state",
            "candidate_token",
            "execution_profile_id",
            "consumed_command_seq",
            "logical_batch_sequence",
            "batch_row_index",
        )
    }


def _safe_home(observation: Mapping[str, int]) -> bool:
    return bool(
        observation["state"] in {READY_HOME, READY_HOME_NEXT}
        and observation["safety_mode"] == 1
        and observation["controller_state"] == 0
    )


def _terminal_failure_class(terminal_reason: int) -> str | None:
    """Classify only explicit terminal reasons; unknown reasons stay software."""

    if terminal_reason == 1:
        return None
    if terminal_reason in EXTERNAL_HARDWARE_TERMINAL_REASONS:
        return "EXTERNAL_HARDWARE"
    if terminal_reason in PARAMETER_GUARD_TERMINAL_REASONS:
        return "PARAMETER_GUARD"
    return "SOFTWARE"


def _publish_status(
    args: argparse.Namespace,
    *,
    state: str,
    observation: Mapping[str, int] | None,
    blocker: str | None = None,
) -> dict[str, Any]:
    if state not in RUNNER_STATES:
        raise ParameterCampaignError(f"invalid receiver state: {state}")
    queue = receiver_status(args.receiver_root)
    payload = {
        "schema": STATUS_SCHEMA,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "route": "autotune_v3_parameter_receiver",
        "state": state,
        "receiver_accepting": state != "SHUTDOWN",
        "campaign_root": str(args.campaign_root.resolve()),
        "receiver_root": str(args.receiver_root.resolve()),
        "bridge_run": str(args.bridge_run.resolve()),
        "release_manifest_sha256": args.release_manifest_sha256,
        "queue": {
            "revision": queue["revision"],
            "dispatch_sequence": queue["dispatch_sequence"],
            "pending_count": queue["pending_count"],
            "attempted_count": queue["attempted_count"],
            "inflight": queue["inflight"],
            "accepting": True,
            "capacity": None,
        },
        "tp_observation": None if observation is None else dict(observation),
        "blocker": blocker,
        "next_action": (
            "submit another parameter; TP remains stationary at Home"
            if state == "WAITING_FOR_PARAMETERS"
            else "wait for safe Home and bridge recovery"
            if state in {"WAITING_FOR_HOME", "WAITING_FOR_HARDWARE", "RECOVERING"}
            else "wait"
        ),
    }
    atomic_json(args.campaign_root / "parameter_receiver_status.json", payload)
    return payload


def _validate_authority(
    args: argparse.Namespace,
    binding: Mapping[str, Any],
) -> None:
    lease = load_campaign_lease(args.campaign_lease)
    if any(
        (
            lease.campaign_id != binding["campaign_id"],
            lease.campaign_epoch != binding["campaign_epoch"],
            lease.manifest_sha256 != args.release_manifest_sha256,
            lease.program_id != args.v3_program_id,
            lease.supervisor_pid != os.getppid(),
            process_starttime(lease.supervisor_pid) != lease.supervisor_starttime,
        )
    ):
        raise ParameterCampaignError("campaign lease differs from receiver runner")


def _prepared(
    args: argparse.Namespace,
    *,
    binding: Mapping[str, Any],
    dispatch: Mapping[str, Any],
) -> tuple[HostPacket, Any]:
    packet = dispatch["packet"]
    request = dispatch["request"]
    profile = load_launch_profile(
        args.v3_launch_profile,
        expected_tp_program_id=args.v3_program_id,
    )
    overlay = request["overlay"]
    matches = [
        item
        for item in NORMAL_FILTER_PROFILES
        if item.profile_id == overlay["execution_profile_id"]
    ]
    if len(matches) != 1:
        raise ParameterCampaignError("receiver execution profile differs")
    execution_profile = matches[0]
    campaign = campaign_spec(
        args.experiment_root,
        str(binding["campaign_fingerprint"]),
        int(binding["campaign_epoch"]),
        campaign_id=str(binding["campaign_id"]),
    )
    trial = TrialSpec(
        campaign=campaign,
        trial_id=int(packet["trial_id"]),
        candidate_token=int(packet["candidate_token"]),
        command_seq=int(packet["command_seq"]),
        plant_epoch=1,
        backend_id="step5d_parameter_receiver_v1",
        candidate=ForceCandidate(
            force_p_gain=float(overlay["force_p_gain"]),
            force_i_gain=float(overlay["force_i_gain"]),
            force_damping=float(overlay["force_damping"]),
        ),
        execution_profile=execution_profile,
        source_fingerprint=args.release_manifest_sha256,
        config_fingerprint=str(request["normalized_overlay_sha256"]),
        transition=TrialTransition(TrialTransitionKind.BATCH_BOOTSTRAP),
    )
    prepared = PreparedTrial(
        trial=trial,
        frozen=PreparedFingerprint(
            source_fingerprint=args.release_manifest_sha256,
            config_fingerprint=str(request["normalized_overlay_sha256"]),
            composite_fingerprint=str(binding["campaign_fingerprint"]),
        ),
        environment={
            "STEP5D_AUTOTUNE_FORCE_P": repr(trial.candidate.force_p_gain),
            "STEP5D_AUTOTUNE_FORCE_I": repr(trial.candidate.force_i_gain),
            "STEP5D_AUTOTUNE_FORCE_DAMPING": repr(
                trial.candidate.force_damping
            ),
            "STEP5D_AUTOTUNE_NORMAL_RATE_RAD_S": repr(
                execution_profile.normal_max_rate_rad_s
            ),
            "STEP5D_AUTOTUNE_HOST_SLEW_RAD_S2": repr(
                execution_profile.host_qdot_slew_rad_s2
            ),
            "STEP5D_AUTOTUNE_SPEEDJ_ACCELERATION_RAD_S2": repr(
                execution_profile.tp_speedj_accel_rad_s2
            ),
        },
        runner_arguments=(),
        trial_overlay=overlay,
        trial_overlay_sha256=normalized_overlay_sha256(profile, overlay),
        batch_row_index=1,
        occurrence_uid=dispatch["request_identity"]["occurrence_uid"],
        transport_candidate_uid=dispatch["request_identity"][
            "transport_candidate_uid"
        ],
        control_candidate_uid=request["control_candidate_uid"],
    )
    arm = HostPacket(
        campaign_epoch=int(packet["campaign_epoch"]),
        trial_id=int(packet["trial_id"]),
        command=HostCommand.ARM,
        candidate_token=int(packet["candidate_token"]),
        execution_profile_id=int(packet["execution_profile_id"]),
        command_seq=int(packet["command_seq"]),
        logical_batch_sequence=int(packet["logical_batch_sequence"]),
    )
    return arm, prepared


def _send(
    args: argparse.Namespace,
    *,
    binding: Mapping[str, Any],
    dispatch: Mapping[str, Any],
) -> tuple[HostPacket, Any]:
    arm, prepared = _prepared(args, binding=binding, dispatch=dispatch)
    mailbox = AtomicCommandMailbox(
        args.mailbox,
        network_mode=True,
        launch_profile=load_launch_profile(
            args.v3_launch_profile,
            expected_tp_program_id=args.v3_program_id,
        ),
    )
    try:
        mailbox.send_command(arm, prepared_trial=prepared)
        decoded = mailbox.read_latest()
    except (ConnectionError, OSError, TimeoutError, RuntimeError) as exc:
        raise HardwareRecoveryRequired("ARM mailbox or bridge is unavailable") from exc
    if decoded is None or decoded.packet != arm:
        raise HardwareRecoveryRequired("receiver ARM mailbox readback differs")
    return arm, prepared


def _capture_health(
    path: Path,
) -> tuple[str, str | None]:
    """Inspect already-sealed data without delaying the next physical trial."""

    if path.is_symlink():
        return "DATA_ISSUE", "capture path is a symlink"
    if not path.is_file():
        return "PENDING", "capture seal pending asynchronous outbox validation"
    try:
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.reader(stream)
            header = next(reader)
            first = next(reader)
        if not header or len(header) != len(set(header)) or len(first) != len(header):
            raise ValueError("capture shape differs")
    except (OSError, StopIteration, UnicodeError, ValueError, csv.Error) as exc:
        return "DATA_ISSUE", f"capture unreadable: {type(exc).__name__}:{exc}"
    return "COMPLETE", None


def _wait_initial_home(
    args: argparse.Namespace,
    follower: BridgeCsvFollower,
) -> dict[str, int]:
    _publish_status(args, state="WAITING_FOR_HOME", observation=None)
    while True:
        try:
            for row in follower.rows(timeout_s=OBSERVATION_POLL_S):
                observed = _tp_observation(row)
                if observed["safety_mode"] != 1:
                    raise HardwareRecoveryRequired("UR Safety is not NORMAL")
                if (
                    _integer(row, "ur_runtime_state") == UR_RUNTIME_PLAYING
                    and observed["state"] == READY_HOME
                ):
                    zero_fields = (
                        observed["campaign_epoch"],
                        observed["trial_id"],
                        observed["candidate_token"],
                        observed["consumed_command_seq"],
                        observed["logical_batch_sequence"],
                    )
                    if any(zero_fields):
                        raise ParameterCampaignError(
                            "initial READY_HOME identity is not zero"
                        )
                    return observed
        except BridgeCsvTimeout:
            continue


def _wait_resume_home(
    args: argparse.Namespace,
    follower: BridgeCsvFollower,
    *,
    home_identity: Mapping[str, int],
) -> dict[str, int]:
    """Adopt only the exact durable terminal Home after a runner restart."""

    expected = {
        "campaign_epoch": int(home_identity["campaign_epoch"]),
        "trial_id": int(home_identity["last_trial_id"]),
        "consumed_command_seq": int(home_identity["last_command_seq"]),
    }
    _publish_status(args, state="WAITING_FOR_HOME", observation=None)
    while True:
        try:
            for row in follower.rows(timeout_s=OBSERVATION_POLL_S):
                observed = _tp_observation(row)
                if observed["safety_mode"] != 1:
                    raise HardwareRecoveryRequired("UR Safety is not NORMAL")
                if (
                    _integer(row, "ur_runtime_state") != UR_RUNTIME_PLAYING
                    or observed["state"] != READY_HOME_NEXT
                ):
                    continue
                if not _safe_home(observed):
                    raise HardwareRecoveryRequired(
                        "resume READY_HOME_NEXT is not a safe Home"
                    )
                actual = {
                    key: observed[key]
                    for key in (
                        "campaign_epoch",
                        "trial_id",
                        "consumed_command_seq",
                    )
                }
                if actual != expected:
                    raise ParameterCampaignError(
                        "resume READY_HOME_NEXT identity differs from durable Home"
                    )
                return observed
        except BridgeCsvTimeout:
            continue


def _wait_next_dispatch(
    args: argparse.Namespace,
    follower: BridgeCsvFollower,
    observation: Mapping[str, int],
) -> dict[str, Any]:
    if not _safe_home(observation):
        raise HardwareRecoveryRequired("safe Home is not currently proven")
    while True:
        dispatch = prepare_next_dispatch(args.receiver_root)
        if dispatch is not None:
            return dispatch
        _publish_status(
            args,
            state="WAITING_FOR_PARAMETERS",
            observation=observation,
        )
        try:
            for row in follower.rows(timeout_s=OBSERVATION_POLL_S):
                current = _tp_observation(row)
                if not _safe_home(current):
                    raise HardwareRecoveryRequired("TP left safe Home")
                dispatch = prepare_next_dispatch(args.receiver_root)
                if dispatch is not None:
                    return dispatch
        except BridgeCsvTimeout:
            continue


def _wait_terminal(
    follower: BridgeCsvFollower,
    *,
    arm: HostPacket,
    poll_s: float = OBSERVATION_POLL_S,
    on_consumed: Callable[[Mapping[str, int]], None] | None = None,
) -> tuple[dict[str, int], dict[str, str]]:
    expected = {
        "campaign_epoch": arm.campaign_epoch,
        "trial_id": arm.trial_id,
        "candidate_token": arm.candidate_token,
        "execution_profile_id": arm.execution_profile_id,
        "consumed_command_seq": arm.command_seq,
        "logical_batch_sequence": arm.logical_batch_sequence,
        "batch_row_index": 1,
    }
    identity_failure: str | None = None
    attempt_recorded = False
    while True:
        try:
            for row in follower.rows(timeout_s=poll_s):
                observed = _tp_observation(row)
                if observed["safety_mode"] != 1:
                    raise HardwareRecoveryRequired("UR Safety is not NORMAL")
                sequence = observed["consumed_command_seq"]
                if sequence < arm.command_seq:
                    continue
                if sequence > arm.command_seq:
                    identity_failure = (
                        "observation consumed a command newer than this trial"
                    )
                elif any(observed[key] != value for key, value in expected.items()):
                    identity_failure = (
                        "same-sequence observation identity differs from dispatched ARM"
                    )
                elif not attempt_recorded and on_consumed is not None:
                    on_consumed(observed)
                    attempt_recorded = True
                if observed["state"] != READY_HOME_NEXT:
                    continue
                if identity_failure is not None:
                    raise TrialOutcomeError(
                        "IDENTITY",
                        identity_failure,
                        observed=observed,
                    )
                return observed, row
        except BridgeCsvTimeout:
            # A follower timeout is only a poll boundary.  It never completes,
            # fails, clears, or replays the inflight physical trial.
            continue


def _inflight_identity_matches(
    dispatch: Mapping[str, Any], observation: Mapping[str, int]
) -> bool:
    packet = dispatch["packet"]
    expected = {
        "campaign_epoch": packet["campaign_epoch"],
        "trial_id": packet["trial_id"],
        "candidate_token": packet["candidate_token"],
        "execution_profile_id": packet["execution_profile_id"],
        "consumed_command_seq": packet["command_seq"],
        "logical_batch_sequence": packet["logical_batch_sequence"],
        "batch_row_index": 1,
    }
    return all(observation.get(key) == value for key, value in expected.items())


def _classify_inflight_observation(
    dispatch: Mapping[str, Any], observation: Mapping[str, int]
) -> tuple[str, str]:
    """Classify fresh evidence without inferring an ARM or trial outcome.

    The command sequence is the only evidence that can prove a command was not
    consumed.  A terminal Home row is required before any consumed dispatch is
    durably classified.  All other observations remain a hardware wait.
    """

    target_sequence = int(dispatch["packet"]["command_seq"])
    observed_sequence = int(observation["consumed_command_seq"])
    terminal_home = bool(
        observation["state"] == READY_HOME_NEXT and _safe_home(observation)
    )
    if observed_sequence < target_sequence:
        if _safe_home(observation):
            return (
                "NOT_CONSUMED",
                "fresh safe Home proves the inflight ARM was not consumed",
            )
        return (
            "WAITING_FOR_HARDWARE",
            "consumed sequence is behind dispatch but safe Home is not proven",
        )
    if observed_sequence == target_sequence:
        if not terminal_home:
            return (
                "WAITING_FOR_HARDWARE",
                "inflight ARM was consumed; waiting for terminal Home evidence",
            )
        if _inflight_identity_matches(dispatch, observation):
            return (
                "TERMINAL",
                "fresh terminal Home matches the inflight dispatch",
            )
        return (
            "IDENTITY",
            "fresh terminal Home consumed the dispatch with mismatched identity",
        )
    if terminal_home:
        return (
            "IDENTITY",
            "fresh terminal Home consumed a newer command than the inflight dispatch",
        )
    return (
        "WAITING_FOR_HARDWARE",
        "consumed sequence does not match and no terminal identity is proven",
    )


def _finish_adopted_terminal(
    args: argparse.Namespace,
    *,
    binding: Mapping[str, Any],
    dispatch: Mapping[str, Any],
    observed: Mapping[str, int],
    decision: str,
    detail: str,
) -> None:
    """Persist an already-consumed dispatch without sending a new ARM."""

    _arm, prepared = _prepared(args, binding=binding, dispatch=dispatch)
    capture = (
        args.bridge_run
        / "autotune_trials"
        / prepared.trial.trial_uid
        / "capture.csv"
    )
    if decision == "IDENTITY":
        _finish_trial(
            args,
            dispatch=dispatch,
            prepared=prepared,
            observed=observed,
            status="FAILED",
            failure_class="IDENTITY",
            detail=detail,
            capture=capture,
        )
        return

    failure_class = _terminal_failure_class(observed["terminal_reason"])
    if failure_class is None:
        capture_status, capture_detail = _capture_health(capture)
        status = "FAILED" if capture_status == "DATA_ISSUE" else "SUCCEEDED"
        failure_class = "DATA_QUALITY" if status == "FAILED" else None
        detail = capture_detail
    else:
        status = "FAILED"
        detail = (
            f"terminal reason={observed['terminal_reason']}"
            f" classified={failure_class}"
        )
    _finish_trial(
        args,
        dispatch=dispatch,
        prepared=prepared,
        observed=observed,
        status=status,
        failure_class=failure_class,
        detail=detail,
        capture=capture,
    )


def _adopt_inflight(
    args: argparse.Namespace,
    *,
    binding: Mapping[str, Any],
    follower: BridgeCsvFollower,
    dispatch: Mapping[str, Any],
) -> dict[str, int]:
    """Observe a durable inflight dispatch; never replay its ARM command."""

    detail = "runner restart is adopting durable inflight dispatch by observation"
    while True:
        _publish_status(
            args,
            state="WAITING_FOR_HARDWARE",
            observation=None,
            blocker=detail,
        )
        adopted: tuple[str, str, dict[str, int]] | None = None
        try:
            for row in follower.rows(timeout_s=INFLIGHT_OBSERVATION_POLL_S):
                observed = _tp_observation(row)
                decision, evidence_detail = _classify_inflight_observation(
                    dispatch, observed
                )
                if decision == "WAITING_FOR_HARDWARE":
                    if _inflight_identity_matches(dispatch, observed):
                        record_dispatch_consumed(
                            args.receiver_root,
                            observed=observed,
                        )
                    detail = evidence_detail
                    _publish_status(
                        args,
                        state="WAITING_FOR_HARDWARE",
                        observation=observed,
                        blocker=detail,
                    )
                    continue
                adopted = (decision, evidence_detail, observed)
                break
        except (BridgeCsvTimeout, OSError, RuntimeError, ValueError) as exc:
            detail = (
                "inflight adoption evidence unavailable: "
                f"{type(exc).__name__}:{exc}"
            )
            _publish_status(
                args,
                state="WAITING_FOR_HARDWARE",
                observation=None,
                blocker=detail,
            )
        if adopted is not None:
            decision, evidence_detail, observed = adopted
            if decision == "NOT_CONSUMED":
                reconcile_not_consumed(
                    args.receiver_root,
                    detail=evidence_detail,
                    observed_command_seq=observed["consumed_command_seq"],
                )
                return observed
            _finish_adopted_terminal(
                args,
                binding=binding,
                dispatch=dispatch,
                observed=observed,
                decision=decision,
                detail=evidence_detail,
            )
            return observed
        _sleep(args, INFLIGHT_RECOVERY_SLEEP_S)


def _sleep(args: argparse.Namespace, delay_s: float) -> None:
    getattr(args, "sleep", time.sleep)(delay_s)


def _recover_home(
    args: argparse.Namespace,
    follower: BridgeCsvFollower,
    *,
    observation: Mapping[str, int] | None,
    detail: str,
) -> dict[str, int]:
    _publish_status(
        args,
        state="WAITING_FOR_HARDWARE",
        observation=observation,
        blocker=detail,
    )
    while True:
        for delay_s in RECOVERY_BACKOFF_S:
            _publish_status(
                args,
                state="RECOVERING",
                observation=observation,
                blocker=detail,
            )
            try:
                for row in follower.rows(timeout_s=0.5):
                    current = _tp_observation(row)
                    if _safe_home(current):
                        return current
                    observation = current
            except (BridgeCsvTimeout, OSError, RuntimeError, ValueError) as exc:
                detail = f"recovery: {type(exc).__name__}:{exc}"
            _sleep(args, delay_s)
        # Backoff is bounded; waiting for fresh evidence is not.


def _finish_trial(
    args: argparse.Namespace,
    *,
    dispatch: Mapping[str, Any],
    prepared: Any,
    observed: Mapping[str, int],
    status: str,
    failure_class: str | None,
    detail: str | None,
    capture: Path,
) -> None:
    queue_observed = (
        dict(observed)
        if failure_class == "IDENTITY"
        else _terminal_identity(observed)
    )
    receipt = finish_dispatch(
        args.receiver_root,
        status=status,
        failure_class=failure_class,
        observed=queue_observed,
        detail=detail,
    )
    result_path = (
        args.campaign_root
        / "parameter_results"
        / f"{dispatch['dispatch_sequence']:012d}.json"
    )
    outbox_path = None
    if status == "SUCCEEDED":
        outbox_path = enqueue_postprocess_task(
            args.campaign_root / "parameter_outbox",
            dispatch_sequence=int(dispatch["dispatch_sequence"]),
            trial_uid=prepared.trial.trial_uid,
            capture_path=capture,
            result_path=result_path,
        )
    result = {
        "schema": "step5d.parameter-receiver/trial-result-v2",
        "request_uid": dispatch["request"]["request_uid"],
        "dispatch_sequence": dispatch["dispatch_sequence"],
        "trial_uid": prepared.trial.trial_uid,
        "capture": str(capture),
        "capture_sha256": None,
        "status": status,
        "failure_class": failure_class,
        "detail": detail,
        "outbox_task": None if outbox_path is None else str(outbox_path),
        "receipt": receipt,
    }
    encoded = canonical_json_bytes(result)
    result_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if result_path.exists() or result_path.is_symlink():
        if result_path.is_symlink() or result_path.read_bytes() != encoded:
            raise ParameterCampaignError("immutable parameter result differs")
    else:
        result_path.write_bytes(encoded)


def _run_trial(
    args: argparse.Namespace,
    follower: BridgeCsvFollower,
    *,
    dispatch: Mapping[str, Any],
    arm: HostPacket,
    prepared: Any,
    observation: Mapping[str, int],
) -> dict[str, int]:
    while True:
        try:
            on_consumed = (
                None
                if not hasattr(args, "receiver_root")
                else lambda observed: record_dispatch_consumed(
                    args.receiver_root,
                    observed=observed,
                )
            )
            terminal, _row = _wait_terminal(
                follower,
                arm=arm,
                on_consumed=on_consumed,
            )
            break
        except HardwareRecoveryRequired as exc:
            observation = _recover_home(
                args,
                follower,
                observation=observation,
                detail=str(exc),
            )
        except TrialOutcomeError as exc:
            observed = exc.observed or dict(observation)
            _finish_trial(
                args,
                dispatch=dispatch,
                prepared=prepared,
                observed=observed,
                status=exc.status,
                failure_class=exc.failure_class,
                detail=exc.detail,
                capture=args.bridge_run / "autotune_trials" / prepared.trial.trial_uid / "capture.csv",
            )
            return observed

    capture = (
        args.bridge_run
        / "autotune_trials"
        / prepared.trial.trial_uid
        / "capture.csv"
    )
    failure_class = _terminal_failure_class(terminal["terminal_reason"])
    if failure_class is None:
        capture_status, detail = _capture_health(capture)
        status = "FAILED" if capture_status == "DATA_ISSUE" else "SUCCEEDED"
        failure_class = "DATA_QUALITY" if status == "FAILED" else None
    else:
        status = "FAILED"
        detail = (
            f"terminal reason={terminal['terminal_reason']}"
            f" classified={failure_class}"
        )
    _finish_trial(
        args,
        dispatch=dispatch,
        prepared=prepared,
        observed=terminal,
        status=status,
        failure_class=failure_class,
        detail=detail,
        capture=capture,
    )
    return terminal


def run(args: argparse.Namespace) -> None:
    binding = _strict_object(args.campaign_binding, "campaign binding")
    _validate_authority(args, binding)
    seeded = seed_initial_manifest(
        args.receiver_root,
        campaign_id=str(binding["campaign_id"]),
        release_manifest_sha256=args.release_manifest_sha256,
        launch_profile_path=args.v3_launch_profile,
        manifest_path=args.initial_manifest,
        experiment_root=args.experiment_root,
    )
    queue_state = load_state(args.receiver_root)
    ready = {
        "schema": "step5d.parameter-receiver/runner-ready-v1",
        "campaign_id": binding["campaign_id"],
        "release_manifest_sha256": args.release_manifest_sha256,
        "receiver_root": str(args.receiver_root.resolve()),
        "initial_parameters_seeded": len(seeded),
        "optimizer_required": False,
        "motion_authorized": False,
    }
    atomic_json(args.runner_ready_file, ready)
    follower = BridgeCsvFollower(args.bridge_run / "bridge_rtde_500hz.csv")
    if queue_state["inflight"] is not None:
        dispatch = prepare_next_dispatch(args.receiver_root)
        if dispatch is None:
            raise ParameterCampaignError(
                "queue inflight identity has no durable dispatch record"
            )
        # Adoption consumes only fresh TP/RTDE observations.  It may reconcile
        # a provably unconsumed command or close an already-consumed terminal
        # outcome, but it never calls _send for the recovered dispatch.
        observed = _adopt_inflight(
            args,
            binding=binding,
            follower=follower,
            dispatch=dispatch,
        )
    else:
        home_identity = queue_state["home_identity"]
        resume_home = (
            home_identity is not None
            and (
                int(home_identity["last_trial_id"]) > 0
                or int(home_identity["last_command_seq"]) > 0
            )
        )
        if not resume_home:
            while True:
                try:
                    observed = _wait_initial_home(args, follower)
                    break
                except HardwareRecoveryRequired as exc:
                    observed = _recover_home(
                        args,
                        follower,
                        observation=None,
                        detail=str(exc),
                    )
            bind_home(
                args.receiver_root,
                campaign_epoch=max(1, observed["campaign_epoch"]),
                last_trial_id=observed["trial_id"],
                last_command_seq=observed["consumed_command_seq"],
            )
        else:
            observed = _wait_resume_home(
                args,
                follower,
                home_identity=home_identity,
            )
    dispatch = None
    while True:
        try:
            if dispatch is None:
                dispatch = _wait_next_dispatch(args, follower, observed)
            arm, prepared = _send(args, binding=binding, dispatch=dispatch)
            _publish_status(args, state="RUNNING", observation=observed)
            observed = _run_trial(
                args,
                follower,
                dispatch=dispatch,
                arm=arm,
                prepared=prepared,
                observation=observed,
            )
            dispatch = None
        except HardwareRecoveryRequired as exc:
            observed = _recover_home(
                args,
                follower,
                observation=observed,
                detail=str(exc),
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-root", type=Path, required=True)
    parser.add_argument("--bridge-run", type=Path, required=True)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--receiver-root", type=Path, required=True)
    parser.add_argument("--mailbox", type=Path, required=True)
    parser.add_argument("--runner-ready-file", type=Path, required=True)
    parser.add_argument("--campaign-binding", type=Path, required=True)
    parser.add_argument("--campaign-lease", type=Path, required=True)
    parser.add_argument("--release-manifest-sha256", required=True)
    parser.add_argument("--v3-launch-profile", type=Path, required=True)
    parser.add_argument("--v3-program-id", required=True)
    parser.add_argument("--initial-manifest", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    previous_handlers = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }

    def request_shutdown(signum, _frame):
        raise ExplicitShutdown(f"received signal {signum}")

    for signum in previous_handlers:
        signal.signal(signum, request_shutdown)
    try:
        run(args)
    except (KeyboardInterrupt, ExplicitShutdown) as exc:
        try:
            if (args.receiver_root / "state.json").is_file():
                _publish_status(
                    args,
                    state="SHUTDOWN",
                    observation=None,
                    blocker=f"{type(exc).__name__}:{exc}",
                )
        except Exception:
            pass
        print(
            f"parameter campaign shutdown: {type(exc).__name__}:{exc}",
            file=__import__("sys").stderr,
        )
        return 2
    except Exception as exc:
        try:
            if (args.receiver_root / "state.json").is_file():
                _publish_status(
                    args,
                    state="RECOVERING",
                    observation=None,
                    blocker=f"{type(exc).__name__}:{exc}",
                )
        except Exception:
            pass
        print(
            f"parameter campaign recovering: {type(exc).__name__}:{exc}",
            file=__import__("sys").stderr,
        )
        return 2
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
