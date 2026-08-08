#!/usr/bin/env python3
"""Execute fail-closed TacDiffusion formal V4 qualification and campaigns.

All dataset/model operations remain offline or model-inactive.  Live commands
reuse the accepted Direct Torque transport and Kunwei KWR75 raw TCP owner.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
import time
from typing import Any, Mapping, Sequence

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parents[1]
VIC_ROOT = ROOT.parent / "ur10e-variable-impedance"
for import_root in (ROOT / "tools", VIC_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

import run_tacdiffusion_remote_direct_torque_v4 as legacy  # noqa: E402
from _ur_common import read_rtde_once  # noqa: E402
from ur10e_vic.tacdiffusion.contracts import (  # noqa: E402
    ContactGuardProfileV1,
    FormalEpisodeManifestV1,
    KunweiOnlyForceAuthorityV1,
)
from ur10e_vic.tacdiffusion.eligibility import (  # noqa: E402
    FormalEligibilityValidator,
)
from ur10e_vic.tacdiffusion.episode_recorder import (  # noqa: E402
    FormalEpisodeRecorder,
    read_episode_artifact,
    validate_formal_episode_artifact,
)
from ur10e_vic.tacdiffusion.direct_torque_live_v4 import (  # noqa: E402
    LiveTubeContract,
    build_live_receiver_source,
    parse_live_receiver_source,
)
from ur10e_vic.tacdiffusion.formal_dynamics import (  # noqa: E402
    ProductionDynamicsRuntimeV1,
    load_formal_dynamics_conformance_receipt,
)
from ur10e_vic.tacdiffusion.formal_episode import FormalFrameComposerV1  # noqa: E402
from ur10e_vic.tacdiffusion.formal_equipment import (  # noqa: E402
    FORMAL_EQUIPMENT_READBACK_SCHEMA_V1,
    load_formal_equipment_contract,
)
from ur10e_vic.tacdiffusion.formal_artifacts import (  # noqa: E402
    load_sealed_formal_predictor,
    train_and_seal_formal_checkpoint,
)
from ur10e_vic.tacdiffusion.formal_benchmark import (  # noqa: E402
    benchmark_formal_50_step_sampler,
    validate_formal_50_step_sampler_candidate,
)
from ur10e_vic.tacdiffusion.formal_campaign import (  # noqa: E402
    load_formal_campaign_source_contract,
)
from ur10e_vic.tacdiffusion.formal_dataset import (  # noqa: E402
    build_formal_campaign_dataset,
)
from ur10e_vic.tacdiffusion.formal_identity import (  # noqa: E402
    resolve_formal_current_state,
)
from ur10e_vic.tacdiffusion.formal_orchestration import (  # noqa: E402
    ContactAcquisitionContractV1,
    FormalAttemptOutcome,
    FormalAttemptPhase,
    FormalAttemptReceiptV1,
    FormalCampaignLedgerV1,
    classify_fault,
)
from ur10e_vic.tacdiffusion.formal_trajectory import (  # noqa: E402
    build_formal_trajectory_timeline,
)
from ur10e_vic.tacdiffusion.trajectory import TRAJECTORY_FAMILIES  # noqa: E402
from ur10e_vic.tacdiffusion.expert import FixedKExpertV1, VariableKExpertV1  # noqa: E402


FORMAL_QUALIFICATION_SCHEMA_V1 = (
    "ur10e_tacdiffusion_formal_trajectory_qualification/v1"
)
FORMAL_TOOL_SCHEMA_V1 = "ur10e_tacdiffusion_formal_runtime/v1"
U_AXIS_BASE = (0.9995861541165553, -0.028766656018281204, 0.0)
V_AXIS_BASE = (0.028766656018281198, 0.9995861541165553, 0.0)
CAMPAIGN_CONTRACT_PATH = (
    VIC_ROOT / "config" / "tacdiffusion_formal_v4_campaign_contract.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json_new(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _replace_json(path: Path, payload: Mapping[str, Any]) -> None:
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(dict(payload), handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _campaign(kind: str):
    source = load_formal_campaign_source_contract(CAMPAIGN_CONTRACT_PATH)
    if kind == "fixed_k":
        return source.fixed_campaign
    if kind == "variable_k":
        return source.variable_campaign
    raise ValueError("formal campaign kind must be fixed_k or variable_k")


def run_resolve(_: argparse.Namespace) -> dict[str, Any]:
    result = resolve_formal_current_state(REPOSITORY_ROOT)
    result["contact_acquisition"] = ContactAcquisitionContractV1().as_json()
    return result


def run_init_campaign(args: argparse.Namespace) -> dict[str, Any]:
    identity = resolve_formal_current_state(REPOSITORY_ROOT)
    if identity.get("ok") is not True:
        raise RuntimeError(f"formal_identity_blocked:{identity.get('blockers')}")
    contract = _campaign(args.kind)
    if contract.kind == "variable_k":
        fixed = FormalCampaignLedgerV1(args.fixed_campaign_root.resolve(), _campaign("fixed_k"))
        if not fixed.progress().complete:
            raise RuntimeError("variable_k_requires_complete_fixed_k_campaign")
        if args.fixed_k_qualification is None:
            raise RuntimeError("variable_k_requires_fixed_k_qualification_receipt")
        qualification = json.loads(args.fixed_k_qualification.read_text(encoding="utf-8"))
        if (
            not isinstance(qualification, Mapping)
            or qualification.get("fixed_campaign_complete") is not True
            or qualification.get("k_load_qualified") is not True
        ):
            raise RuntimeError("variable_k_fixed_k_qualification_not_accepted")
    bindings = {
        "source_content": str(identity["source_content_sha256"]),
        "campaign_contract": _sha256(CAMPAIGN_CONTRACT_PATH),
        "formal_source_contract": _sha256(
            VIC_ROOT / "config" / "tacdiffusion_formal_v4_contract.json"
        ),
        "formal_equipment_contract": _sha256(
            VIC_ROOT / "config" / "tacdiffusion_formal_v4_equipment_contract.json"
        ),
        "sensor_calibration": _sha256(
            ROOT / "config" / "step5d_tacdiffusion_sensor_frame_v4.json"
        ),
        "dynamics_evidence": _sha256(args.dynamics_evidence.resolve()),
    }
    ledger = FormalCampaignLedgerV1(args.campaign_root.resolve(), contract)
    manifest = ledger.initialize(source_bindings=bindings)
    return {
        "ok": True,
        "campaign_manifest": manifest,
        "progress": ledger.progress().as_json(),
        "contact_acquisition": ContactAcquisitionContractV1().as_json(),
        "model_active": False,
        "shadow_only": True,
    }


def run_campaign_status(args: argparse.Namespace) -> dict[str, Any]:
    ledger = FormalCampaignLedgerV1(args.campaign_root.resolve(), _campaign(args.kind))
    progress = ledger.progress()
    result = progress.as_json()
    if not progress.terminal:
        attempt, episode, path = ledger.next_attempt()
        result["next_attempt"] = {
            "attempt_ordinal": attempt,
            "episode": episode.as_json(),
            "path": str(path),
        }
    return result


def run_promote_qualification(args: argparse.Namespace) -> dict[str, Any]:
    identity = resolve_formal_current_state(REPOSITORY_ROOT)
    if identity.get("ok") is not True:
        raise RuntimeError(f"formal_identity_blocked:{identity.get('blockers')}")
    if identity.get("current_stage_id") != "formal_v4_no_contact_qualification":
        raise RuntimeError("formal_qualification_promotion_stage_mismatch")
    evidence_path = args.qualification_summary.resolve()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if not isinstance(evidence, Mapping):
        raise ValueError("formal qualification summary is not an object")
    if (
        evidence.get("schema") != FORMAL_TOOL_SCHEMA_V1
        or evidence.get("claim_class")
        != "live_no_contact_all_seven_trajectories_qualified"
        or evidence.get("ok") is not True
        or evidence.get("families") != list(TRAJECTORY_FAMILIES)
        or evidence.get("model_active") is not False
        or evidence.get("contact_authorized") is not False
        or evidence.get("ur_internal_ft_used") is not False
        or evidence.get("source_content_sha256") != identity.get("source_content_sha256")
    ):
        raise ValueError("formal qualification summary is not promotable")
    current_path = ROOT / "config" / "tacdiffusion_formal_v4_current_stage.json"
    table_path = ROOT / "config" / "tacdiffusion_formal_v4_stage_table.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    table = json.loads(table_path.read_text(encoding="utf-8"))
    for row in table["stages"]:
        row["active"] = row["id"] == "formal_v4_fixed_k_campaign"
        if row["id"] == "formal_v4_no_contact_qualification":
            row["status"] = "accepted"
            row["qualification_summary"] = str(evidence_path)
            row["qualification_summary_sha256"] = _sha256(evidence_path)
        elif row["id"] == "formal_v4_fixed_k_campaign":
            row["status"] = "ready"
    current["current_stage_id"] = "formal_v4_fixed_k_campaign"
    current["qualification_summary"] = str(evidence_path)
    current["qualification_summary_sha256"] = _sha256(evidence_path)
    _replace_json(table_path, table)
    _replace_json(current_path, current)
    resolved = resolve_formal_current_state(REPOSITORY_ROOT)
    if resolved.get("ok") is not True or resolved.get("current_stage_id") != "formal_v4_fixed_k_campaign":
        raise RuntimeError("formal qualification promotion cold-read failed")
    return {"ok": True, "resolver": resolved}


def run_invalidate_qualification(args: argparse.Namespace) -> dict[str, Any]:
    """Return to qualification after an accepted source identity changes."""

    identity = resolve_formal_current_state(REPOSITORY_ROOT)
    if identity.get("ok") is not True:
        raise RuntimeError(f"formal_identity_blocked:{identity.get('blockers')}")
    if identity.get("current_stage_id") != "formal_v4_fixed_k_campaign":
        raise RuntimeError("formal_qualification_invalidation_stage_mismatch")
    if args.reason != "source_changed_after_contact_acceptance_repair":
        raise ValueError("formal_qualification_invalidation_reason_not_accepted")
    current_path = ROOT / "config" / "tacdiffusion_formal_v4_current_stage.json"
    table_path = ROOT / "config" / "tacdiffusion_formal_v4_stage_table.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    table = json.loads(table_path.read_text(encoding="utf-8"))
    prior_summary = current.get("qualification_summary")
    prior_summary_sha256 = current.get("qualification_summary_sha256")
    for row in table["stages"]:
        row["active"] = row["id"] == "formal_v4_no_contact_qualification"
        if row["id"] == "formal_v4_no_contact_qualification":
            row["status"] = "invalidated_by_source_change"
            row["invalidated_qualification_summary"] = prior_summary
            row["invalidated_qualification_summary_sha256"] = prior_summary_sha256
            row["invalidation_reason"] = args.reason
            row.pop("qualification_summary", None)
            row.pop("qualification_summary_sha256", None)
        elif row["id"] == "formal_v4_fixed_k_campaign":
            row["status"] = "blocked_on_requalification"
    current["current_stage_id"] = "formal_v4_no_contact_qualification"
    current["invalidated_qualification_summary"] = prior_summary
    current["invalidated_qualification_summary_sha256"] = prior_summary_sha256
    current["qualification_invalidation_reason"] = args.reason
    current.pop("qualification_summary", None)
    current.pop("qualification_summary_sha256", None)
    _replace_json(table_path, table)
    _replace_json(current_path, current)
    resolved = resolve_formal_current_state(REPOSITORY_ROOT)
    if (
        resolved.get("ok") is not True
        or resolved.get("current_stage_id")
        != "formal_v4_no_contact_qualification"
    ):
        raise RuntimeError("formal qualification invalidation cold-read failed")
    return {"ok": True, "resolver": resolved}


def _smooth01(value: float) -> float:
    p = max(0.0, min(1.0, float(value)))
    return 10.0 * p**3 - 15.0 * p**4 + 6.0 * p**5


def _annotate_contact_row(
    row: dict[str, Any],
    metadata: Mapping[str, Any] | None,
) -> None:
    if metadata is None:
        row["formal_phase"] = "UNBOUND"
        return
    row["formal_phase"] = str(metadata["phase"])
    row["formal_reference_sample_id"] = str(metadata["reference_sample_id"])
    for prefix, values in (
        ("formal_desired_pose", metadata["desired_pose_base"]),
        ("formal_desired_twist", metadata["desired_twist_base"]),
        ("formal_desired_acceleration", metadata["desired_acceleration_base"]),
    ):
        for axis, value in enumerate(values):
            row[f"{prefix}_{axis}"] = float(value)


def _run_contact_attempt_locked(
    *,
    args: argparse.Namespace,
    ledger: FormalCampaignLedgerV1,
    identity: Mapping[str, Any],
    dynamics_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    attempt_ordinal, episode, attempt_dir = ledger.next_attempt(
        verify_artifacts=False
    )
    attempt_dir.mkdir(parents=True, exist_ok=False)
    attempt_id = attempt_dir.name
    contract = ContactAcquisitionContractV1()
    status = legacy.readonly_status(args.robot_host)
    _validate_common_preflight(status)
    entry_pose = tuple(float(value) for value in status["rtde"]["actual_TCP_pose"])
    tube = _contact_episode_tube(entry_pose, contract)
    receiver_tube = _contact_receiver_tube(entry_pose, contract)
    receiver_source = build_live_receiver_source(
        receiver_tube,
        friction_profile=legacy.FRICTION_PROFILE_UR_DEFAULT_V2_FORMAL_CONTACT,
        guard_force_limit_n=20.0,
        guard_torque_limit_nm=2.0,
    )
    receiver_contract = parse_live_receiver_source(receiver_source)
    if (
        receiver_contract.guard_force_limit_n != 20.0
        or receiver_contract.guard_torque_limit_nm != 2.0
    ):
        raise RuntimeError("formal_contact_receiver_guard_mismatch")
    receiver_sha = hashlib.sha256(receiver_source.encode("utf-8")).hexdigest()
    calibration, calibration_sha = legacy.validate_calibration(
        args.kunwei_calibration.resolve()
    )
    source_hashes = {
        "source_content": str(identity["source_content_sha256"]),
        "receiver_source": receiver_sha,
        "dynamics_conformance": str(
            dynamics_evidence["conformance_receipt"]["receipt_sha256"]
        ),
        "sensor_calibration": calibration_sha,
        "campaign_manifest": _sha256(ledger.root / "campaign_manifest.json"),
    }
    formal_manifest = _formal_manifest_for_attempt(
        attempt_id=attempt_id, source_hashes=source_hashes
    )
    lease_id, episode_identity = legacy._new_live_identity_pair()
    initial_guard = (0.0,) * 6
    idle = legacy._command_packet(
        command=legacy.MODE_IDLE,
        sequence=0,
        progress_s=0.0,
        pose=entry_pose,
        lease_id=lease_id,
        episode_identity=episode_identity,
        kunwei_guard_wrench_tcp_si=initial_guard,
        stiffness_6d=FixedKExpertV1().stiffness_6d,
        raw_f_ff_6d=(0.0,) * 6,
    )
    outgoing = idle
    lineages: dict[int, legacy.CommandLineage] = {0: idle.lineage}
    command_metadata: dict[int, dict[str, Any]] = {
        0: {
            "phase": FormalAttemptPhase.CONTACT_SEARCH.value,
            "reference_sample_id": f"{attempt_id}:search:0",
            "desired_pose_base": entry_pose,
            "desired_twist_base": (0.0,) * 6,
            "desired_acceleration_base": (0.0,) * 6,
        }
    }
    scheduler = legacy.AckPacedScheduler()
    rows: list[dict[str, Any]] = []
    kunwei_summary: dict[str, Any] = {}
    equipment_receipt: dict[str, Any] | None = None
    phase = FormalAttemptPhase.CONTACT_SEARCH
    phase_started_s: float | None = None
    torque_started_s: float | None = None
    contact_pose: tuple[float, ...] | None = None
    contact_latch_sample_index: int | None = None
    timeline = None
    last_actual_pose = entry_pose
    last_actual_speed = (0.0,) * 6
    previous_k = FixedKExpertV1().stiffness_6d
    track_completed = False
    contact_not_found = False
    home_stable_rows = 0
    home_return_start_pose = entry_pose
    home_return_duration_s = 2.0
    end_sent = False
    observed_complete = False
    failure: str | None = None
    fault_class: str | None = None
    auto_return_performed = False
    start_s: float | None = None
    primary_barrier: Mapping[str, Any] | None = None
    deadline_s: float | None = None
    try:
        with (
            legacy.KunweiGuardCapture(
                sensor_ip=args.sensor_ip,
                sensor_port=args.sensor_port,
                connect_timeout_s=args.connect_timeout_s,
                output_dir=attempt_dir,
                calibration=calibration,
                delivery_watchdog_s=args.sensor_delivery_watchdog_s,
                active_force_limit_n=20.0,
                active_torque_limit_nm=2.0,
                contact_latch_load_n=contract.contact_latch_load_n,
                contact_latch_samples=contract.latch_samples,
            ) as kunwei,
            legacy.LiveRTDE(args.robot_host, timeout=args.connect_timeout_s) as rtde,
        ):
            preflight_guard = kunwei.wait_preflight()
            equipment_receipt = _fresh_equipment_readback(
                robot_host=args.robot_host,
                status=status,
                kunwei_ready=True,
                output_path=attempt_dir / "equipment_readback.json",
            )
            source_hashes["equipment_readback"] = _sha256(
                attempt_dir / "equipment_readback.json"
            )
            formal_manifest = _formal_manifest_for_attempt(
                attempt_id=attempt_id, source_hashes=source_hashes
            )
            rtde.negotiate()
            output_recipe, output_types = rtde.setup_outputs(500.0, legacy.OUTPUT_FIELDS)
            input_recipe, input_types = rtde.setup_inputs(legacy.INPUT_FIELDS)
            rtde.start()
            idle = legacy._command_packet(
                command=legacy.MODE_IDLE,
                sequence=0,
                progress_s=0.0,
                pose=entry_pose,
                lease_id=lease_id,
                episode_identity=episode_identity,
                kunwei_guard_wrench_tcp_si=preflight_guard.wrench_tcp_si,
                kunwei_sample_index=preflight_guard.sample_index,
                kunwei_receive_batch_id=preflight_guard.receive_batch_id,
                kunwei_nominal_sensor_time_s=preflight_guard.nominal_sensor_time_s,
                kunwei_batch_arrival_monotonic_s=preflight_guard.t_monotonic_s,
                stiffness_6d=FixedKExpertV1().stiffness_6d,
                raw_f_ff_6d=(0.0,) * 6,
            )
            outgoing = idle
            lineages[0] = idle.lineage
            legacy._prime_idle_inputs(
                rtde,
                input_recipe,
                input_types,
                outgoing.values,
                output_recipe,
                output_types,
                legacy.OUTPUT_FIELDS,
            )
            primary_barrier = legacy._send_urscript_with_primary_start_barrier(
                args.robot_host,
                receiver_source,
                timeout_s=args.connect_timeout_s,
            )
            diagnostics: list[dict[str, Any]] = []
            start_s, first = legacy._wait_for_fresh_receiver_waiting(
                rtde,
                output_recipe,
                output_types,
                legacy.OUTPUT_FIELDS,
                receiver_wait_s=args.receiver_wait_s,
                lease_id=lease_id,
                episode_identity=episode_identity,
                samples_out=diagnostics,
            )
            scheduler.arm(start_s)
            deadline_s = start_s + 90.0
            pending: list[dict[str, Any]] = [first]
            while not observed_complete:
                now = time.monotonic()
                if deadline_s is not None and now >= deadline_s:
                    raise RuntimeError("formal_contact_attempt_deadline_exceeded")
                guard = kunwei.snapshot(max_age_s=args.sensor_delivery_watchdog_s)
                if not pending:
                    pending = legacy._receive_available(
                        rtde,
                        output_recipe,
                        output_types,
                        legacy.OUTPUT_FIELDS,
                        0.005,
                    )
                    if not pending:
                        continue
                last_sample = pending[-1]
                for sample in pending:
                    state = int(sample["output_int_register_24"])
                    fault = int(sample["output_int_register_26"])
                    if (
                        int(sample["robot_mode"]) != legacy.ROBOT_MODE_RUNNING
                        or int(sample["safety_mode"]) != legacy.SAFETY_MODE_NORMAL
                    ):
                        raise RuntimeError("formal_contact_runtime_safety_changed")
                    if int(sample["output_int_register_32"]) != legacy.LIVE_PROTOCOL_TOKEN:
                        raise RuntimeError("formal_contact_protocol_identity_changed")
                    if state == legacy.STATE_FAULT or fault != 0:
                        raise RuntimeError(f"formal_contact_receiver_fault:{fault}")
                    if state in (legacy.STATE_STARTUP, legacy.STATE_TORQUE):
                        if int(sample["output_int_register_27"]) != lease_id:
                            raise RuntimeError("formal_contact_lease_echo_mismatch")
                        if int(sample["output_int_register_31"]) != episode_identity:
                            raise RuntimeError("formal_contact_episode_echo_mismatch")
                        if torque_started_s is None:
                            torque_started_s = time.monotonic()
                            phase_started_s = torque_started_s
                    last_actual_pose = tuple(float(value) for value in sample["actual_TCP_pose"])
                    last_actual_speed = tuple(float(value) for value in sample["actual_TCP_speed"])
                    tube.assert_contains_pose(last_actual_pose, role="actual")
                    ack = int(sample["output_int_register_25"])
                    acked = lineages.get(ack)
                    row = legacy._output_row(
                        sample,
                        time.monotonic() - start_s,
                        outgoing=outgoing.lineage,
                        acked=acked,
                    )
                    _annotate_contact_row(row, command_metadata.get(ack))
                    rows.append(row)
                    if state == legacy.STATE_COMPLETE:
                        observed_complete = True
                if observed_complete:
                    break

                now = time.monotonic()
                if torque_started_s is not None and phase_started_s is not None:
                    elapsed = now - phase_started_s
                    if phase == FormalAttemptPhase.CONTACT_SEARCH:
                        if guard.contact_latched:
                            contact_pose = last_actual_pose
                            contact_latch_sample_index = guard.sample_index
                            phase = FormalAttemptPhase.CONTACT_SETTLE
                            phase_started_s = now
                        elif contract.search_displacement_m(elapsed) >= contract.maximum_search_distance_m:
                            contact_not_found = True
                            phase = FormalAttemptPhase.HOME_RETURN
                            phase_started_s = now
                            home_return_start_pose = last_actual_pose
                            home_return_duration_s = max(
                                2.0,
                                math.dist(last_actual_pose[:3], entry_pose[:3]) / 0.005,
                            )
                    elif phase == FormalAttemptPhase.CONTACT_SETTLE and elapsed >= contract.settle_duration_s:
                        assert contact_pose is not None
                        timeline = build_formal_trajectory_timeline(
                            family=episode.trajectory_family,
                            seed=args.seed + episode.episode_index,
                            anchor_pose_base=contact_pose,
                            u_axis_base=U_AXIS_BASE,
                            v_axis_base=V_AXIS_BASE,
                        )
                        phase = FormalAttemptPhase.TRACK
                        phase_started_s = now
                    elif phase == FormalAttemptPhase.TRACK and elapsed >= contract.track_duration_s:
                        track_completed = True
                        phase = FormalAttemptPhase.RETRACT
                        phase_started_s = now
                    elif phase == FormalAttemptPhase.RETRACT and elapsed >= 2.0:
                        phase = FormalAttemptPhase.HOME_RETURN
                        phase_started_s = now
                        home_return_start_pose = last_actual_pose
                        home_return_duration_s = max(
                            2.0,
                            math.dist(last_actual_pose[:3], entry_pose[:3]) / 0.005,
                        )
                    elif phase == FormalAttemptPhase.HOME_RETURN:
                        translation_error = math.dist(last_actual_pose[:3], entry_pose[:3])
                        speed_ok = max(abs(value) for value in last_actual_speed[:3]) <= 0.001
                        if elapsed >= home_return_duration_s and translation_error <= 0.0005 and speed_ok:
                            home_stable_rows += len(pending)
                        else:
                            home_stable_rows = 0
                        if home_stable_rows >= 25 and not end_sent:
                            end_command = legacy._command_packet(
                                command=legacy.MODE_END,
                                sequence=outgoing.lineage.command_sequence,
                                progress_s=0.0,
                                pose=entry_pose,
                                lease_id=lease_id,
                                episode_identity=episode_identity,
                                kunwei_guard_wrench_tcp_si=guard.wrench_tcp_si,
                                kunwei_sample_index=guard.sample_index,
                                kunwei_receive_batch_id=guard.receive_batch_id,
                                kunwei_nominal_sensor_time_s=guard.nominal_sensor_time_s,
                                kunwei_batch_arrival_monotonic_s=guard.t_monotonic_s,
                                stiffness_6d=previous_k,
                                raw_f_ff_6d=(0.0,) * 6,
                            )
                            rtde.send_inputs(input_recipe, input_types, end_command.values)
                            outgoing = end_command
                            end_sent = True
                            auto_return_performed = True

                if end_sent:
                    pending = []
                    continue
                if scheduler.release_due(now):
                    next_sequence = scheduler.next_sequence(
                        int(last_sample["output_int_register_25"])
                    )
                    if next_sequence is not None:
                        desired_pose = entry_pose
                        desired_twist = (0.0,) * 6
                        desired_acceleration = (0.0,) * 6
                        reference_sample_id = f"{attempt_id}:{phase.value}:{next_sequence}"
                        feedforward = (0.0,) * 6
                        stiffness = previous_k
                        progress_s = 0.0
                        if torque_started_s is not None and phase_started_s is not None:
                            elapsed = max(0.0, now - phase_started_s)
                            if phase == FormalAttemptPhase.CONTACT_SEARCH:
                                desired_pose = contract.search_pose(entry_pose, elapsed)
                                desired_twist = contract.approach_normal_base + (0.0,) * 3
                                desired_twist = tuple(
                                    contract.approach_speed_m_s * value
                                    for value in desired_twist[:3]
                                ) + (0.0,) * 3
                                progress_s = contract.search_displacement_m(elapsed)
                            elif phase == FormalAttemptPhase.CONTACT_SETTLE:
                                assert contact_pose is not None
                                desired_pose = contact_pose
                                ramp = _smooth01(elapsed / contract.settle_duration_s)
                                full = _approach_feedforward_tcp(
                                    contact_pose, episode.target_load_n
                                )
                                feedforward = tuple(ramp * value for value in full)
                            elif phase == FormalAttemptPhase.TRACK:
                                assert timeline is not None
                                reference = timeline.row_at(elapsed)
                                desired_pose = tuple(reference["desired_pose_base"])
                                desired_twist = tuple(reference["desired_twist_base"])
                                desired_acceleration = tuple(reference["desired_acceleration_base"])
                                reference_sample_id = str(reference["reference_sample_id"])
                                progress_s = float(reference["progress_s"])
                                feedforward = _approach_feedforward_tcp(
                                    desired_pose, episode.target_load_n
                                )
                                if ledger.contract.kind == "variable_k":
                                    tracking_error = tuple(
                                        desired_pose[index] - last_actual_pose[index]
                                        for index in range(3)
                                    )
                                    stiffness = VariableKExpertV1().stiffness(
                                        tracking_error,
                                        guard.wrench_tcp_si[:3],
                                        previous_stiffness=previous_k[:3],
                                        dt_s=max(0.002, scheduler.period_s),
                                    )
                            elif phase == FormalAttemptPhase.RETRACT:
                                assert contact_pose is not None
                                p = min(1.0, elapsed / 2.0)
                                desired_pose = contract.retract_pose(contact_pose, p)
                                feedforward_full = _approach_feedforward_tcp(
                                    contact_pose, episode.target_load_n
                                )
                                feedforward = tuple((1.0 - p) * value for value in feedforward_full)
                            elif phase == FormalAttemptPhase.HOME_RETURN:
                                p = _smooth01(elapsed / home_return_duration_s)
                                desired_pose = tuple(
                                    home_return_start_pose[index]
                                    + p * (entry_pose[index] - home_return_start_pose[index])
                                    for index in range(3)
                                ) + entry_pose[3:]
                        tube.assert_contains_pose(desired_pose, role="desired")
                        packet = legacy._command_packet(
                            command=legacy.MODE_RUN,
                            sequence=next_sequence,
                            progress_s=progress_s,
                            pose=desired_pose,
                            lease_id=lease_id,
                            episode_identity=episode_identity,
                            kunwei_guard_wrench_tcp_si=guard.wrench_tcp_si,
                            kunwei_sample_index=guard.sample_index,
                            kunwei_receive_batch_id=guard.receive_batch_id,
                            kunwei_nominal_sensor_time_s=guard.nominal_sensor_time_s,
                            kunwei_batch_arrival_monotonic_s=guard.t_monotonic_s,
                            stiffness_6d=stiffness,
                            raw_f_ff_6d=feedforward,
                        )
                        rtde.send_inputs(input_recipe, input_types, packet.values)
                        legacy._register_command_lineage(lineages, packet)
                        command_metadata[next_sequence] = {
                            "phase": phase.value,
                            "reference_sample_id": reference_sample_id,
                            "desired_pose_base": desired_pose,
                            "desired_twist_base": desired_twist,
                            "desired_acceleration_base": desired_acceleration,
                        }
                        outgoing = packet
                        previous_k = stiffness
                pending = []
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        fault_class, _ = classify_fault(exc)
        # Fault exits Direct Torque only. No automatic retract/home is sent
        # from this handler, including sensor/guard/protective/safety faults.
        try:
            if "rtde" in locals() and outgoing is not None:
                abort = legacy._command_packet(
                    command=legacy.MODE_ABORT,
                    sequence=outgoing.lineage.command_sequence,
                    progress_s=outgoing.lineage.progress_s,
                    pose=outgoing.lineage.desired_pose,
                    lease_id=lease_id,
                    episode_identity=episode_identity,
                    kunwei_guard_wrench_tcp_si=outgoing.lineage.kunwei_guard_wrench_tcp_si,
                )
                rtde.send_inputs(input_recipe, input_types, abort.values)
        except Exception:
            pass
    finally:
        if "kunwei" in locals():
            try:
                kunwei.stop()
            except Exception as stop_exc:
                if failure is None:
                    failure = f"{type(stop_exc).__name__}: {stop_exc}"
                    fault_class, _ = classify_fault(stop_exc)
            kunwei_summary = kunwei.summary()

    csv_path = attempt_dir / "direct_torque_rtde.csv"
    if rows:
        _write_csv_new(csv_path, rows)
    post_status: Mapping[str, Any] | None = None
    try:
        post_status = legacy.readonly_status(args.robot_host)
    except Exception as exc:
        if failure is None:
            failure = f"post_status:{type(exc).__name__}:{exc}"
            fault_class, _ = classify_fault(exc)
    task_ready_home = bool(
        auto_return_performed
        and observed_complete
        and post_status is not None
        and post_status.get("stationary") is True
        and post_status.get("stopped") is True
        and math.dist(
            tuple(float(value) for value in post_status["rtde"]["actual_TCP_pose"][:3]),
            entry_pose[:3],
        )
        <= 0.0005
    )
    if auto_return_performed and not task_ready_home and failure is None:
        failure = "TaskReadyHome_not_verified_after_return"
        fault_class = "recoverable_runtime"

    formal_result: dict[str, Any] | None = None
    if failure is None and track_completed and task_ready_home:
        try:
            formal_result = _compose_formal_artifact(
                attempt_dir=attempt_dir,
                rows=rows,
                formal_manifest=formal_manifest,
                dynamics_evidence=dynamics_evidence,
                tube=tube,
                semantic_fingerprint_sha256=str(identity["source_content_sha256"]),
            )
        except Exception as exc:
            failure = f"formal_finalize:{type(exc).__name__}:{exc}"
            fault_class = "recoverable_runtime"

    eligible = bool(
        formal_result is not None
        and formal_result.get("formal_eligible") is True
        and failure is None
        and task_ready_home
    )
    if eligible:
        outcome = FormalAttemptOutcome.ELIGIBLE
        terminal_phase = FormalAttemptPhase.COMPLETE
        fault_class = None
    elif failure is not None and fault_class in contract.no_auto_return_fault_classes:
        outcome = FormalAttemptOutcome.HARD_FAULT
        terminal_phase = FormalAttemptPhase.FAULT
    elif contact_not_found or failure is not None:
        outcome = FormalAttemptOutcome.RECOVERABLE_FAILURE
        terminal_phase = FormalAttemptPhase.FAULT
        fault_class = fault_class or "contact_not_found"
    else:
        outcome = FormalAttemptOutcome.INELIGIBLE
        terminal_phase = FormalAttemptPhase.COMPLETE
    formal_result_for_evidence = (
        None
        if formal_result is None
        else {
            key: (str(value) if isinstance(value, Path) else value)
            for key, value in formal_result.items()
        }
    )
    evidence: dict[str, Any] = {
        "schema_version": "ur10e_tacdiffusion_formal_contact_attempt/v1",
        "ok": eligible,
        "campaign_id": ledger.contract.campaign_id,
        "attempt_id": attempt_id,
        "attempt_ordinal": attempt_ordinal,
        "eligible_ordinal": episode.episode_index,
        "episode": episode.as_json(),
        "outcome": outcome.value,
        "terminal_phase": terminal_phase.value,
        "failure": failure,
        "fault_class": fault_class,
        "contact_latched": contact_latch_sample_index is not None,
        "contact_latch_sample_index": contact_latch_sample_index,
        "contact_not_found": contact_not_found,
        "track_completed": track_completed,
        "observed_complete": observed_complete,
        "auto_return_performed": auto_return_performed,
        "task_ready_home": task_ready_home,
        "entry_pose_base": list(entry_pose),
        "post_status": post_status,
        "source_content_sha256": identity["source_content_sha256"],
        "receiver_source_sha256": receiver_sha,
        "receiver_friction_profile": (
            legacy.FRICTION_PROFILE_UR_DEFAULT_V2_FORMAL_CONTACT
        ),
        "receiver_tube": {
            "center_semantics": "controller_rebased_to_actual_entry",
            "normal_half_width_m": receiver_tube.normal_half_width_m,
        },
        "formal_manifest": formal_manifest.as_json(),
        "contact_acquisition": contract.as_json(),
        "equipment_readback": equipment_receipt,
        "equipment_readback_sha256": (
            None
            if equipment_receipt is None
            else _sha256(attempt_dir / "equipment_readback.json")
        ),
        "kunwei": kunwei_summary,
        "kunwei_only": True,
        "ur_internal_ft_used": False,
        "guard_force_limit_n": 20.0,
        "guard_torque_limit_nm": 2.0,
        "model_active": False,
        "shadow_only": True,
        "formal_result": formal_result_for_evidence,
        "rtde_row_count": len(rows),
        "raw_rtde_csv": str(csv_path) if rows else None,
        "primary_client_start_barrier": primary_barrier,
        "scheduler": scheduler.summary(),
        "duration_s": None if start_s is None else time.monotonic() - start_s,
    }
    evidence_path = attempt_dir / "evidence.json"
    _write_json_new(evidence_path, evidence)
    relative = lambda path: str(Path(path).resolve().relative_to(ledger.root.resolve()))
    receipt = FormalAttemptReceiptV1(
        campaign_id=ledger.contract.campaign_id,
        attempt_ordinal=attempt_ordinal,
        eligible_ordinal=episode.episode_index,
        episode=episode,
        attempt_id=attempt_id,
        outcome=outcome.value,
        phase=terminal_phase.value,
        fault_class=fault_class,
        auto_return_performed=auto_return_performed,
        evidence_path=relative(evidence_path),
        evidence_sha256=_sha256(evidence_path),
        artifact_path=(
            None if formal_result is None else relative(formal_result["artifact_path"])
        ),
        artifact_sha256=(
            None if formal_result is None else _sha256(formal_result["artifact_path"])
        ),
        recorder_manifest_path=(
            None
            if formal_result is None
            else relative(formal_result["recorder_manifest_path"])
        ),
        recorder_manifest_sha256=(
            None
            if formal_result is None
            else _sha256(formal_result["recorder_manifest_path"])
        ),
        eligibility_path=(
            None
            if formal_result is None
            else relative(formal_result["eligibility_path"])
        ),
        eligibility_sha256=(
            None
            if formal_result is None
            else _sha256(formal_result["eligibility_path"])
        ),
        previous_record_sha256=ledger.progress(
            verify_artifacts=False
        ).last_record_sha256,
    )
    ledger.append_terminal(receipt)
    if outcome == FormalAttemptOutcome.HARD_FAULT:
        raise RuntimeError(f"formal_contact_hard_fault:{fault_class}:{attempt_id}")
    return evidence


def run_collect_campaign(args: argparse.Namespace) -> dict[str, Any]:
    if not all(
        (
            args.live,
            args.send_urscript,
            args.write_rtde_inputs,
            args.allow_direct_torque,
            args.allow_motion,
            args.allow_contact,
            args.allow_kunwei_stream_command,
        )
    ):
        raise RuntimeError("formal_contact_all_independent_live_gates_required")
    identity = resolve_formal_current_state(REPOSITORY_ROOT)
    expected_stage = (
        "formal_v4_fixed_k_campaign"
        if args.kind == "fixed_k"
        else "formal_v4_variable_k_campaign"
    )
    if identity.get("ok") is not True or identity.get("current_stage_id") != expected_stage:
        raise RuntimeError(f"formal_contact_identity_or_stage_blocked:{identity}")
    dynamics = _load_dynamics_receipt(args.dynamics_evidence.resolve())
    ledger = FormalCampaignLedgerV1(args.campaign_root.resolve(), _campaign(args.kind))
    if not (ledger.root / "campaign_manifest.json").is_file():
        raise RuntimeError("formal_campaign_not_initialized")
    manifest = json.loads(
        (ledger.root / "campaign_manifest.json").read_text(encoding="utf-8")
    )
    bound_source = manifest.get("source_bindings", {}).get("source_content")
    if bound_source != identity.get("source_content_sha256"):
        raise RuntimeError("formal_campaign_source_content_mismatch")
    attempts_started = 0
    results: list[dict[str, Any]] = []
    with legacy._live_writer_lease():
        legacy._enforce_no_live_writer_conflict()
        # One cold full-artifact audit on resume; hot-loop progress verifies
        # the hash chain while avoiding an O(N^2) rehash of prior episode data.
        initial_progress = ledger.progress(verify_artifacts=True)
        if initial_progress.hard_faults:
            raise RuntimeError("formal_campaign_is_latched_on_hard_fault")
        while not ledger.progress(verify_artifacts=False).complete:
            if args.max_attempts > 0 and attempts_started >= args.max_attempts:
                break
            results.append(
                _run_contact_attempt_locked(
                    args=args,
                    ledger=ledger,
                    identity=identity,
                    dynamics_evidence=dynamics,
                )
            )
            attempts_started += 1
    return {
        "ok": ledger.progress(verify_artifacts=False).complete,
        "campaign_id": ledger.contract.campaign_id,
        "attempts_this_invocation": attempts_started,
        "progress": ledger.progress(verify_artifacts=False).as_json(),
        "results": results,
        "model_active": False,
        "shadow_only": True,
    }


def run_build_dataset(args: argparse.Namespace) -> dict[str, Any]:
    contract = _campaign(args.kind)
    source_contract = VIC_ROOT / "config" / "tacdiffusion_formal_v4_contract.json"
    result = build_formal_campaign_dataset(
        campaign_root=args.campaign_root.resolve(),
        contract=contract,
        output_dir=args.output_dir.resolve(),
        surface_calibration_sha256=_sha256(
            ROOT / "config" / "step5d_tacdiffusion_sensor_frame_v4.json"
        ),
        action_profile_sha256=_sha256(source_contract),
        filter_profile_sha256=_sha256(source_contract),
        normalization_sha256=_sha256(
            VIC_ROOT / "ur10e_vic" / "tacdiffusion" / "formal_model.py"
        ),
    )
    return {"ok": True, **result.as_json()}


def run_train(args: argparse.Namespace) -> dict[str, Any]:
    result = train_and_seal_formal_checkpoint(
        training_dataset_path=args.training_dataset.resolve(),
        training_manifest_path=args.training_manifest.resolve(),
        output_dir=args.output_dir.resolve(),
        epochs=args.epochs,
        seed=args.seed,
        device=args.device,
    )
    return {"ok": True, **result}


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    predictor = load_sealed_formal_predictor(args.training_receipt.resolve())
    with np.load(args.training_dataset.resolve(), allow_pickle=False) as archive:
        observations = np.asarray(archive["observations"], dtype=np.float64)
        splits = np.asarray(archive["splits"])
    candidates = np.flatnonzero(splits == "validation")
    if not len(candidates):
        raise ValueError("formal benchmark requires a validation observation")
    candidate = benchmark_formal_50_step_sampler(
        predictor,
        observations[int(candidates[0])],
        duration_per_rate_s=args.duration_per_rate_s,
        checkpoint_sha256=predictor.checkpoint_sha256,
        dataset_sha256=predictor.dataset_sha256,
    )
    selection = validate_formal_50_step_sampler_candidate(
        candidate,
        expected_checkpoint_sha256=predictor.checkpoint_sha256,
        expected_dataset_sha256=predictor.dataset_sha256,
    )
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    _write_json_new(output / "timing_candidate.json", candidate)
    _write_json_new(output / "timing_selection.json", selection)
    return {
        "ok": bool(selection.get("selected_rate_hz") is not None),
        "candidate": str(output / "timing_candidate.json"),
        "selection": str(output / "timing_selection.json"),
        "selection_result": selection,
        "model_active": False,
        "shadow_only": True,
    }


def _write_csv_new(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        raise RuntimeError("formal_qualification_has_no_rows")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _load_dynamics_receipt(path: Path) -> Mapping[str, Any]:
    evidence = json.loads(path.read_text(encoding="utf-8"))
    if evidence.get("ok") is not True or evidence.get("motion_performed") is not False:
        raise ValueError("formal dynamics evidence is not accepted no-motion evidence")
    if evidence.get("ur_force_fields_read") is not False:
        raise ValueError("formal dynamics evidence used a forbidden force field")
    receipt_payload = evidence.get("conformance_receipt")
    if not isinstance(receipt_payload, Mapping):
        raise ValueError("formal dynamics conformance receipt is missing")
    load_formal_dynamics_conformance_receipt(receipt_payload)
    return evidence


def _validate_common_preflight(status: Mapping[str, Any]) -> None:
    legacy.validate_compile_probe_preflight(status)
    loaded = str(status["dashboard"].get("get loaded program", ""))
    if "autotune" not in loaded.lower() and "tacdiffusion" not in loaded.lower():
        # Loaded TP is not executed by this Remote Secondary Client route, but
        # retaining its identity prevents an unrelated bench program from
        # being mistaken for the current experiment context.
        raise RuntimeError("formal_preflight_loaded_program_identity_unexpected")


def _episode_tube(anchor_pose: Sequence[float]) -> LiveTubeContract:
    anchor = tuple(float(value) for value in anchor_pose)
    return LiveTubeContract(
        center_base_m=anchor[:3],
        anchor_pose_base=anchor,
        u_axis_base=U_AXIS_BASE,
        v_axis_base=V_AXIS_BASE,
        safe_u_half_width_m=0.012,
        safe_v_half_width_m=0.012,
        normal_half_width_m=0.002,
        orientation_tolerance_rad=math.radians(5.0),
    )


def _contact_episode_tube(
    entry_pose: Sequence[float], contract: ContactAcquisitionContractV1
) -> LiveTubeContract:
    entry = tuple(float(value) for value in entry_pose)
    midpoint = contract.maximum_search_distance_m / 2.0
    center = tuple(
        entry[index] + midpoint * contract.approach_normal_base[index]
        for index in range(3)
    )
    # 0.5 mm remains as numeric/braking tolerance around the exact 25 mm
    # search corridor.  Tangential references stay within the frozen 3 mm
    # envelope while the tube remains independently checked on every tick.
    return LiveTubeContract(
        center_base_m=center,
        anchor_pose_base=entry,
        u_axis_base=U_AXIS_BASE,
        v_axis_base=V_AXIS_BASE,
        safe_u_half_width_m=0.012,
        safe_v_half_width_m=0.012,
        normal_half_width_m=midpoint + 0.0005,
        orientation_tolerance_rad=math.radians(5.0),
    )


def _contact_receiver_tube(
    entry_pose: Sequence[float], contract: ContactAcquisitionContractV1
) -> LiveTubeContract:
    """Controller Tube for a receiver that rebases its center at entry."""

    entry = tuple(float(value) for value in entry_pose)
    return LiveTubeContract(
        center_base_m=entry[:3],
        anchor_pose_base=entry,
        u_axis_base=U_AXIS_BASE,
        v_axis_base=V_AXIS_BASE,
        safe_u_half_width_m=0.012,
        safe_v_half_width_m=0.012,
        normal_half_width_m=contract.maximum_search_distance_m + 0.0005,
        orientation_tolerance_rad=math.radians(5.0),
    )


def _rotation_matrix_from_rotvec(rotvec: Sequence[float]) -> np.ndarray:
    vector = np.asarray(tuple(float(value) for value in rotvec), dtype=float)
    if vector.shape != (3,) or not np.isfinite(vector).all():
        raise ValueError("rotation vector must contain three finite values")
    angle = float(np.linalg.norm(vector))
    if angle < 1.0e-12:
        return np.eye(3)
    axis = vector / angle
    skew = np.asarray(
        [
            [0.0, -axis[2], axis[1]],
            [axis[2], 0.0, -axis[0]],
            [-axis[1], axis[0], 0.0],
        ],
        dtype=float,
    )
    return np.eye(3) + math.sin(angle) * skew + (1.0 - math.cos(angle)) * (skew @ skew)


def _approach_feedforward_tcp(
    pose_base: Sequence[float], target_load_n: float
) -> tuple[float, ...]:
    pose = tuple(float(value) for value in pose_base)
    if len(pose) != 6 or not all(math.isfinite(value) for value in pose):
        raise ValueError("feedforward pose must contain six finite values")
    target = float(target_load_n)
    if target not in (3.0, 5.0, 8.0):
        raise ValueError("formal target load must be exactly 3, 5, or 8 N")
    rotation_base_from_tcp = _rotation_matrix_from_rotvec(pose[3:])
    approach_base = np.asarray((0.0, 0.0, -1.0), dtype=float)
    force_tcp = rotation_base_from_tcp.T @ (target * approach_base)
    return tuple(float(value) for value in force_tcp) + (0.0, 0.0, 0.0)


def _formal_manifest_for_attempt(
    *, attempt_id: str, source_hashes: Mapping[str, str]
) -> FormalEpisodeManifestV1:
    authority = KunweiOnlyForceAuthorityV1()
    return FormalEpisodeManifestV1(
        manifest_id=attempt_id,
        force_authority=authority,
        contact_guard_profile=ContactGuardProfileV1.expert_contact(authority=authority),
        rtde_output_fields=tuple(legacy.OUTPUT_FIELDS),
        source_hashes=source_hashes,
    )


def _production_dynamics_runtime(
    dynamics_evidence: Mapping[str, Any],
) -> ProductionDynamicsRuntimeV1:
    import pinocchio as pin
    import step5c_calibrated_kinematics_audit as step5d_kin
    from kunwei_rtde_bridge import step5d_tcp_jacobian_base

    conformance = load_formal_dynamics_conformance_receipt(
        dynamics_evidence["conformance_receipt"]
    )
    model_bundle = step5d_kin.build_calibrated_model()
    model_sha = hashlib.sha256(model_bundle.urdf_text.encode("utf-8")).hexdigest()
    if model_sha != conformance.calibrated_model_sha256:
        raise RuntimeError("formal_calibrated_model_changed_since_conformance")

    def jacobian(q: np.ndarray) -> np.ndarray:
        return np.asarray(
            step5d_tcp_jacobian_base(
                model_bundle,
                q,
                np.asarray((0.0, 0.0, 0.0874), dtype=float),
            ),
            dtype=float,
        )

    def coriolis(q: np.ndarray, qd: np.ndarray) -> np.ndarray:
        matrix = pin.computeCoriolisMatrix(
            model_bundle.model, model_bundle.data, q, qd
        )
        return np.asarray(matrix @ qd, dtype=float)

    return ProductionDynamicsRuntimeV1(
        conformance,
        jacobian_provider=jacobian,
        coriolis_provider=coriolis,
        source_hashes={
            "controller_conformance": conformance.receipt_sha256,
            "calibrated_model": conformance.calibrated_model_sha256,
        },
    )


def _lineage_from_output_row(row: Mapping[str, Any]) -> legacy.CommandLineage | None:
    if int(row.get("ack_command_lineage_missing", 1)) != 0:
        return None
    return legacy.CommandLineage(
        command_mode=int(row["acked_command_mode"]),
        command_sequence=int(row["acked_command_sequence"]),
        progress_s=float(row["command_progress_s"]),
        desired_pose=tuple(float(row[f"command_desired_pose_{axis}"]) for axis in range(6)),
        commanded_k=tuple(float(row[f"commanded_k_{axis}"]) for axis in range(6)),
        kunwei_guard_wrench_tcp_si=tuple(
            float(row[f"kunwei_guard_wrench_tcp_si_{axis}"]) for axis in range(6)
        ),
        commanded_raw_f_ff=tuple(
            float(row[f"commanded_raw_f_ff_{axis}"]) for axis in range(6)
        ),
        kunwei_sample_index=int(row["kunwei_sample_index"]),
        kunwei_receive_batch_id=int(row["kunwei_receive_batch_id"]),
        kunwei_nominal_sensor_time_s=float(row["kunwei_nominal_sensor_time_s"]),
        kunwei_batch_arrival_monotonic_s=float(
            row["kunwei_batch_arrival_monotonic_s"]
        ),
        lease=int(row["command_lease"]),
        episode=int(row["command_episode"]),
        model_mode=int(row["command_model_mode"]),
        frame_token=int(row["command_frame_token"]),
    )


def _fresh_equipment_readback(
    *,
    robot_host: str,
    status: Mapping[str, Any],
    kunwei_ready: bool,
    output_path: Path,
) -> dict[str, Any]:
    contract = load_formal_equipment_contract(
        VIC_ROOT / "config" / "tacdiffusion_formal_v4_equipment_contract.json"
    )
    if legacy._detect_live_writer_processes():
        raise RuntimeError("formal_equipment_readback_writer_conflict")
    readback = read_rtde_once(
        robot_host,
        ["payload", "payload_cog", "tcp_offset", "actual_TCP_speed"],
        frequency_hz=10.0,
        timeout=3.0,
    )
    dashboard = status["dashboard"]
    receipt: dict[str, Any] = {
        "schema_version": FORMAL_EQUIPMENT_READBACK_SCHEMA_V1,
        "equipment_contract_sha256": contract.fingerprint_sha256,
        "force_authority": contract.authority.as_json(),
        "dashboard": {
            "remote_control": bool(status["remote_control"]),
            "safety_mode": (
                "NORMAL"
                if dashboard.get("safetystatus") == "Safetystatus: NORMAL"
                else dashboard.get("safetystatus")
            ),
            "robot_mode": (
                "RUNNING"
                if dashboard.get("robotmode") == "Robotmode: RUNNING"
                else dashboard.get("robotmode")
            ),
            "program_running": not bool(status["stopped"]),
        },
        "readback": {
            "payload_kg": readback["payload"],
            "payload_cog_m": readback["payload_cog"],
            "tcp_offset_m_rad": readback["tcp_offset"],
            "actual_tcp_speed_m_s_rad_s": readback["actual_TCP_speed"],
        },
        "checks": {
            "single_writer": True,
            "payload_match": True,
            "cog_match": True,
            "tcp_match": True,
            "stationary": bool(status["stationary"]),
            "kunwei_raw_stream": bool(kunwei_ready),
            "no_ur_force_fields_read": True,
        },
        "rtde_fields_read": [
            "payload",
            "payload_cog",
            "tcp_offset",
            "actual_TCP_speed",
        ],
        "ur_force_fields_read": False,
        "motion_performed": False,
    }
    contract.validate_readback(receipt)
    _write_json_new(output_path, receipt)
    return receipt


def _compose_formal_artifact(
    *,
    attempt_dir: Path,
    rows: Sequence[Mapping[str, Any]],
    formal_manifest: FormalEpisodeManifestV1,
    dynamics_evidence: Mapping[str, Any],
    tube: LiveTubeContract,
    semantic_fingerprint_sha256: str,
) -> dict[str, Any]:
    track_rows = [row for row in rows if row.get("formal_phase") == "TRACK"]
    if len(track_rows) < int(0.9 * 8.0 * 500.0):
        raise RuntimeError("formal_track_rows_insufficient")
    if any(
        not bool(row.get("action_echo_coherent"))
        or int(row.get("ack_command_lineage_missing", 1)) != 0
        for row in track_rows
    ):
        raise RuntimeError("formal_track_contains_torn_or_unbound_action_rows")
    for row in track_rows:
        actual = tuple(float(row[f"actual_TCP_pose_{axis}"]) for axis in range(6))
        desired = tuple(float(row[f"formal_desired_pose_{axis}"]) for axis in range(6))
        tube.assert_contains_pose(actual, role="actual")
        tube.assert_contains_pose(desired, role="desired")
    dynamics_runtime = _production_dynamics_runtime(dynamics_evidence)
    composer = FormalFrameComposerV1(
        manifest=formal_manifest,
        semantic_context_fingerprint_sha256=semantic_fingerprint_sha256,
        dynamics_runtime=dynamics_runtime,
    )
    first_host = min(
        float(row["kunwei_batch_arrival_monotonic_s"]) for row in track_rows
    )
    control_clock = legacy.RecorderControlClock(host_anchor_s=first_host)
    observation_history = legacy.RecorderObservationHistory()
    alignment = legacy.CausalKunweiAlignmentAdapter(
        expected_frame_id="tool0_tcp",
        calibration_sha256=_sha256(
            ROOT / "config" / "step5d_tacdiffusion_sensor_frame_v4.json"
        ),
    )
    recorder = FormalEpisodeRecorder(
        attempt_dir,
        episode_id=formal_manifest.manifest_id,
        metadata={
            "formal_manifest": formal_manifest.as_json(),
            "semantic_context_fingerprint_sha256": semantic_fingerprint_sha256,
            "raw_rtde_csv": "direct_torque_rtde.csv",
        },
    )
    composed_count = 0
    recorder.start()
    try:
        previous_row: Mapping[str, Any] | None = None
        for index, row in enumerate(track_rows):
            acked = _lineage_from_output_row(row)
            assert acked is not None
            desired_row = {
                "desired_pose_base": tuple(
                    float(row[f"formal_desired_pose_{axis}"]) for axis in range(6)
                ),
                "desired_twist_base": tuple(
                    float(row[f"formal_desired_twist_{axis}"]) for axis in range(6)
                ),
                "desired_acceleration_base": tuple(
                    float(row[f"formal_desired_acceleration_{axis}"])
                    for axis in range(6)
                ),
                "reference_sample_id": str(row["formal_reference_sample_id"]),
                "reference_derivatives_valid": True,
            }
            base = legacy._recorder_frame(
                row,
                sample_index=index,
                control_sequence=index,
                acked=acked,
                outgoing=acked,
                recorder_start_s=first_host,
                observation_history=observation_history,
                action_provider=None,
                desired_row=desired_row,
                kunwei_alignment_adapter=alignment,
                internal_wrench_provider=None,
                candidate_window=True,
                capture_phase="formal_candidate",
                control_clock=control_clock,
            )
            formal = composer.compose(
                base,
                previous_runtime_row=previous_row,
                tube_payload={
                    "schema": "formal_contact_tube_decision/v1",
                    "accepted": True,
                    "actual_inside": True,
                    "desired_inside": True,
                    "tube_center_base_m": list(tube.center_base_m),
                    "normal_half_width_m": tube.normal_half_width_m,
                    "safe_u_half_width_m": tube.safe_u_half_width_m,
                    "safe_v_half_width_m": tube.safe_v_half_width_m,
                },
            )
            previous_row = row
            if formal is None:
                continue
            if not recorder.enqueue(formal):
                raise RuntimeError("formal_recorder_enqueue_failed")
            composed_count += 1
            recorder.require_healthy()
    finally:
        recorder.close(seal=True)
    validate_formal_episode_artifact(recorder.artifact_path, recorder.manifest_path)
    _, frames = read_episode_artifact(recorder.artifact_path)
    if len(frames) != composed_count:
        raise RuntimeError("formal_recorder_cold_read_count_mismatch")
    health = recorder.health(validate_tamper=True)
    eligibility_path = attempt_dir / "formal_eligibility.json"
    decision, receipt = FormalEligibilityValidator().evaluate_and_write(
        eligibility_path,
        frames,
        recorder_health=health,
        formal_manifest=formal_manifest,
        episode_id=formal_manifest.manifest_id,
        first_live_shadow=False,
    )
    return {
        "artifact_path": recorder.artifact_path,
        "recorder_manifest_path": recorder.manifest_path,
        "eligibility_path": eligibility_path,
        "formal_row_count": len(frames),
        "formal_eligible": decision.formal_eligible,
        "eligibility": receipt,
        "recorder_health": health.as_json(),
        "causal_alignment_fault": alignment.fault,
    }


def _strict_episode_gate(
    *,
    rows: Sequence[Mapping[str, Any]],
    kunwei: Mapping[str, Any],
    complete: bool,
    duration_s: float,
) -> dict[str, bool]:
    active = [
        row
        for row in rows
        if int(row["receiver_state"]) in (legacy.STATE_STARTUP, legacy.STATE_TORQUE)
    ]
    timestamps = [float(row["controller_timestamp_s"]) for row in rows]
    span = max(0.0, timestamps[-1] - timestamps[0]) if timestamps else 0.0
    row_rate = (len(timestamps) - 1) / span if span > 0.0 else 0.0
    coherent = [row for row in active if legacy._action_echo_coherent(row)]
    gate = {
        "observed_complete": bool(complete),
        "active_rows_present": len(active) >= int(0.9 * duration_s * 500.0),
        "output_rate_450_to_550hz": 450.0 <= row_rate <= 550.0,
        # Startup and RTDE sampling can expose transient torn snapshots while
        # the generation counter changes.  Those rows are excluded from
        # episode eligibility; qualification requires usable coherent echoes,
        # matching the accepted Direct Torque live gate.
        "coherent_action_echo_rows_present": len(coherent) >= 2,
        "no_lineage_misses": all(
            int(row.get("ack_command_lineage_missing", 1)) == 0 for row in rows
        ),
        "runtime_safety_normal": all(
            int(row["robot_mode"]) == legacy.ROBOT_MODE_RUNNING
            and int(row["safety_mode"]) == legacy.SAFETY_MODE_NORMAL
            for row in rows
        ),
        "kunwei_parse_errors_zero": int(kunwei["parse_errors"]) == 0,
        "kunwei_dropped_sync_bytes_zero": int(kunwei["dropped_sync_bytes"]) == 0,
        "kunwei_rate_900_to_1100hz": (
            kunwei["rate_hz_by_first_last"] is not None
            and 900.0 <= float(kunwei["rate_hz_by_first_last"]) <= 1100.0
        ),
        "kunwei_force_below_6n": (
            float(kunwei["max_zeroed_force_norm_n"]) <= 6.0
        ),
        "kunwei_torque_below_0p5nm": (
            float(kunwei["max_zeroed_torque_norm_nm"]) <= 0.5
        ),
    }
    gate["ok"] = all(gate.values())
    return gate


def _run_no_contact_episode(
    *,
    family: str,
    seed: int,
    args: argparse.Namespace,
    output_dir: Path,
) -> dict[str, Any]:
    status = legacy.readonly_status(args.robot_host)
    _validate_common_preflight(status)
    anchor = tuple(float(value) for value in status["rtde"]["actual_TCP_pose"])
    tube = _episode_tube(anchor)
    timeline = build_formal_trajectory_timeline(
        family=family,
        seed=seed,
        anchor_pose_base=anchor,
        u_axis_base=U_AXIS_BASE,
        v_axis_base=V_AXIS_BASE,
    )
    if timeline.max_anchor_excursion_m > 0.010:
        raise RuntimeError("formal_qualification_path_exceeds_10mm_envelope")
    for row in timeline.rows:
        tube.assert_contains_pose(row.desired_pose_base, role="desired")
    source = build_live_receiver_source(
        tube, guard_force_limit_n=6.0, guard_torque_limit_nm=0.5
    )
    contract = parse_live_receiver_source(source)
    if contract.guard_force_limit_n != 6.0 or contract.guard_torque_limit_nm != 0.5:
        raise RuntimeError("formal_qualification_receiver_guard_mismatch")
    source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    calibration, calibration_sha256 = legacy.validate_calibration(
        args.kunwei_calibration.resolve()
    )
    lease_id, episode_identity = legacy._new_live_identity_pair()
    idle = legacy._command_packet(
        command=legacy.MODE_IDLE,
        sequence=0,
        progress_s=0.0,
        pose=anchor,
        lease_id=lease_id,
        episode_identity=episode_identity,
    )
    outgoing = idle
    lineages: dict[int, legacy.CommandLineage] = {0: idle.lineage}
    scheduler = legacy.AckPacedScheduler()
    rows: list[dict[str, Any]] = []
    complete = False
    torque_start: float | None = None
    output_dir.mkdir(parents=True, exist_ok=False)
    with (
        legacy.KunweiGuardCapture(
            sensor_ip=args.sensor_ip,
            sensor_port=args.sensor_port,
            connect_timeout_s=args.connect_timeout_s,
            output_dir=output_dir,
            calibration=calibration,
            delivery_watchdog_s=args.sensor_delivery_watchdog_s,
            active_force_limit_n=6.0,
            active_torque_limit_nm=0.5,
        ) as kunwei,
        legacy.LiveRTDE(args.robot_host, timeout=args.connect_timeout_s) as rtde,
    ):
        kunwei.wait_preflight()
        rtde.negotiate()
        output_recipe, output_types = rtde.setup_outputs(500.0, legacy.OUTPUT_FIELDS)
        input_recipe, input_types = rtde.setup_inputs(legacy.INPUT_FIELDS)
        rtde.start()
        legacy._prime_idle_inputs(
            rtde,
            input_recipe,
            input_types,
            outgoing.values,
            output_recipe,
            output_types,
            legacy.OUTPUT_FIELDS,
        )
        try:
            legacy._send_urscript_with_primary_start_barrier(
                args.robot_host, source, timeout_s=args.connect_timeout_s
            )
            pending: list[dict[str, Any]] = []
            start, _ = legacy._wait_for_fresh_receiver_waiting(
                rtde,
                output_recipe,
                output_types,
                legacy.OUTPUT_FIELDS,
                receiver_wait_s=args.receiver_wait_s,
                lease_id=lease_id,
                episode_identity=episode_identity,
                samples_out=pending,
            )
            scheduler.arm(start)
            while True:
                guard = kunwei.snapshot(max_age_s=args.sensor_delivery_watchdog_s)
                if not pending:
                    pending = legacy._receive_available(
                        rtde,
                        output_recipe,
                        output_types,
                        legacy.OUTPUT_FIELDS,
                        0.01,
                    )
                    if not pending:
                        raise RuntimeError("formal_qualification_rtde_output_stale")
                last_sample = pending[-1]
                for sample in pending:
                    state = int(sample["output_int_register_24"])
                    fault = int(sample["output_int_register_26"])
                    if int(sample["robot_mode"]) != legacy.ROBOT_MODE_RUNNING or int(sample["safety_mode"]) != legacy.SAFETY_MODE_NORMAL:
                        raise RuntimeError("formal_qualification_runtime_safety_changed")
                    if int(sample["output_int_register_32"]) != legacy.LIVE_PROTOCOL_TOKEN:
                        raise RuntimeError("formal_qualification_protocol_changed")
                    if state == legacy.STATE_FAULT or fault != 0:
                        raise RuntimeError(f"formal_qualification_receiver_fault:{fault}")
                    ack = int(sample["output_int_register_25"])
                    acked = lineages.get(ack)
                    elapsed = time.monotonic() - start
                    row = legacy._output_row(
                        sample, elapsed, outgoing=outgoing.lineage, acked=acked
                    )
                    row["trajectory_family"] = family
                    row["trajectory_seed"] = seed
                    rows.append(row)
                    if state in (legacy.STATE_STARTUP, legacy.STATE_TORQUE) and torque_start is None:
                        torque_start = time.monotonic()
                    if state == legacy.STATE_COMPLETE:
                        complete = True
                active_elapsed = 0.0 if torque_start is None else time.monotonic() - torque_start
                if complete:
                    break
                if active_elapsed >= timeline.duration_s and torque_start is not None:
                    final_ref = timeline.row_at(timeline.duration_s)
                    outgoing = legacy._command_packet(
                        command=legacy.MODE_END,
                        sequence=outgoing.lineage.command_sequence,
                        progress_s=timeline.duration_s,
                        pose=final_ref["desired_pose_base"],
                        lease_id=lease_id,
                        episode_identity=episode_identity,
                        kunwei_guard_wrench_tcp_si=guard.wrench_tcp_si,
                        kunwei_sample_index=guard.sample_index,
                        kunwei_receive_batch_id=guard.receive_batch_id,
                        kunwei_nominal_sensor_time_s=guard.nominal_sensor_time_s,
                        kunwei_batch_arrival_monotonic_s=guard.t_monotonic_s,
                    )
                    rtde.send_inputs(input_recipe, input_types, outgoing.values)
                elif scheduler.release_due(start + (time.monotonic() - start)):
                    next_sequence = scheduler.next_sequence(
                        int(last_sample["output_int_register_25"])
                    )
                    if next_sequence is not None:
                        reference = timeline.row_at(active_elapsed)
                        outgoing = legacy._command_packet(
                            command=legacy.MODE_RUN,
                            sequence=next_sequence,
                            progress_s=float(reference["progress_s"]),
                            pose=reference["desired_pose_base"],
                            lease_id=lease_id,
                            episode_identity=episode_identity,
                            kunwei_guard_wrench_tcp_si=guard.wrench_tcp_si,
                            kunwei_sample_index=guard.sample_index,
                            kunwei_receive_batch_id=guard.receive_batch_id,
                            kunwei_nominal_sensor_time_s=guard.nominal_sensor_time_s,
                            kunwei_batch_arrival_monotonic_s=guard.t_monotonic_s,
                        )
                        rtde.send_inputs(input_recipe, input_types, outgoing.values)
                        legacy._register_command_lineage(lineages, outgoing)
                pending = []
        except Exception:
            try:
                abort = legacy._command_packet(
                    command=legacy.MODE_ABORT,
                    sequence=outgoing.lineage.command_sequence,
                    progress_s=outgoing.lineage.progress_s,
                    pose=outgoing.lineage.desired_pose,
                    lease_id=lease_id,
                    episode_identity=episode_identity,
                    kunwei_guard_wrench_tcp_si=outgoing.lineage.kunwei_guard_wrench_tcp_si,
                )
                rtde.send_inputs(input_recipe, input_types, abort.values)
            finally:
                raise
        finally:
            kunwei.stop()
            kunwei_summary = kunwei.summary()
    csv_path = output_dir / "direct_torque_rtde.csv"
    _write_csv_new(csv_path, rows)
    gate = _strict_episode_gate(
        rows=rows,
        kunwei=kunwei_summary,
        complete=complete,
        duration_s=timeline.duration_s,
    )
    evidence = {
        "schema": FORMAL_QUALIFICATION_SCHEMA_V1,
        "claim_class": "live_no_contact_seven_family_qualification",
        "ok": gate["ok"],
        "trajectory_family": family,
        "trajectory_seed": seed,
        "duration_s": timeline.duration_s,
        "rate_hz": timeline.rate_hz,
        "max_translation_speed_m_s": timeline.max_translation_speed_m_s,
        "max_translation_acceleration_m_s2": timeline.max_translation_acceleration_m_s2,
        "max_anchor_excursion_m": timeline.max_anchor_excursion_m,
        "receiver_source_sha256": source_sha256,
        "receiver_guard": {"force_limit_n": 6.0, "torque_limit_nm": 0.5},
        "kunwei_calibration_sha256": calibration_sha256,
        "kunwei": kunwei_summary,
        "strict_success_gate": gate,
        "model_active": False,
        "shadow_only": True,
        "contact_authorized": False,
        "training_dataset": False,
        "ur_internal_ft_used": False,
        "data_csv": str(csv_path),
    }
    evidence_path = output_dir / "evidence.json"
    _write_json_new(evidence_path, evidence)
    if not evidence["ok"]:
        raise RuntimeError(f"formal_qualification_gate_failed:{family}")
    return evidence


def run_qualify_seven(args: argparse.Namespace) -> dict[str, Any]:
    if not all(
        (
            args.live,
            args.send_urscript,
            args.write_rtde_inputs,
            args.allow_direct_torque,
            args.allow_motion,
            args.no_contact,
            args.allow_kunwei_stream_command,
        )
    ):
        raise RuntimeError("formal_qualification_explicit_live_gates_required")
    identity = resolve_formal_current_state(REPOSITORY_ROOT)
    if identity.get("ok") is not True:
        raise RuntimeError(f"formal_identity_blocked:{identity.get('blockers')}")
    if identity.get("current_stage_id") != "formal_v4_no_contact_qualification":
        raise RuntimeError("formal_qualification_stage_identity_mismatch")
    dynamics = _load_dynamics_receipt(args.dynamics_evidence.resolve())
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    results: list[dict[str, Any]] = []
    with legacy._live_writer_lease():
        legacy._enforce_no_live_writer_conflict()
        for index, family in enumerate(TRAJECTORY_FAMILIES):
            results.append(
                _run_no_contact_episode(
                    family=family,
                    seed=args.seed + index,
                    args=args,
                    output_dir=root / f"{index:02d}_{family}",
                )
            )
    summary = {
        "schema": FORMAL_TOOL_SCHEMA_V1,
        "claim_class": "live_no_contact_all_seven_trajectories_qualified",
        "ok": len(results) == 7 and all(result["ok"] for result in results),
        "families": [result["trajectory_family"] for result in results],
        "episodes": results,
        "dynamics_evidence_sha256": _sha256(args.dynamics_evidence.resolve()),
        "dynamics_receipt_sha256": dynamics["conformance_receipt"]["receipt_sha256"],
        "source_content_sha256": identity["source_content_sha256"],
        "model_active": False,
        "contact_authorized": False,
        "training_dataset": False,
        "ur_internal_ft_used": False,
    }
    _write_json_new(root / "qualification_summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("resolve")

    initialize = subparsers.add_parser("init-campaign")
    initialize.add_argument("--kind", choices=("fixed_k", "variable_k"), required=True)
    initialize.add_argument("--campaign-root", type=Path, required=True)
    initialize.add_argument("--dynamics-evidence", type=Path, required=True)
    initialize.add_argument("--fixed-campaign-root", type=Path)
    initialize.add_argument("--fixed-k-qualification", type=Path)

    status = subparsers.add_parser("campaign-status")
    status.add_argument("--kind", choices=("fixed_k", "variable_k"), required=True)
    status.add_argument("--campaign-root", type=Path, required=True)

    promote = subparsers.add_parser("promote-qualification")
    promote.add_argument("--qualification-summary", type=Path, required=True)

    invalidate = subparsers.add_parser("invalidate-qualification")
    invalidate.add_argument("--reason", required=True)

    dataset = subparsers.add_parser("build-dataset")
    dataset.add_argument("--kind", choices=("fixed_k", "variable_k"), required=True)
    dataset.add_argument("--campaign-root", type=Path, required=True)
    dataset.add_argument("--output-dir", type=Path, required=True)

    train = subparsers.add_parser("train")
    train.add_argument("--training-dataset", type=Path, required=True)
    train.add_argument("--training-manifest", type=Path, required=True)
    train.add_argument("--output-dir", type=Path, required=True)
    train.add_argument("--epochs", type=int, default=10)
    train.add_argument("--seed", type=int, default=42)
    train.add_argument("--device", default="cpu")

    benchmark = subparsers.add_parser("benchmark")
    benchmark.add_argument("--training-receipt", type=Path, required=True)
    benchmark.add_argument("--training-dataset", type=Path, required=True)
    benchmark.add_argument("--output-dir", type=Path, required=True)
    benchmark.add_argument("--duration-per-rate-s", type=float, default=10.0)

    collect = subparsers.add_parser("collect-campaign")
    collect.add_argument("--kind", choices=("fixed_k", "variable_k"), required=True)
    collect.add_argument("--campaign-root", type=Path, required=True)
    collect.add_argument("--dynamics-evidence", type=Path, required=True)
    collect.add_argument("--robot-host", default="192.168.1.18")
    collect.add_argument("--sensor-ip", default="192.168.50.25")
    collect.add_argument("--sensor-port", type=int, default=5152)
    collect.add_argument(
        "--kunwei-calibration",
        type=Path,
        default=ROOT / "config" / "step5d_tacdiffusion_sensor_frame_v4.json",
    )
    collect.add_argument("--seed", type=int, default=2000)
    collect.add_argument("--connect-timeout-s", type=float, default=3.0)
    collect.add_argument("--receiver-wait-s", type=float, default=2.0)
    collect.add_argument("--sensor-delivery-watchdog-s", type=float, default=0.080)
    collect.add_argument("--max-attempts", type=int, default=0)
    collect.add_argument("--live", action="store_true")
    collect.add_argument("--send-urscript", action="store_true")
    collect.add_argument("--write-rtde-inputs", action="store_true")
    collect.add_argument("--allow-direct-torque", action="store_true")
    collect.add_argument("--allow-motion", action="store_true")
    collect.add_argument("--allow-contact", action="store_true")
    collect.add_argument("--allow-kunwei-stream-command", action="store_true")

    qualify = subparsers.add_parser("qualify-seven-no-contact")
    qualify.add_argument("--robot-host", default="192.168.1.18")
    qualify.add_argument("--sensor-ip", default="192.168.50.25")
    qualify.add_argument("--sensor-port", type=int, default=5152)
    qualify.add_argument(
        "--kunwei-calibration",
        type=Path,
        default=ROOT / "config/step5d_tacdiffusion_sensor_frame_v4.json",
    )
    qualify.add_argument("--dynamics-evidence", type=Path, required=True)
    qualify.add_argument("--output-dir", type=Path, required=True)
    qualify.add_argument("--seed", type=int, default=1000)
    qualify.add_argument("--connect-timeout-s", type=float, default=3.0)
    qualify.add_argument("--receiver-wait-s", type=float, default=2.0)
    qualify.add_argument(
        "--sensor-delivery-watchdog-s", type=float, default=0.080
    )
    qualify.add_argument("--live", action="store_true")
    qualify.add_argument("--send-urscript", action="store_true")
    qualify.add_argument("--write-rtde-inputs", action="store_true")
    qualify.add_argument("--allow-direct-torque", action="store_true")
    qualify.add_argument("--allow-motion", action="store_true")
    qualify.add_argument("--no-contact", action="store_true")
    qualify.add_argument("--allow-kunwei-stream-command", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "resolve":
            result = run_resolve(args)
        elif args.command == "init-campaign":
            if args.kind == "variable_k" and args.fixed_campaign_root is None:
                raise ValueError("variable_k init requires --fixed-campaign-root")
            result = run_init_campaign(args)
        elif args.command == "campaign-status":
            result = run_campaign_status(args)
        elif args.command == "promote-qualification":
            result = run_promote_qualification(args)
        elif args.command == "invalidate-qualification":
            result = run_invalidate_qualification(args)
        elif args.command == "build-dataset":
            result = run_build_dataset(args)
        elif args.command == "train":
            result = run_train(args)
        elif args.command == "benchmark":
            result = run_benchmark(args)
        elif args.command == "collect-campaign":
            result = run_collect_campaign(args)
        elif args.command == "qualify-seven-no-contact":
            result = run_qualify_seven(args)
        else:
            raise ValueError(f"unsupported formal command: {args.command}")
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
