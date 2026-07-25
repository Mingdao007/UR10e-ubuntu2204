#!/usr/bin/env python3
"""Run V3 from an independent, unbounded parameter receiver."""

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
from typing import Any, Mapping

from prepare_step5d_autotune_launch import _sha256_path
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
from step5d_parameter_queue import (
    bind_home,
    finish_dispatch,
    load_state,
    prepare_next_dispatch,
    status as receiver_status,
)
from step5d_production_csv import BridgeCsvFollower, BridgeCsvTimeout


STATUS_SCHEMA = "step5d.parameter-receiver/live-status-v1"
READY_HOME = int(TpLoopState.READY_HOME)
READY_HOME_NEXT = int(TpLoopState.READY_HOME_NEXT)
TERMINAL_CAPTURE_WAIT_S = 3.0


class ParameterCampaignError(RuntimeError):
    pass


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


def _publish_status(
    args: argparse.Namespace,
    *,
    state: str,
    observation: Mapping[str, int] | None,
    blocker: str | None = None,
) -> dict[str, Any]:
    queue = receiver_status(args.receiver_root)
    payload = {
        "schema": STATUS_SCHEMA,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "route": "autotune_v3_parameter_receiver",
        "state": state,
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
            "press Play once"
            if state == "WAITING_FOR_PLAY"
            else "submit another parameter; TP remains stationary at Home"
            if state == "WAITING_FOR_PARAMETERS"
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
    mailbox.send_command(arm, prepared_trial=prepared)
    decoded = mailbox.read_latest()
    if decoded is None or decoded.packet != arm:
        raise ParameterCampaignError("receiver ARM mailbox readback differs")
    return arm, prepared


def _capture_health(path: Path) -> tuple[str, str | None]:
    deadline = time.monotonic() + TERMINAL_CAPTURE_WAIT_S
    while time.monotonic() < deadline:
        if path.is_file() and not path.is_symlink():
            break
        time.sleep(0.01)
    if path.is_symlink() or not path.is_file():
        return "DATA_ISSUE", "capture missing after terminal Home"
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
    _publish_status(args, state="WAITING_FOR_PLAY", observation=None)
    while True:
        try:
            for row in follower.rows(timeout_s=1.0):
                observed = _tp_observation(row)
                if observed["safety_mode"] != 1:
                    raise ParameterCampaignError("UR Safety left NORMAL before Play")
                if observed["state"] == READY_HOME:
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
        except BridgeCsvTimeout as exc:
            if exc.code == "no_fresh_rows":
                raise ParameterCampaignError("bridge stopped publishing before Play") from exc


def _wait_next_dispatch(
    args: argparse.Namespace,
    follower: BridgeCsvFollower,
    observation: Mapping[str, int],
) -> dict[str, Any]:
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
            for row in follower.rows(timeout_s=0.5):
                current = _tp_observation(row)
                if not _safe_home(current):
                    raise ParameterCampaignError(
                        "TP left safe Home while receiver queue was empty"
                    )
                dispatch = prepare_next_dispatch(args.receiver_root)
                if dispatch is not None:
                    return dispatch
        except BridgeCsvTimeout as exc:
            if exc.code == "no_fresh_rows":
                raise ParameterCampaignError(
                    "bridge stopped publishing while receiver queue was empty"
                ) from exc


def _wait_terminal(
    follower: BridgeCsvFollower,
    *,
    arm: HostPacket,
    timeout_s: float,
) -> tuple[dict[str, int], dict[str, str]]:
    for row in follower.rows(timeout_s=timeout_s):
        observed = _tp_observation(row)
        if observed["safety_mode"] != 1:
            raise ParameterCampaignError("UR Safety left NORMAL during trial")
        if observed["state"] != READY_HOME_NEXT:
            continue
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
        if _terminal_identity(observed) != expected:
            raise ParameterCampaignError(
                "terminal Home identity differs from dispatched ARM"
            )
        return observed, row
    raise AssertionError("unreachable")


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
    if queue_state["inflight"] is not None:
        raise ParameterCampaignError(
            "unresolved inflight dispatch; automatic physical replay is forbidden"
        )
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
    observed = _wait_initial_home(args, follower)
    bind_home(
        args.receiver_root,
        campaign_epoch=max(1, observed["campaign_epoch"]),
        last_trial_id=observed["trial_id"],
        last_command_seq=observed["consumed_command_seq"],
    )
    while True:
        dispatch = _wait_next_dispatch(args, follower, observed)
        arm, prepared = _send(args, binding=binding, dispatch=dispatch)
        _publish_status(args, state="ARM_PENDING", observation=observed)
        observed, _row = _wait_terminal(
            follower,
            arm=arm,
            timeout_s=args.trial_timeout_s,
        )
        capture = (
            args.bridge_run
            / "autotune_trials"
            / prepared.trial.trial_uid
            / "capture.csv"
        )
        result_status, detail = _capture_health(capture)
        receipt = finish_dispatch(
            args.receiver_root,
            status=result_status,
            observed=_terminal_identity(observed),
            detail=detail,
        )
        result = {
            "schema": "step5d.parameter-receiver/trial-result-v1",
            "request_uid": dispatch["request"]["request_uid"],
            "dispatch_sequence": dispatch["dispatch_sequence"],
            "trial_uid": prepared.trial.trial_uid,
            "capture": str(capture),
            "capture_sha256": (
                _sha256_path(capture) if result_status == "COMPLETE" else None
            ),
            "receipt": receipt,
        }
        encoded = canonical_json_bytes(result)
        result_path = (
            args.campaign_root
            / "parameter_results"
            / f"{dispatch['dispatch_sequence']:012d}.json"
        )
        result_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if result_path.exists() or result_path.is_symlink():
            if result_path.is_symlink() or result_path.read_bytes() != encoded:
                raise ParameterCampaignError("immutable parameter result differs")
        else:
            result_path.write_bytes(encoded)


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
    parser.add_argument("--trial-timeout-s", type=float, default=180.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        run(args)
    except Exception as exc:
        try:
            if (args.receiver_root / "state.json").is_file():
                _publish_status(
                    args,
                    state="BLOCKED",
                    observation=None,
                    blocker=f"{type(exc).__name__}:{exc}",
                )
        except Exception:
            pass
        print(f"parameter campaign blocked: {type(exc).__name__}:{exc}", file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
