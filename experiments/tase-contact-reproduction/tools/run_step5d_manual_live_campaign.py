#!/usr/bin/env python3
"""Run the governed Manual V2 grid on one already-running production bridge."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR
from step5d_autotune_contract import (
    CampaignSpec,
    ForceCandidate,
    NORMAL_FILTER_PROFILES,
    TrialSpec,
    TrialTransition,
    TrialTransitionKind,
)
from step5d_autotune_evaluator import (
    fixed_f0_bin_metrics,
    profile_metrics,
    qd_tracking_metrics,
    read_csv_rows,
)
from step5d_autotune_live_driver import AtomicCommandMailbox, TrialArtifactProducer
from step5d_autotune_state_machine import HostCommand, HostPacket, TpLoopState
from step5d_autotune_v3.dashboard import dashboard_exchange
from step5d_autotune_v3.runtime_gate import loaded_program_matches
from step5d_autotune_v3.state import atomic_json
from step5d_autotune_v3.profile import canonical_json_bytes
from step5d_autotune_v3.runtime_profile import (
    normalized_overlay_sha256,
    normalize_trial_overlay,
)
from step5d_bridge_status import (
    readiness_claim,
    resolve_status as resolve_bridge_status,
)
from step5d_manual_bridge import (
    PROGRAM,
    PROTOCOL,
    ROOT,
    require_canonical_shell,
    strict_object,
)
from step5d_manual_campaign_plan import (
    INITIAL_GROUPS,
    TrialScore,
    append_top2_repeats,
    seed_initial_grid,
)
from step5d_manual_queue import close as close_queue, load_queue
from step5d_manual_runtime import (
    confirm_home_complete,
    issue_prepared_intent,
    load_state,
    prepare_next_intent,
    seed_home_state,
)
from step5d_manual_profile import (
    DEFAULT_LAUNCH_PROFILE,
    load_manual_launch_profile as load_launch_profile,
)
from step5d_manual_authorization import (
    AUTHORIZATION_SCHEMA,
    CAPABILITIES,
    ManualAuthorizationError,
    load_capability_authorization as _load_capability_authorization,
)
from step5d_manual_qualification import validate_result as validate_manual_qualification
from run_step5d_manual_bridge import ARM_GATE_SCHEMA


BACKEND_ID = "step5d_manual_hold_v1"
READY_HOME = 10
READY_HOME_NEXT = 78
STATUS_SCHEMA = "step5d.manual-v2/governed-status-v1"
EXPECTED_PROGRAM = f"/programs/andyl/kunwei/step5/{PROGRAM}.urp"
ARM_ACKNOWLEDGED_STATES = frozenset(
    {
        int(TpLoopState.ARMED),
        int(TpLoopState.RUN),
        int(TpLoopState.TERMINAL),
        int(TpLoopState.RETRACT),
        int(TpLoopState.RETURN),
        int(TpLoopState.HOME_VERIFY),
        int(TpLoopState.WAIT_ACK),
        int(TpLoopState.WAIT_INFRA_READY),
        int(TpLoopState.READY_NEAR),
        int(TpLoopState.READY_HOME_CLOSED),
        int(TpLoopState.READY_HOME_NEXT),
    }
)


class ManualLiveError(RuntimeError):
    pass


def load_capability_authorization(
    path: Path,
    *,
    attempt_id: str,
    campaign_id: str,
    release_manifest_sha256: str,
    now_ns: int | None = None,
) -> dict[str, Any]:
    try:
        return _load_capability_authorization(
            path,
            attempt_id=attempt_id,
            campaign_id=campaign_id,
            release_manifest_sha256=release_manifest_sha256,
            now_ns=now_ns,
        )
    except ManualAuthorizationError as exc:
        raise ManualLiveError(str(exc)) from exc


def _sha(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(dict(payload))).hexdigest()


def _write_once(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise ManualLiveError(f"write-once artifact already exists: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False).encode() + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _pid_alive(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return False
    try:
        os.kill(value, 0)
    except OSError:
        return False
    return True


def _latest_bridge_row(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ManualLiveError("bridge CSV is missing")
    if datetime.now().timestamp() - path.stat().st_mtime > 2.0:
        raise ManualLiveError("bridge CSV is stale")
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise ManualLiveError("bridge CSV has no data rows")
    return rows[-1]


def validate_bridge(
    output_root: Path,
    release_sha: str,
    *,
    require_mailbox_absent: bool = False,
) -> tuple[Path, dict[str, int]]:
    root = output_root.expanduser().resolve(strict=True)
    launch = strict_object(root / "bridge_launch.json", "manual bridge launch")
    ticket = strict_object(root / "runtime/runtime_ticket.json", "manual runtime ticket")
    ready = strict_object(root / "runtime/bridge/bridge_ready.json", "manual bridge readiness")
    if any((
        launch.get("ok") is not True,
        launch.get("scope") != "manual_bridge_no_arm",
        launch.get("program") != PROGRAM,
        launch.get("protocol") != PROTOCOL,
        launch.get("manual_release_manifest_sha256") != release_sha,
        launch.get("arm_authorized") is not False,
        launch.get("motion_authorized") is not False,
        not _pid_alive(launch.get("parent_pid")),
        not _pid_alive(launch.get("pid")),
        ready.get("ok") is not True,
        ready.get("pid") != launch.get("pid"),
        ready.get("launch_nonce") != ticket.get("launch_id"),
        ready.get("rtde_send_succeeded") is not True,
        ready.get("sensor_stream_ready") is not True,
    )):
        raise ManualLiveError("running manual bridge identity/readiness differs")
    mailbox = Path(str(launch.get("mailbox"))).resolve()
    expected = root / "runtime/command.json"
    if mailbox != expected or mailbox.is_symlink():
        raise ManualLiveError("manual mailbox path differs at the bridge runtime")
    if require_mailbox_absent and mailbox.exists():
        raise ManualLiveError("manual mailbox is not initially absent at the bridge runtime")
    row = _latest_bridge_row(root / "runtime/bridge/bridge_rtde_500hz.csv")
    observed = {
        "state": int(float(row["ur_output_int_register_26"])),
        "campaign_epoch": int(float(row["ur_output_int_register_24"])),
        "trial_id": int(float(row["ur_output_int_register_25"])),
        "consumed_command_seq": int(float(row["ur_output_int_register_30"])),
        "command": int(float(row["command"])),
        "controller_state": int(float(row["step4e_controller_state"])),
        "safety_mode": int(float(row["ur_safety_mode"])),
    }
    if any((
        observed["controller_state"] != 0,
        observed["safety_mode"] != 1,
        observed["campaign_epoch"] < 0,
        observed["trial_id"] < 0,
        observed["consumed_command_seq"] < 0,
    )):
        raise ManualLiveError("bridge/TP safety or identity observation differs")
    return mailbox, observed


def _prepared(intent: Mapping[str, Any], *, plant_epoch: int = 1) -> tuple[HostPacket, Any]:
    packet = intent["packet"]
    overlay = normalize_trial_overlay(
        intent["overlay"], profile=load_launch_profile(DEFAULT_LAUNCH_PROFILE)
    )
    profiles = [row for row in NORMAL_FILTER_PROFILES if row.profile_id == overlay["execution_profile_id"]]
    if len(profiles) != 1:
        raise ManualLiveError("manual execution profile differs")
    profile = profiles[0]
    candidate = ForceCandidate(
        force_p_gain=overlay["force_p_gain"],
        force_i_gain=overlay["force_i_gain"],
        force_damping=overlay["force_damping"],
    )
    campaign_fingerprint = _sha({
        "schema": "step5d.manual-hold/live-campaign/v1",
        "campaign_id": intent["campaign_id"],
        "release_manifest_sha256": intent["release_manifest_sha256"],
    })
    source_fingerprint = intent["release_manifest_sha256"]
    config_fingerprint = intent["request_identity"]["normalized_overlay_sha256"]
    campaign = CampaignSpec(
        campaign_id=intent["campaign_id"],
        campaign_epoch=packet["campaign_epoch"],
        campaign_fingerprint=campaign_fingerprint,
        f0_shadow_reaction_normal_base=STEP5D_V3_PHYSICAL_PRIOR.reaction_normal_b,
    )
    trial = TrialSpec(
        campaign=campaign,
        trial_id=packet["trial_id"],
        candidate_token=packet["candidate_token"],
        command_seq=packet["command_seq"],
        plant_epoch=plant_epoch,
        backend_id=BACKEND_ID,
        candidate=candidate,
        execution_profile=profile,
        source_fingerprint=source_fingerprint,
        config_fingerprint=config_fingerprint,
        transition=TrialTransition(TrialTransitionKind.BATCH_BOOTSTRAP),
    )
    frozen = SimpleNamespace(
        source_fingerprint=source_fingerprint,
        config_fingerprint=config_fingerprint,
        composite_fingerprint=campaign_fingerprint,
    )
    environment = {
        "STEP5D_AUTOTUNE_FORCE_P": repr(candidate.force_p_gain),
        "STEP5D_AUTOTUNE_FORCE_I": repr(candidate.force_i_gain),
        "STEP5D_AUTOTUNE_FORCE_DAMPING": repr(candidate.force_damping),
        "STEP5D_AUTOTUNE_NORMAL_RATE_RAD_S": repr(profile.normal_max_rate_rad_s),
        "STEP5D_AUTOTUNE_HOST_SLEW_RAD_S2": repr(profile.host_qdot_slew_rad_s2),
        "STEP5D_AUTOTUNE_SPEEDJ_ACCELERATION_RAD_S2": repr(profile.tp_speedj_accel_rad_s2),
    }
    prepared = SimpleNamespace(
        trial=trial,
        frozen=frozen,
        environment=environment,
        trial_overlay=overlay,
        trial_overlay_sha256=normalized_overlay_sha256(
            load_launch_profile(DEFAULT_LAUNCH_PROFILE), overlay
        ),
        batch_row_index=1,
        occurrence_uid=intent["request_identity"]["occurrence_uid"],
        transport_candidate_uid=intent["request_identity"]["transport_candidate_uid"],
        control_candidate_uid=overlay["control_candidate_uid"],
    )
    host = HostPacket(
        campaign_epoch=packet["campaign_epoch"],
        trial_id=packet["trial_id"],
        command=HostCommand.ARM,
        candidate_token=packet["candidate_token"],
        execution_profile_id=packet["execution_profile_id"],
        command_seq=packet["command_seq"],
        logical_batch_sequence=packet["logical_batch_sequence"],
    )
    return host, prepared


def _status_path(args: argparse.Namespace) -> Path:
    return args.campaign_root / "manual_governed_status.json"


def _publish_status(
    args: argparse.Namespace,
    *,
    state: str,
    observed: Mapping[str, int] | None,
    completed: int,
    total: int,
    next_group: str | None,
    blocker: str | None = None,
    capabilities: Mapping[str, bool] | None = None,
    authorization_file: Path | None = None,
    controller_observation: Mapping[str, Any] | None = None,
    launch_attempt_id: str,
) -> dict[str, Any]:
    bridge_launch = strict_object(
        args.bridge_output_root / "bridge_launch.json", "manual bridge launch"
    )
    bridge_pid = bridge_launch.get("pid")
    payload = {
        "schema": STATUS_SCHEMA,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "route": "manual_v2",
        "state": state,
        "release_sha": args.release_manifest_sha256,
        "campaign_id": args.campaign_id,
        "launch_attempt_id": launch_attempt_id,
        "campaign_root": str(args.campaign_root.resolve()),
        "output_root": str(args.bridge_output_root.resolve()),
        "loaded_program": EXPECTED_PROGRAM,
        "controller_observation": (
            None if controller_observation is None else dict(controller_observation)
        ),
        "bridge_pid": bridge_pid,
        "bridge_heartbeat": _pid_alive(bridge_pid),
        "offline_qualification": {
            "path": str(args.qualification_result.resolve(strict=True)),
            "sha256": hashlib.sha256(args.qualification_result.read_bytes()).hexdigest(),
        },
        "offline_proven": True,
        "capabilities": {
            name: bool((capabilities or {}).get(name, name == "bridge"))
            for name in CAPABILITIES
        },
        "authorization": (
            None
            if authorization_file is None
            else {
                "path": str(authorization_file.resolve(strict=True)),
                "sha256": hashlib.sha256(authorization_file.read_bytes()).hexdigest(),
            }
        ),
        "play_prompt_ready": (
            state == "WAITING_FOR_IDENTITY_PLAY"
            and capabilities is not None
            and all(capabilities.get(name) is True for name in ("play", "arm", "motion"))
        ),
        "prepared_group": next_group,
        "completed_trials": completed,
        "planned_trials": total,
        "tp_observation": None if observed is None else dict(observed),
        "blocker": blocker,
        "next_action": (
            "press Play once on the loaded Manual V2 program"
            if state == "WAITING_FOR_IDENTITY_PLAY" and capabilities is not None
            else "await explicit Play/ARM/motion authorization or stop"
            if state == "BRIDGE_ALIVE_NO_ARM"
            else "wait for exact TP ARM acknowledgement"
            if state in {"PLAY_OBSERVED_IDENTITY_RECHECKED", "ARM_PENDING"}
            else "none" if state in {"RUNNING", "COMPLETE"} else "inspect blocker evidence"
        ),
    }
    atomic_json(_status_path(args), payload)
    return payload


def _publish_canonical_readiness_claim(
    args: argparse.Namespace, required_state: str
) -> dict[str, Any]:
    try:
        status = resolve_bridge_status(ROOT)
        claim = readiness_claim(status, required_state)
    except Exception as exc:
        raise ManualLiveError(
            "canonical Manual readiness claim was not admitted: "
            f"{type(exc).__name__}:{exc}"
        ) from exc
    atomic_json(args.bridge_output_root / "readiness-claim.json", claim)
    return claim


def _observe_controller_identity(
    robot_host: str, *, timeout_s: float = 2.0
) -> dict[str, Any]:
    dashboard = dashboard_exchange(
        robot_host,
        ["programState", "safetymode", "get loaded program"],
        timeout=timeout_s,
    )
    loaded = dashboard["get loaded program"]
    if not loaded_program_matches(loaded, EXPECTED_PROGRAM):
        raise ManualLiveError("Manual loaded program changed before ARM")
    return {
        "observed_at_unix_ns": time.time_ns(),
        "loaded_program_response": loaded,
        "program_state": dashboard["programState"],
        "safety_mode": dashboard["safetymode"],
        "expected_loaded_program": EXPECTED_PROGRAM,
    }


def _group_id(request: Mapping[str, Any]) -> str:
    source = str(request.get("source", ""))
    parts = source.split(":")
    if len(parts) < 2 or not parts[1].startswith("G"):
        raise ManualLiveError("manual request group identity differs")
    return parts[1]


def _terminal_observation(row: Mapping[str, str]) -> dict[str, int]:
    return {
        "campaign_epoch": int(float(row["ur_output_int_register_24"])),
        "trial_id": int(float(row["ur_output_int_register_25"])),
        "state": int(float(row["ur_output_int_register_26"])),
        "candidate_token": int(float(row["ur_output_int_register_27"])),
        "execution_profile_id": int(float(row["ur_output_int_register_29"])),
        "consumed_command_seq": int(float(row["ur_output_int_register_30"])),
        "logical_batch_sequence": int(float(row["ur_output_int_register_34"])),
        "batch_row_index": int(float(row["ur_output_int_register_31"])),
    }


def _arm_acknowledged(observed: Mapping[str, int], arm: HostPacket) -> bool:
    return bool(
        observed.get("state") in ARM_ACKNOWLEDGED_STATES
        and observed.get("campaign_epoch") == arm.campaign_epoch
        and observed.get("trial_id") == arm.trial_id
        and observed.get("candidate_token") == arm.candidate_token
        and observed.get("execution_profile_id") == arm.execution_profile_id
        and observed.get("consumed_command_seq") == arm.command_seq
        and observed.get("logical_batch_sequence") == arm.logical_batch_sequence
        and observed.get("batch_row_index") == 1
    )


def _score_capture(
    capture: Path,
    *,
    trial: TrialSpec,
    expected_arm: HostPacket,
    group_id: str,
) -> TrialScore:
    rows = read_csv_rows(capture)
    final_reason = int(float(rows[-1]["ur_output_int_register_28"]))
    assessment = TrialArtifactProducer(capture.parent.parent, trial).derive_assessment(
        expected_arm, expected_terminal_reason=final_reason
    )
    objective = fixed_f0_bin_metrics(
        rows,
        reaction_normal_base=trial.campaign.f0_shadow_reaction_normal_base or (),
    )
    profile = profile_metrics(
        rows,
        angular_rate_limit_rad_s=trial.execution_profile.normal_max_rate_rad_s,
        tp_accel_limit_rad_s2=trial.execution_profile.tp_speedj_accel_rad_s2,
    )
    tracking = qd_tracking_metrics(rows)
    force_norm = [
        float(row["force_norm_n"])
        for row in rows
        if row.get("force_norm_n") not in (None, "")
        and math.isfinite(float(row["force_norm_n"]))
    ]
    eligible = bool(
        final_reason == 1
        and assessment.completion_marker
        and assessment.cadence_ok
        and assessment.feedback_fresh
        and assessment.rnn_oracle_aligned
        and objective.get("complete_bins") == 550
        and isinstance(objective.get("mae_n"), (int, float))
        and profile.get("orientation_evidence_complete") is True
        and profile.get("orientation_qualified") is True
        and tracking.get("evidence_complete") is True
        and force_norm
    )
    return TrialScore(
        group_id=group_id,
        eligible=eligible,
        objective_mae_n=float(objective["mae_n"]) if eligible else None,
        gross_peak_force_norm_n=max(force_norm) if eligible else None,
        qdot_saturation_duty=(
            float(profile["qdot_saturation_duty"]) if eligible else None
        ),
    )


def _write_result(path: Path, payload: Mapping[str, Any]) -> None:
    atomic_json(path, dict(payload))


def _load_initial_scores(root: Path) -> dict[str, TrialScore]:
    scores: dict[str, TrialScore] = {}
    if not root.exists():
        return scores
    for path in sorted(root.glob("[0-9][0-9]-G[0-9][0-9].json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        request = payload.get("request")
        score = payload.get("score")
        if not isinstance(request, Mapping) or not isinstance(score, Mapping):
            raise ManualLiveError("persisted manual result fields differ")
        if int(request.get("logical_batch_sequence", 0)) > len(INITIAL_GROUPS):
            continue
        parsed = TrialScore(**score)
        if parsed.group_id in scores:
            raise ManualLiveError("persisted manual result repeats an initial group")
        scores[parsed.group_id] = parsed
    return scores


def _require_preplay_observation(observed: Mapping[str, int]) -> None:
    if observed["state"] == READY_HOME and observed["command"] == 0:
        return
    identity = (
        observed["campaign_epoch"],
        observed["trial_id"],
        observed["consumed_command_seq"],
    )
    if observed["state"] != 0 or identity != (0, 0, 0):
        raise ManualLiveError("pre-Play TP identity is neither zero nor READY_HOME")


def _wait_for_ready_home(
    args: argparse.Namespace,
    deadline: float,
    *,
    refresh_readiness_claim: Callable[[], Any] | None = None,
) -> dict[str, int]:
    next_claim_refresh = 0.0
    while time.monotonic() < deadline:
        if refresh_readiness_claim is not None and time.monotonic() >= next_claim_refresh:
            refresh_readiness_claim()
            next_claim_refresh = time.monotonic() + 0.5
        _, observed = validate_bridge(
            args.bridge_output_root,
            args.release_manifest_sha256,
            require_mailbox_absent=True,
        )
        _require_preplay_observation(observed)
        if observed["state"] == READY_HOME and observed["command"] == 0:
            return observed
        time.sleep(0.1)
    raise ManualLiveError("physical Play was not observed before timeout")


def _issue(args: argparse.Namespace, mailbox: Path) -> tuple[dict[str, Any], HostPacket, TrialSpec]:
    state = load_state(
        args.state,
        campaign_id=args.campaign_id,
        release_sha=args.release_manifest_sha256,
    )
    if state["inflight"] is None:
        prepared_intent = prepare_next_intent(
            queue_path=args.queue,
            state_path=args.state,
            campaign_id=args.campaign_id,
            release_manifest_sha256=args.release_manifest_sha256,
        )
        if prepared_intent is None:
            raise ManualLiveError("manual queue contains no request to ARM")

    prepared_values: tuple[HostPacket, Any] | None = None
    published_command: Any | None = None

    def sink(document: Mapping[str, Any]) -> None:
        nonlocal prepared_values, published_command
        prepared_values = _prepared(document, plant_epoch=args.plant_epoch)
        host, prepared = prepared_values
        command_mailbox = AtomicCommandMailbox(
            mailbox,
            network_mode=True,
            launch_profile=load_launch_profile(DEFAULT_LAUNCH_PROFILE),
        )
        command_mailbox.send_command(host, prepared_trial=prepared)
        published_command = command_mailbox.read_latest()
        if published_command is None or published_command.packet != host:
            raise ManualLiveError("manual ARM mailbox vanished before gate publication")
        attempt_id = os.environ.get("STEP5D_V3_LAUNCH_ATTEMPT_ID", "")
        authorization = load_capability_authorization(
            args.authorization_file,
            attempt_id=attempt_id,
            campaign_id=args.campaign_id,
            release_manifest_sha256=args.release_manifest_sha256,
        )
        atomic_json(
            mailbox.parent / "manual_arm_gate.json",
            {
                "schema": ARM_GATE_SCHEMA,
                "attempt_id": attempt_id,
                "campaign_id": args.campaign_id,
                "release_manifest_sha256": args.release_manifest_sha256,
                "created_at_unix_ns": time.time_ns(),
                "authorization": {
                    "path": str(args.authorization_file.expanduser().absolute()),
                    "sha256": hashlib.sha256(
                        args.authorization_file.read_bytes()
                    ).hexdigest(),
                },
                "arm_binding": published_command.arm_gate_binding,
            },
        )

    issued = issue_prepared_intent(
        state_path=args.state,
        campaign_id=args.campaign_id,
        release_manifest_sha256=args.release_manifest_sha256,
        sink=sink,
    )
    if prepared_values is None:
        raise ManualLiveError("manual ARM was not atomically published")
    host, prepared = prepared_values
    decoded = AtomicCommandMailbox(
        mailbox,
        network_mode=True,
        launch_profile=load_launch_profile(DEFAULT_LAUNCH_PROFILE),
    ).read_latest()
    if decoded is None or decoded.packet != host or decoded.binding.trial_uid != prepared.trial.trial_uid:
        raise ManualLiveError("manual ARM mailbox readback differs")
    if published_command is None or decoded.sha256 != published_command.sha256:
        raise ManualLiveError("manual ARM mailbox changed after gate publication")
    return issued, host, prepared.trial


def _complete_at_home(
    mailbox: Path,
    *,
    arm: HostPacket,
    prepared_trial: Any,
) -> None:
    complete = HostPacket(
        campaign_epoch=arm.campaign_epoch,
        trial_id=arm.trial_id,
        command=HostCommand.COMPLETE_AT_HOME,
        candidate_token=arm.candidate_token,
        execution_profile_id=arm.execution_profile_id,
        command_seq=arm.command_seq + 1,
        logical_batch_sequence=arm.logical_batch_sequence,
    )
    AtomicCommandMailbox(
        mailbox,
        network_mode=True,
        launch_profile=load_launch_profile(DEFAULT_LAUNCH_PROFILE),
    ).send_command(complete, prepared_trial=prepared_trial)


def run(args: argparse.Namespace) -> dict[str, Any]:
    args.campaign_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    validate_manual_qualification(
        ROOT,
        args.qualification_result,
        release_manifest_sha256=args.release_manifest_sha256,
    )
    seed_initial_grid(
        args.queue,
        campaign_id=args.campaign_id,
        release_manifest_sha256=args.release_manifest_sha256,
        launch_profile_path=args.launch_profile,
    )
    queue = load_queue(args.queue)
    completed = 0
    if args.state.exists():
        completed = len(load_state(
            args.state,
            campaign_id=args.campaign_id,
            release_sha=args.release_manifest_sha256,
        )["completed_sequences"])
    next_group = _group_id(queue["requests"][completed])
    attempt_id = os.environ.get("STEP5D_V3_LAUNCH_ATTEMPT_ID", "")
    if not attempt_id:
        raise ManualLiveError("canonical launch attempt identity is missing")
    _publish_status(
        args,
        state="BRIDGE_ALIVE_NO_ARM",
        observed=None,
        completed=completed,
        total=len(INITIAL_GROUPS),
        next_group=next_group,
        launch_attempt_id=attempt_id,
    )
    mailbox, _ = validate_bridge(
        args.bridge_output_root,
        args.release_manifest_sha256,
        require_mailbox_absent=True,
    )
    authorization_deadline = time.monotonic() + args.play_timeout_s
    while True:
        if not args.authorization_file.exists() and not args.authorization_file.is_symlink():
            if time.monotonic() >= authorization_deadline:
                raise ManualLiveError("explicit Play/ARM/motion authorization was not supplied")
            time.sleep(0.1)
            continue
        authorization = load_capability_authorization(
            args.authorization_file,
            attempt_id=attempt_id,
            campaign_id=args.campaign_id,
            release_manifest_sha256=args.release_manifest_sha256,
        )
        break
    capabilities = authorization["capabilities"]

    def require_current_authorization() -> None:
        current = load_capability_authorization(
            args.authorization_file,
            attempt_id=attempt_id,
            campaign_id=args.campaign_id,
            release_manifest_sha256=args.release_manifest_sha256,
        )
        if current["capabilities"] != capabilities:
            raise ManualLiveError("Manual authorization capabilities changed")

    def refresh_preplay_claim() -> dict[str, Any]:
        require_current_authorization()
        _, bridge_observation = validate_bridge(
            args.bridge_output_root,
            args.release_manifest_sha256,
            require_mailbox_absent=True,
        )
        _require_preplay_observation(bridge_observation)
        controller = _observe_controller_identity(args.robot_host)
        _publish_status(
            args,
            state="WAITING_FOR_IDENTITY_PLAY",
            observed=None,
            completed=completed,
            total=len(INITIAL_GROUPS),
            next_group=next_group,
            capabilities=capabilities,
            authorization_file=args.authorization_file,
            controller_observation=controller,
            launch_attempt_id=attempt_id,
        )
        _publish_canonical_readiness_claim(args, "WAITING_FOR_IDENTITY_PLAY")
        return controller

    controller_observation = refresh_preplay_claim()
    observed = _wait_for_ready_home(
        args,
        time.monotonic() + args.play_timeout_s,
        refresh_readiness_claim=refresh_preplay_claim,
    )
    require_current_authorization()
    controller_observation = _observe_controller_identity(args.robot_host)
    _publish_status(
        args,
        state="PLAY_OBSERVED_IDENTITY_RECHECKED",
        observed=observed,
        completed=completed,
        total=len(INITIAL_GROUPS),
        next_group=next_group,
        capabilities=capabilities,
        authorization_file=args.authorization_file,
        controller_observation=controller_observation,
        launch_attempt_id=attempt_id,
    )
    if not args.state.exists() and not args.state.is_symlink():
        seed_home_state(
            args.state,
            campaign_id=args.campaign_id,
            release_sha=args.release_manifest_sha256,
            campaign_epoch=max(1, observed["campaign_epoch"]),
            last_trial_id=observed["trial_id"],
            last_command_seq=observed["consumed_command_seq"],
        )
    results_root = args.campaign_root / "manual_results"
    score_by_group = _load_initial_scores(results_root)
    last_arm: HostPacket | None = None
    last_prepared: Any | None = None
    while True:
        queue = load_queue(args.queue)
        state = load_state(
            args.state,
            campaign_id=args.campaign_id,
            release_sha=args.release_manifest_sha256,
        )
        completed = len(state["completed_sequences"])
        if completed == len(INITIAL_GROUPS) and len(queue["requests"]) == len(INITIAL_GROUPS):
            if len(score_by_group) != len(INITIAL_GROUPS):
                raise ManualLiveError("all 18 canonical scores are required before Top-2")
            append_top2_repeats(
                args.queue,
                campaign_id=args.campaign_id,
                release_manifest_sha256=args.release_manifest_sha256,
                launch_profile_path=args.launch_profile,
                scores=score_by_group.values(),
            )
            queue = load_queue(args.queue)
        if completed >= len(queue["requests"]):
            close_queue(args.queue, reason="governed_manual_v2_complete")
            if last_arm is not None and last_prepared is not None:
                _complete_at_home(
                    mailbox,
                    arm=last_arm,
                    prepared_trial=last_prepared,
                )
            return _publish_status(
                args,
                state="COMPLETE",
                observed=observed,
                completed=completed,
                total=len(queue["requests"]),
                next_group=None,
                capabilities=capabilities,
                authorization_file=args.authorization_file,
                controller_observation=controller_observation,
                launch_attempt_id=attempt_id,
            )
        request = queue["requests"][completed]
        require_current_authorization()
        controller_observation = _observe_controller_identity(args.robot_host)
        issued, arm, trial = _issue(args, mailbox)
        _, last_prepared = _prepared(issued, plant_epoch=args.plant_epoch)
        last_arm = arm
        group_id = _group_id(request)
        _publish_status(
            args,
            state="ARM_PENDING",
            observed=observed,
            completed=completed,
            total=len(queue["requests"]),
            next_group=group_id,
            capabilities=capabilities,
            authorization_file=args.authorization_file,
            controller_observation=controller_observation,
            launch_attempt_id=attempt_id,
        )
        deadline = time.monotonic() + args.trial_timeout_s
        running_published = False
        while time.monotonic() < deadline:
            row = _latest_bridge_row(
                args.bridge_output_root / "runtime/bridge/bridge_rtde_500hz.csv"
            )
            terminal = _terminal_observation(row)
            if not running_published and _arm_acknowledged(terminal, arm):
                _publish_status(
                    args,
                    state="RUNNING",
                    observed=terminal,
                    completed=completed,
                    total=len(queue["requests"]),
                    next_group=group_id,
                    capabilities=capabilities,
                    authorization_file=args.authorization_file,
                    controller_observation=controller_observation,
                    launch_attempt_id=attempt_id,
                )
                running_published = True
            expected = {
                "campaign_epoch": arm.campaign_epoch,
                "trial_id": arm.trial_id,
                "state": READY_HOME_NEXT,
                "candidate_token": arm.candidate_token,
                "execution_profile_id": arm.execution_profile_id,
                "consumed_command_seq": arm.command_seq,
                "logical_batch_sequence": arm.logical_batch_sequence,
                "batch_row_index": 1,
            }
            if terminal == expected:
                observed = {
                    "state": terminal["state"],
                    "campaign_epoch": terminal["campaign_epoch"],
                    "trial_id": terminal["trial_id"],
                    "consumed_command_seq": terminal["consumed_command_seq"],
                    "command": int(float(row["command"])),
                    "controller_state": int(float(row["step4e_controller_state"])),
                    "safety_mode": int(float(row["ur_safety_mode"])),
                }
                break
            time.sleep(0.1)
        else:
            raise ManualLiveError(f"{group_id} did not reach exact READY_HOME_NEXT")
        capture = (
            args.bridge_output_root
            / "runtime/bridge/autotune_trials"
            / trial.trial_uid
            / "capture.csv"
        )
        capture_deadline = time.monotonic() + 5.0
        while not capture.is_file() and time.monotonic() < capture_deadline:
            time.sleep(0.05)
        score = _score_capture(
            capture,
            trial=trial,
            expected_arm=arm,
            group_id=group_id,
        )
        if request["logical_batch_sequence"] <= len(INITIAL_GROUPS):
            score_by_group[group_id] = score
        _write_result(
            results_root / f"{request['logical_batch_sequence']:02d}-{group_id}.json",
            {
                "schema": "step5d.manual-v2/trial-result-v1",
                "request": request,
                "intent_sha256": issued["intent_sha256"],
                "trial_uid": trial.trial_uid,
                "capture": str(capture),
                "score": score.__dict__,
                "terminal_observation": terminal,
            },
        )
        confirm_home_complete(
            state_path=args.state,
            campaign_id=args.campaign_id,
            release_manifest_sha256=args.release_manifest_sha256,
            observed=terminal,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-output-root", type=Path, required=True)
    parser.add_argument("--campaign-root", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--release-manifest-sha256", required=True)
    parser.add_argument("--authorization-file", type=Path, required=True)
    parser.add_argument("--qualification-result", type=Path, required=True)
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--launch-profile", type=Path, default=DEFAULT_LAUNCH_PROFILE)
    parser.add_argument("--plant-epoch", type=int, default=1)
    parser.add_argument("--play-timeout-s", type=float, default=900.0)
    parser.add_argument("--trial-timeout-s", type=float, default=180.0)
    args = parser.parse_args()
    try:
        require_canonical_shell()
        print(json.dumps(run(args), indent=2, sort_keys=True))
    except Exception as exc:
        try:
            if (args.bridge_output_root / "bridge_launch.json").is_file():
                _publish_status(
                    args,
                    state="BLOCKED",
                    observed=None,
                    completed=0,
                    total=len(INITIAL_GROUPS),
                    next_group=None,
                    blocker=f"{type(exc).__name__}:{exc}",
                    launch_attempt_id=os.environ.get(
                        "STEP5D_V3_LAUNCH_ATTEMPT_ID", ""
                    ),
                )
        except Exception:
            pass
        print(f"manual live campaign blocked: {exc}", file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
