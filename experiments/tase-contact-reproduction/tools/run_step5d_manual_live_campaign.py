#!/usr/bin/env python3
"""Bind one durable manual intent to a running NO_ARM bridge mailbox."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from step5d_autotune_contract import ForceCandidate, NORMAL_FILTER_PROFILES
from step5d_autotune_live_driver import AtomicCommandMailbox
from step5d_autotune_state_machine import HostCommand, HostPacket
from step5d_autotune_v3.profile import canonical_json_bytes
from step5d_autotune_v3.runtime_profile import (
    DEFAULT_LAUNCH_PROFILE,
    load_launch_profile,
    normalized_overlay_sha256,
    normalize_trial_overlay,
)
from step5d_manual_bridge import PROGRAM, PROTOCOL, strict_object
from step5d_manual_runtime import (
    issue_prepared_intent,
    load_state,
    prepare_next_intent,
    seed_home_state,
)


BACKEND_ID = "step5d_manual_hold_v1"
READY_HOME = 90


class ManualLiveError(RuntimeError):
    pass


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


def validate_bridge(output_root: Path, release_sha: str) -> tuple[Path, dict[str, int]]:
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
    if mailbox != expected or mailbox.exists() or mailbox.is_symlink():
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
        observed["state"] != READY_HOME,
        observed["command"] != 0,
        observed["controller_state"] != 0,
        observed["safety_mode"] != 1,
        observed["campaign_epoch"] < 1,
        observed["trial_id"] < 1,
        observed["consumed_command_seq"] < 1,
    )):
        raise ManualLiveError("bridge/TP is not stationary READY_HOME command=0")
    return mailbox, observed


def _prepared(intent: Mapping[str, Any]) -> tuple[HostPacket, Any]:
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
    campaign = SimpleNamespace(
        campaign_epoch=packet["campaign_epoch"],
        campaign_fingerprint=campaign_fingerprint,
    )
    trial = SimpleNamespace(
        campaign=campaign,
        trial_id=packet["trial_id"],
        candidate_token=packet["candidate_token"],
        command_seq=packet["command_seq"],
        trial_uid=intent["request_identity"]["occurrence_uid"],
        backend_id=BACKEND_ID,
        candidate=candidate,
        execution_profile=profile,
        source_fingerprint=source_fingerprint,
        config_fingerprint=config_fingerprint,
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


def run(args: argparse.Namespace) -> dict[str, Any]:
    mailbox, observed = validate_bridge(args.bridge_output_root, args.release_manifest_sha256)
    if not args.state.exists() and not args.state.is_symlink():
        seed_home_state(
            args.state,
            campaign_id=args.campaign_id,
            release_sha=args.release_manifest_sha256,
            campaign_epoch=observed["campaign_epoch"],
            last_trial_id=observed["trial_id"],
            last_command_seq=observed["consumed_command_seq"],
        )
        prepared = prepare_next_intent(
            queue_path=args.queue,
            state_path=args.state,
            campaign_id=args.campaign_id,
            release_manifest_sha256=args.release_manifest_sha256,
        )
        if prepared is None:
            raise ManualLiveError("manual queue contains no request to ARM")
    state = load_state(
        args.state,
        campaign_id=args.campaign_id,
        release_sha=args.release_manifest_sha256,
    )
    intent = state.get("inflight")
    if not isinstance(intent, Mapping):
        raise ManualLiveError("manual durable inflight intent is missing")
    packet = intent["packet"]
    if any((
        packet["campaign_epoch"] != observed["campaign_epoch"],
        packet["trial_id"] <= observed["trial_id"],
        packet["command_seq"] <= observed["consumed_command_seq"],
        intent["overlay"]["force_i_gain"] != 0.0001,
    )):
        raise ManualLiveError("manual ARM intent is stale or not exact I=1e-4")
    authorization = {
        "schema": "step5d.manual-hold/arm-authorization-v1",
        "authorized_at": datetime.now(timezone.utc).isoformat(),
        "authorization_source": "explicit_current_turn_user_request",
        "intent_sha256": intent["intent_sha256"],
        "bridge_output_root": str(args.bridge_output_root.resolve()),
        "arm_authorized": True,
        "motion_authorized": True,
    }
    auth_path = args.state.parent / "arm_authorization.json"
    _write_once(auth_path, authorization)

    def sink(document: Mapping[str, Any]) -> None:
        host, prepared = _prepared(document)
        AtomicCommandMailbox(mailbox, network_mode=True).send_command(
            host, prepared_trial=prepared
        )

    issued = issue_prepared_intent(
        state_path=args.state,
        campaign_id=args.campaign_id,
        release_manifest_sha256=args.release_manifest_sha256,
        sink=sink,
    )
    decoded = AtomicCommandMailbox(mailbox, network_mode=True).read_latest()
    if decoded is None or decoded.packet.command is not HostCommand.ARM:
        raise ManualLiveError("manual ARM mailbox readback differs")
    result = {
        "schema": "step5d.manual-hold/arm-staged-v1",
        "ok": True,
        "mailbox": str(mailbox),
        "intent_sha256": issued["intent_sha256"],
        "campaign_epoch": decoded.packet.campaign_epoch,
        "trial_id": decoded.packet.trial_id,
        "command_seq": decoded.packet.command_seq,
        "candidate_token": decoded.packet.candidate_token,
        "force_i_gain": decoded.binding.candidate.force_i_gain,
        "arm_authorized": True,
        "motion_authorized": True,
        "operator_action": "press_play_on_loaded_manual_program",
    }
    result_path = args.state.parent / "arm_staged.json"
    _write_once(result_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-output-root", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--release-manifest-sha256", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args), indent=2, sort_keys=True))
    except (OSError, ValueError, ManualLiveError) as exc:
        print(f"manual live campaign blocked: {exc}", file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
