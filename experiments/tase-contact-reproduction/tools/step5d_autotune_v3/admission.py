"""Offline exact first-row admission for the active rolling campaign."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any, Mapping

from step5d_autotune_batch_plan import (
    PlanLifecycle,
    SCHEMA_VERSION_ROLLING_V2,
    load_plan,
)
from step5d_autotune_contract import ExecutionProfile
from step5d_autotune_live_driver import (
    BridgeMailboxRuntime,
    decode_execution_profile_id,
)
from step5d_autotune_runtime_lifecycle import next_runtime_plan_row
from step5d_autotune_state_machine import HostCommand, HostPacket, TpLoopState

from .release_identity import (
    ROLLING_EXECUTION_PROFILE_BINDINGS,
    ReleaseIdentity,
)
from .release_verifier import _state_write_order
from .runtime_profile import (
    load_launch_profile,
    normalized_overlay_sha256,
    normalize_trial_overlay,
)
from .state import CampaignPaths, read_strict_json


ADMISSION_SCHEMA = "step5d.autotune-v3/first-row-admission-v1"
EXPECTED_STATE_WRITE_ORDER = (24, 25, 27, 28, 29, 31, 32, 33, 34, 26, 30)


class FirstRowAdmissionError(RuntimeError):
    """Campaign bytes cannot safely form the first rolling ARM transaction."""


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise FirstRowAdmissionError(f"admission artifact is missing or unsafe: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _overlay_for_first_row(
    paths: CampaignPaths,
    *,
    plan: Any,
    selected: Any,
    launch_profile_path: Path,
    tp_program_id: str,
) -> tuple[dict[str, Any], str]:
    payload = read_strict_json(paths.trial_overlays, role="first-row overlay plan")
    profile = load_launch_profile(
        launch_profile_path, expected_tp_program_id=tp_program_id
    )
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema") != "step5d.autotune-v3/trial-overlay-plan-v2"
        or payload.get("revision") != plan.revision
        or payload.get("launch_profile_fingerprint") != profile.fingerprint
        or not isinstance(payload.get("batches"), list)
    ):
        raise FirstRowAdmissionError("campaign overlay plan identity differs")
    matches = [
        row
        for batch in payload["batches"]
        if isinstance(batch, Mapping) and isinstance(batch.get("trials"), list)
        for row in batch["trials"]
        if isinstance(row, Mapping)
        and row.get("transport_candidate_uid") == selected.transport_candidate_uid
    ]
    if len(matches) != 1:
        raise FirstRowAdmissionError("first transport UID must select exactly one overlay")
    row = matches[0]
    if any(
        (
            row.get("occurrence_uid") != selected.occurrence_uid,
            row.get("control_candidate_uid") != selected.control_candidate_uid,
        )
    ):
        raise FirstRowAdmissionError("first-row overlay cross-namespace substitution")
    normalized = normalize_trial_overlay(row.get("overlay"), profile=profile)
    overlay_sha256 = normalized_overlay_sha256(profile, normalized)
    if (
        normalized["control_candidate_uid"] != selected.control_candidate_uid
        or row.get("normalized_overlay_sha256") != overlay_sha256
    ):
        raise FirstRowAdmissionError("first-row normalized overlay closure differs")
    selected_with_overlay = selected.with_overlay(normalized)
    return dict(selected_with_overlay.overlay or {}), overlay_sha256


def _dry_run_commit_transcript(
    packet: HostPacket,
    *,
    ready_consumed_command_seq: int,
    write_order: tuple[int, ...],
) -> list[dict[str, int]]:
    values = {index: 0 for index in range(24, 35)}
    values[26] = int(TpLoopState.READY_HOME)
    values[30] = ready_consumed_command_seq
    published = {
        24: packet.campaign_epoch,
        25: packet.trial_id,
        27: packet.candidate_token,
        28: 0,
        29: packet.execution_profile_id,
        31: 1,
        32: 2,
        33: 0,
        34: packet.logical_batch_sequence,
        26: int(TpLoopState.ARMED),
        30: packet.command_seq,
    }
    transcript: list[dict[str, int]] = []
    for register in write_order:
        values[register] = published[register]
        transcript.append(
            {
                "written_register": register,
                "state": values[26],
                "consumed_command_seq": values[30],
            }
        )
        if values[30] != packet.command_seq and values[26] not in {
            int(TpLoopState.READY_HOME),
            int(TpLoopState.ARMED),
        }:
            raise FirstRowAdmissionError("partial ARM transcript can enter motion state")
    if any(
        (
            values[24] != packet.campaign_epoch,
            values[25] != packet.trial_id,
            values[27] != packet.candidate_token,
            values[29] != packet.execution_profile_id,
            values[31] != 1,
            values[34] != packet.logical_batch_sequence,
            values[26] != int(TpLoopState.ARMED),
            values[30] != packet.command_seq,
        )
    ):
        raise FirstRowAdmissionError("committed ARM transcript identity differs")
    return transcript


def verify_first_row_admission(
    root: Path,
    *,
    campaign_root: Path,
    launch_profile_path: Path,
    campaign_epoch: int,
    ready_consumed_command_seq: int,
    release: ReleaseIdentity,
) -> dict[str, Any]:
    if (
        isinstance(campaign_epoch, bool)
        or not isinstance(campaign_epoch, int)
        or campaign_epoch < 1
    ):
        raise FirstRowAdmissionError("campaign epoch must be positive")
    if (
        isinstance(ready_consumed_command_seq, bool)
        or not isinstance(ready_consumed_command_seq, int)
        or ready_consumed_command_seq < 0
    ):
        raise FirstRowAdmissionError("ready consumed sequence must be nonnegative")
    paths = CampaignPaths(campaign_root)
    plan = load_plan(paths.candidate_plan)
    if (
        plan.payload.get("schema_version") != SCHEMA_VERSION_ROLLING_V2
        or plan.lifecycle is not PlanLifecycle.OPEN_READY
        or plan.revision < 1
    ):
        raise FirstRowAdmissionError("active campaign must use an OPEN_READY rolling-v2 plan")
    selected = next_runtime_plan_row(plan=plan, campaign_root=paths.root)
    if selected is None or selected.logical_batch_sequence != 1 or selected.row_index != 1:
        raise FirstRowAdmissionError("campaign first row is not the exact Batch A row 1")
    overlay, overlay_sha256 = _overlay_for_first_row(
        paths,
        plan=plan,
        selected=selected,
        launch_profile_path=launch_profile_path,
        tp_program_id=release.program_id,
    )
    script_path = root / str(release.artifacts[".script"]["path"])
    write_order = _state_write_order(script_path.read_text(encoding="utf-8"))
    if write_order != EXPECTED_STATE_WRITE_ORDER:
        raise FirstRowAdmissionError("active TP state write order lacks commit-last semantics")

    from run_step5d_autotune_v3_bridge import build_release_mailbox_runtime

    runtime = build_release_mailbox_runtime(
        BridgeMailboxRuntime,
        (paths.control / "first_row_admission_dry_run_mailbox.json").absolute(),
        release=release,
    )
    if runtime.completion_protocol != release.protocol_id:
        raise FirstRowAdmissionError("production wrapper constructed a different protocol")
    normal_rate, profile_integer_id = ROLLING_EXECUTION_PROFILE_BINDINGS[
        release.execution_profile_id
    ]
    decoded_normal, host_slew, tp_accel = decode_execution_profile_id(
        profile_integer_id,
        network_mode=True,
    )
    if not math.isclose(decoded_normal, normal_rate, rel_tol=0.0, abs_tol=1e-12):
        raise FirstRowAdmissionError(
            "release binding normal-rate differs from the integer codec"
        )
    high_dynamics = normal_rate >= 0.5
    profile = ExecutionProfile(
        release.execution_profile_id,
        normal_rate,
        host_slew,
        tp_accel,
        qdot_cap_rad_s=2.5 if high_dynamics else 0.5,
        bridge_angular_limit_rad_s=0.25 if high_dynamics else 0.05,
    )
    if overlay["execution_profile_id"] != profile.profile_id:
        raise FirstRowAdmissionError("first overlay uses a different execution profile")
    packet = HostPacket(
        campaign_epoch=campaign_epoch,
        trial_id=1,
        command=HostCommand.ARM,
        candidate_token=1,
        execution_profile_id=profile_integer_id,
        command_seq=ready_consumed_command_seq + 1,
        logical_batch_sequence=1,
    )
    transcript = _dry_run_commit_transcript(
        packet,
        ready_consumed_command_seq=ready_consumed_command_seq,
        write_order=write_order,
    )
    return {
        "schema": ADMISSION_SCHEMA,
        "ok": True,
        "protocol_id": runtime.completion_protocol,
        "candidate_plan_sha256": _sha256(paths.candidate_plan),
        "trial_overlay_plan_sha256": _sha256(paths.trial_overlays),
        "plan_revision": plan.revision,
        "logical_batch_sequence": selected.logical_batch_sequence,
        "row_index": selected.row_index,
        "occurrence_uid": str(selected.occurrence_uid),
        "transport_candidate_uid": str(selected.transport_candidate_uid),
        "control_candidate_uid": str(selected.control_candidate_uid),
        "normalized_overlay_sha256": overlay_sha256,
        "state_write_order": list(write_order),
        "would_be_arm_packet": {
            "campaign_epoch": packet.campaign_epoch,
            "trial_id": packet.trial_id,
            "command": int(packet.command),
            "candidate_token": packet.candidate_token,
            "execution_profile_id": packet.execution_profile_id,
            "command_seq": packet.command_seq,
            "logical_batch_sequence": packet.logical_batch_sequence,
        },
        "commit_transcript": transcript,
    }


__all__ = [
    "ADMISSION_SCHEMA",
    "FirstRowAdmissionError",
    "verify_first_row_admission",
]
