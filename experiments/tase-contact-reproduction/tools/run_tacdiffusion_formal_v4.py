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
    FORMAL_EXPERT_ACTION_COMPONENT_ABS_MAX,
    FORMAL_EXPERT_ACTION_FORCE_NORM_MAX_N,
    FORMAL_EXPERT_ACTION_LIMITS_SCHEMA_V1,
    FORMAL_EXPERT_ACTION_SLEW_PER_S,
    FORMAL_EXPERT_ACTION_TORQUE_NORM_MAX_NM,
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
    FORMAL_SENSOR_DELIVERY_WATCHDOG_S,
    FormalAttemptOutcome,
    FormalAttemptPhase,
    FormalAttemptReceiptV1,
    FormalCampaignLedgerV1,
    classify_fault,
)
from ur10e_vic.tacdiffusion.formal_contact_acquisition import (  # noqa: E402
    ACQUISITION_COMMAND_ABORT,
    ACQUISITION_COMMAND_PREPARE,
    ACQUISITION_COMMAND_START,
    ACQUISITION_COMMAND_STOP_NO_CONTACT,
    ACQUISITION_ROUTE_TOKEN,
    FORMAL_ROUTE_IDENTITY,
    AcquisitionHandoffV1,
    AcquisitionState,
    FormalContactAcquisitionControllerV1,
    KunweiAcquisitionSample,
    StationaryPoseSample,
    build_formal_contact_acquisition_urscript,
    build_formal_direct_torque_tracking_source_after_handoff,
    parse_formal_contact_acquisition_urscript,
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
FORMAL_ROBOT_HOST = "192.168.1.18"
FORMAL_SENSOR_IP = "192.168.50.25"
FORMAL_SENSOR_PORT = 5152
FORMAL_CONTROL_CLOCK_FIT_TOLERANCE_S = 0.0001


def _parse_exact_sensor_delivery_watchdog(value: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "formal sensor delivery watchdog must be exactly 0.080 s"
        ) from exc
    if not math.isfinite(parsed) or parsed != FORMAL_SENSOR_DELIVERY_WATCHDOG_S:
        raise argparse.ArgumentTypeError(
            "formal sensor delivery watchdog must be exactly 0.080 s"
        )
    return parsed


def _require_exact_sensor_delivery_watchdog(args: argparse.Namespace) -> None:
    value = float(args.sensor_delivery_watchdog_s)
    if not math.isfinite(value) or value != FORMAL_SENSOR_DELIVERY_WATCHDOG_S:
        raise RuntimeError(
            "formal_sensor_delivery_watchdog_must_be_exactly_0.080_s"
        )


def _require_frozen_live_endpoints(args: argparse.Namespace) -> None:
    if str(args.robot_host) != FORMAL_ROBOT_HOST:
        raise RuntimeError("formal_controller_endpoint_identity_mismatch")
    if str(args.sensor_ip) != FORMAL_SENSOR_IP or int(args.sensor_port) != FORMAL_SENSOR_PORT:
        raise RuntimeError("formal_kunwei_endpoint_identity_mismatch")


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


FORMAL_QUALIFICATION_INVALIDATION_REASONS_V1 = frozenset(
    {
        "source_changed_after_contact_acceptance_repair",
        "source_changed_after_bounded_formal_control_clock_repair",
    }
)


def run_invalidate_qualification(args: argparse.Namespace) -> dict[str, Any]:
    """Return to qualification after an accepted source identity changes."""

    identity = resolve_formal_current_state(REPOSITORY_ROOT)
    if identity.get("ok") is not True:
        raise RuntimeError(f"formal_identity_blocked:{identity.get('blockers')}")
    if identity.get("current_stage_id") != "formal_v4_fixed_k_campaign":
        raise RuntimeError("formal_qualification_invalidation_stage_mismatch")
    if args.reason not in FORMAL_QUALIFICATION_INVALIDATION_REASONS_V1:
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


def _formal_track_state_torque_rows(
    rows: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Select the sole formal training window: TRACK plus STATE_TORQUE."""

    return [
        row
        for row in rows
        if row.get("formal_phase") == FormalAttemptPhase.TRACK.value
        and int(row.get("receiver_state", -1)) == legacy.STATE_TORQUE
    ]


def _select_formal_artifact_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[
    list[Mapping[str, Any]],
    list[tuple[Mapping[str, Any], Mapping[str, Any] | None]],
    dict[str, Any],
]:
    """Select coherent, lineage-bound rows without erasing raw TRACK evidence.

    RTDE can observe the action-register seqlock while its generation changes.
    Those torn snapshots remain in ``direct_torque_rtde.csv`` and count toward
    the physical eight-second TRACK window, but they are not valid 84D/12D
    training rows.  Each accepted row is paired with its immediately preceding
    raw runtime row so production dynamics retains previous-controller-tick
    semantics even when that preceding row has a torn action echo.
    """

    raw_track_rows = _formal_track_state_torque_rows(rows)
    selected: list[
        tuple[Mapping[str, Any], Mapping[str, Any] | None]
    ] = []
    rejected_torn = 0
    rejected_unbound = 0
    for index, row in enumerate(raw_track_rows):
        coherent = legacy._action_echo_coherent(row)
        lineage_bound = int(row.get("ack_command_lineage_missing", 1)) == 0
        if coherent and lineage_bound:
            selected.append(
                (row, raw_track_rows[index - 1] if index > 0 else None)
            )
            continue
        if not coherent:
            rejected_torn += 1
        if not lineage_bound:
            rejected_unbound += 1
    if raw_track_rows and not selected:
        raise RuntimeError("formal_track_no_coherent_lineage_bound_rows")
    evidence = {
        "schema_version": "ur10e_tacdiffusion_formal_row_selection/v1",
        "raw_track_state_torque_row_count": len(raw_track_rows),
        "selected_coherent_lineage_bound_row_count": len(selected),
        "rejected_row_count": len(raw_track_rows) - len(selected),
        "rejected_torn_row_count": rejected_torn,
        "rejected_unbound_row_count": rejected_unbound,
        "selection_predicate": (
            "formal_phase=TRACK AND receiver_state=STATE_TORQUE AND "
            "legacy._action_echo_coherent(row) AND "
            "ack_command_lineage_missing=0"
        ),
        "raw_evidence_path": "direct_torque_rtde.csv",
    }
    return raw_track_rows, selected, evidence


def _formal_recorder_metadata(
    *,
    formal_manifest: FormalEpisodeManifestV1,
    semantic_fingerprint_sha256: str,
    row_selection: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "formal_manifest": formal_manifest.as_json(),
        "semantic_context_fingerprint_sha256": semantic_fingerprint_sha256,
        "semantic_context": {
            "hash_identities": dict(formal_manifest.source_hashes),
            "semantic_context_fingerprint_sha256": semantic_fingerprint_sha256,
        },
        "raw_rtde_csv": "direct_torque_rtde.csv",
        "row_selection": dict(row_selection),
    }


def _build_formal_control_clock(
    selected_rows: Sequence[tuple[Mapping[str, Any], Mapping[str, Any] | None]],
) -> tuple[legacy.RecorderControlClock, dict[str, Any]]:
    """Bind the robot 500 Hz grid between sensor arrival and host processing."""

    if not selected_rows:
        raise RuntimeError("formal_control_clock_rows_missing")
    lower_bounds: list[float] = []
    upper_bounds: list[float] = []
    for row, _previous_runtime_row in selected_rows:
        try:
            controller = float(row["controller_timestamp_s"])
            sensor_arrival = float(row["kunwei_batch_arrival_monotonic_s"])
            host_processing = float(row["host_monotonic_s"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("formal_control_clock_evidence_missing") from exc
        if not all(
            math.isfinite(value)
            for value in (controller, sensor_arrival, host_processing)
        ):
            raise RuntimeError("formal_control_clock_evidence_nonfinite")
        if sensor_arrival > host_processing + 1e-12:
            raise RuntimeError("formal_control_clock_sensor_arrival_after_host_processing")
        lower_bounds.append(sensor_arrival - controller)
        upper_bounds.append(host_processing - controller)
    lower = max(lower_bounds)
    upper = min(upper_bounds)
    fit_tolerance_s = FORMAL_CONTROL_CLOCK_FIT_TOLERANCE_S
    if lower > upper + fit_tolerance_s:
        raise RuntimeError("formal_control_clock_bounds_do_not_intersect")
    # Prefer the latest admissible offset so host-age evidence is
    # conservative.  When host/controller timestamp quantization leaves a
    # sub-0.1 ms overlap deficit, keep sensor causality exact and record that
    # bounded host-processing fit uncertainty explicitly.
    offset = max(lower, upper)
    first_controller = float(selected_rows[0][0]["controller_timestamp_s"])
    clock = legacy.RecorderControlClock(
        host_anchor_s=first_controller + offset,
    )
    return clock, {
        "schema_version": "ur10e_tacdiffusion_formal_control_clock/v1",
        "method": "robot_grid_offset_bounded_by_kunwei_arrival_and_host_processing",
        "offset_lower_bound_s": lower,
        "offset_upper_bound_s": upper,
        "selected_offset_s": offset,
        "bound_width_s": upper - lower,
        "fit_tolerance_s": fit_tolerance_s,
        "host_processing_bound_max_excess_s": max(0.0, lower - upper),
        "row_count": len(selected_rows),
    }


def _formal_acquisition_input_values(
    *,
    command: int,
    sequence: int,
    lease_id: int,
    episode_identity: int,
    safety_normal: bool,
    robot_running: bool,
    host_latch: bool = False,
    handoff_ack: bool = False,
) -> tuple[Any, ...]:
    """Build the separate acquisition register contract.

    The acquisition URScript has a deliberately different integer-register
    schema from the Direct Torque receiver.  Keeping this packet builder
    separate prevents a Direct Torque command from accidentally starting or
    authorizing the velocity phase.
    """

    if int(command) not in {
        ACQUISITION_COMMAND_PREPARE,
        ACQUISITION_COMMAND_START,
        ACQUISITION_COMMAND_ABORT,
        ACQUISITION_COMMAND_STOP_NO_CONTACT,
    }:
        raise ValueError("formal acquisition command is outside the frozen protocol")
    if int(sequence) <= 0:
        raise ValueError("formal acquisition packet sequence must be positive")
    if int(lease_id) <= 0 or int(episode_identity) <= 0:
        raise ValueError("formal acquisition packet identity must be positive")
    integers = (
        int(command),
        int(sequence),
        int(lease_id),
        int(episode_identity),
        ACQUISITION_ROUTE_TOKEN,
        int(bool(safety_normal)),
        int(bool(robot_running)),
        int(bool(host_latch)),
        int(bool(handoff_ack)),
        0,
        0,
        0,
    )
    return (0.0,) * 24 + integers


def _formal_acquisition_kunwei_sample(
    snapshot: Any,
    *,
    lease_id: int,
    episode_identity: int,
) -> KunweiAcquisitionSample:
    age = max(0.0, time.monotonic() - float(snapshot.t_monotonic_s))
    return KunweiAcquisitionSample(
        sample_index=int(snapshot.sample_index),
        host_age_s=age,
        normal_load_n=float(snapshot.normal_load_n),
        force_norm_n=float(snapshot.force_norm_n),
        torque_norm_nm=float(snapshot.torque_norm_nm),
        route_identity=FORMAL_ROUTE_IDENTITY,
        lease_id=int(lease_id),
        episode_identity=int(episode_identity),
        safety_mode="NORMAL",
        robot_mode="RUNNING",
    )


def _formal_acquisition_stationary_sample(
    sample: Mapping[str, Any],
    *,
    lease_id: int,
    episode_identity: int,
) -> StationaryPoseSample:
    timestamp = float(sample["timestamp"])
    if not math.isfinite(timestamp):
        raise RuntimeError("formal_acquisition_stationary_timestamp_invalid")
    return StationaryPoseSample(
        sample_time_s=timestamp,
        host_age_s=0.0,
        actual_pose_base=tuple(float(value) for value in sample["actual_TCP_pose"]),
        tcp_speed_base=tuple(float(value) for value in sample["actual_TCP_speed"]),
        joint_speed_rad_s=tuple(float(value) for value in sample["actual_qd"]),
        route_identity=FORMAL_ROUTE_IDENTITY,
        lease_id=int(lease_id),
        episode_identity=int(episode_identity),
        safety_mode=(
            "NORMAL"
            if int(sample["safety_mode"]) == legacy.SAFETY_MODE_NORMAL
            else "FAULT"
        ),
        robot_mode=(
            "RUNNING"
            if int(sample["robot_mode"]) == legacy.ROBOT_MODE_RUNNING
            else "STOPPED"
        ),
    )


def _formal_acquisition_evidence_row(
    sample: Mapping[str, Any],
    *,
    elapsed_s: float,
    attempt_id: str,
    host_sequence: int,
) -> dict[str, Any]:
    """Build an acquisition-only row with no Direct Torque register meanings."""

    def _vector(prefix: str, values: object, length: int) -> dict[str, float]:
        sequence = tuple(float(value) for value in values)  # type: ignore[arg-type]
        if len(sequence) != length or not all(math.isfinite(value) for value in sequence):
            raise RuntimeError(f"formal_acquisition_{prefix}_nonfinite")
        return {f"{prefix}_{index}": value for index, value in enumerate(sequence)}

    row: dict[str, Any] = {
        "schema_version": "ur10e_tacdiffusion_contact_acquisition_evidence/v1",
        "acquisition_evidence_only": True,
        "acquisition_training": False,
        "formal_training_included": False,
        "protocol_evidence": False,
        "capture_phase": FormalAttemptPhase.ACQUISITION.value,
        "formal_phase": FormalAttemptPhase.ACQUISITION.value,
        "acquisition_sample_id": f"{attempt_id}:acquisition:{host_sequence}",
        "host_elapsed_s": float(elapsed_s),
        "host_packet_sequence": int(host_sequence),
        "acquisition_output_state": int(sample["output_int_register_24"]),
        "acquisition_output_sequence": int(sample["output_int_register_25"]),
        "acquisition_output_fault": int(sample["output_int_register_26"]),
        "acquisition_lease_echo": int(sample["output_int_register_27"]),
        "acquisition_episode_echo": int(sample["output_int_register_28"]),
        "acquisition_route_echo": int(sample["output_int_register_29"]),
        "acquisition_handoff_ack": int(sample["output_int_register_30"]),
        "acquisition_host_latch_echo": int(sample["output_int_register_31"]),
        "controller_timestamp_s": float(sample["timestamp"]),
        "runtime_state": int(sample["runtime_state"]),
        "robot_mode": int(sample["robot_mode"]),
        "safety_mode": int(sample["safety_mode"]),
        "force_authority": "kunwei_kwr75_tcp_raw_stream_v1",
    }
    row.update(_vector("actual_TCP_pose", sample["actual_TCP_pose"], 6))
    row.update(_vector("actual_TCP_speed", sample["actual_TCP_speed"], 6))
    row.update(_vector("actual_qd", sample["actual_qd"], 6))
    return row


def _formal_acquisition_receive_available(
    rtde: Any,
    output_recipe: int,
    output_types: list[str],
    timeout_s: float,
) -> list[dict[str, Any]]:
    """Bound acquisition reads so the 500 Hz stream cannot starve host ACKs."""

    bounded = getattr(rtde, "receive_available_bounded", None)
    if callable(bounded):
        return list(
            bounded(
                output_recipe,
                output_types,
                legacy.OUTPUT_FIELDS,
                timeout_s,
                max_samples=4,
                max_wall_s=0.004,
            )
        )
    return legacy._receive_available(
        rtde,
        output_recipe,
        output_types,
        legacy.OUTPUT_FIELDS,
        timeout_s,
    )


def _formal_handoff_idle_packet(
    *,
    handoff: AcquisitionHandoffV1,
    guard: Any,
    lease_id: int,
    episode_identity: int,
) -> Any:
    """Re-prime Direct Torque with a fresh sequence-0 handoff identity."""

    if int(lease_id) != handoff.lease_id or int(episode_identity) != handoff.episode_identity:
        raise RuntimeError("formal_handoff_idle_identity_mismatch")
    packet = legacy._command_packet(
        command=legacy.MODE_IDLE,
        sequence=0,
        progress_s=0.0,
        pose=handoff.anchor_pose_base,
        lease_id=lease_id,
        episode_identity=episode_identity,
        kunwei_guard_wrench_tcp_si=guard.wrench_tcp_si,
        kunwei_sample_index=guard.sample_index,
        kunwei_receive_batch_id=guard.receive_batch_id,
        kunwei_nominal_sensor_time_s=guard.nominal_sensor_time_s,
        kunwei_batch_arrival_monotonic_s=guard.t_monotonic_s,
        stiffness_6d=FixedKExpertV1().stiffness_6d,
        raw_f_ff_6d=(0.0,) * 6,
    )
    lineage = packet.lineage
    if (
        lineage.command_sequence != 0
        or lineage.desired_pose != handoff.anchor_pose_base
        or lineage.commanded_k != FixedKExpertV1().stiffness_6d
        or lineage.commanded_raw_f_ff != (0.0,) * 6
        or lineage.kunwei_sample_index != int(guard.sample_index)
        or lineage.kunwei_receive_batch_id != int(guard.receive_batch_id)
    ):
        raise RuntimeError("formal_handoff_idle_packet_not_bumpless")
    return packet


def _run_formal_acquisition_phase(
    *,
    args: argparse.Namespace,
    rtde: Any,
    input_recipe: int,
    input_types: list[str],
    output_recipe: int,
    output_types: list[str],
    contract: ContactAcquisitionContractV1,
    controller: FormalContactAcquisitionControllerV1,
    kunwei: Any,
    lease_id: int,
    episode_identity: int,
    acquisition_source: str,
    attempt_id: str,
    acquisition_rows: list[dict[str, Any]],
) -> tuple[AcquisitionHandoffV1, tuple[float, ...], tuple[float, ...], dict[str, Any]]:
    """Run acquisition to a stationary handoff before any Direct Torque source.

    This helper is intentionally the only live seam that sends the acquisition
    source.  It consumes every bounded native Kunwei frame, keeps the host
    latch authoritative, and returns only a proven handoff anchor.
    """

    prepare_packet = _formal_acquisition_input_values(
        command=ACQUISITION_COMMAND_PREPARE,
        sequence=1,
        lease_id=lease_id,
        episode_identity=episode_identity,
        safety_normal=True,
        robot_running=True,
    )
    rtde.send_inputs(input_recipe, input_types, prepare_packet)
    # A single RTDE input package can race the first controller tick of a newly
    # started Secondary program.  Prime the same inert PREPARE identity across
    # multiple fresh controller ticks so the acquisition program cannot see
    # the default zero lease/episode on its first read and fail closed before
    # it has a chance to acknowledge sequence 1.
    legacy._prime_idle_inputs(
        rtde,
        input_recipe,
        input_types,
        prepare_packet,
        output_recipe,
        output_types,
        legacy.OUTPUT_FIELDS,
        timeout_s=0.250,
        minimum_fresh_ticks=5,
    )
    barrier = legacy._send_urscript_with_primary_start_barrier(
        args.robot_host,
        acquisition_source,
        timeout_s=args.connect_timeout_s,
    )
    sequence = 1
    prepare_ack_observed = False
    prepare_last_sample: Mapping[str, Any] | None = None
    prepare_observation_started_s = time.monotonic()
    prepare_deadline = time.monotonic() + 0.250
    while time.monotonic() < prepare_deadline and not prepare_ack_observed:
        for sample in _formal_acquisition_receive_available(
            rtde,
            output_recipe,
            output_types,
            0.010,
        ):
            prepare_last_sample = sample
            acquisition_rows.append(
                _formal_acquisition_evidence_row(
                    sample,
                    elapsed_s=time.monotonic() - prepare_observation_started_s,
                    attempt_id=attempt_id,
                    host_sequence=sequence,
                )
            )
            if int(sample["robot_mode"]) != legacy.ROBOT_MODE_RUNNING:
                raise RuntimeError("formal_acquisition_prepare_robotmode_changed")
            if int(sample["safety_mode"]) != legacy.SAFETY_MODE_NORMAL:
                raise RuntimeError("formal_acquisition_prepare_safety_changed")
            if int(sample["output_int_register_26"]) != 0:
                raise RuntimeError("formal_acquisition_prepare_fault")
            if (
                int(sample["output_int_register_24"]) == 0
                and int(sample["output_int_register_25"]) == sequence
                and int(sample["output_int_register_29"])
                == ACQUISITION_ROUTE_TOKEN
            ):
                prepare_ack_observed = True
    if not prepare_ack_observed:
        diagnostic = {
            key: None if prepare_last_sample is None else prepare_last_sample.get(key)
            for key in (
                "timestamp",
                "runtime_state",
                "robot_mode",
                "safety_mode",
                "output_int_register_24",
                "output_int_register_25",
                "output_int_register_26",
                "output_int_register_29",
            )
        }
        raise RuntimeError(
            "formal_acquisition_prepare_ack_timeout:"
            + json.dumps(diagnostic, sort_keys=True, separators=(",", ":"))
        )
    barrier = {
        **barrier,
        "prepare_command": ACQUISITION_COMMAND_PREPARE,
        "prepare_sequence": sequence,
        "prepare_ack_observed": True,
        "motion_armed_during_barrier": False,
    }
    latch_sent = False
    handoff_ack_sent = False
    handoff_ack_packet_sent = False
    last_ack_sequence = sequence
    last_packet_values: tuple[Any, ...] | None = None
    pending: list[Mapping[str, Any]] = []
    last_actual_pose: tuple[float, ...] | None = None
    last_actual_speed: tuple[float, ...] | None = None
    handoff: AcquisitionHandoffV1 | None = None

    def send_packet(
        *,
        safety_normal: bool,
        host_latch: bool,
        handoff_ack: bool,
        command: int = ACQUISITION_COMMAND_START,
    ) -> None:
        nonlocal sequence, last_packet_values
        sequence += 1
        last_packet_values = _formal_acquisition_input_values(
            command=command,
            sequence=sequence,
            lease_id=lease_id,
            episode_identity=episode_identity,
            safety_normal=safety_normal,
            robot_running=safety_normal,
            host_latch=host_latch,
            handoff_ack=handoff_ack,
        )
        rtde.send_inputs(
            input_recipe,
            input_types,
            last_packet_values,
        )

    def stop_fault(reason: str) -> None:
        controller.fail_closed(reason)
        try:
            send_packet(
                safety_normal=False,
                host_latch=False,
                handoff_ack=False,
                command=ACQUISITION_COMMAND_ABORT,
            )
        except Exception:
            pass

    def fault_reason(text: object) -> str:
        lowered = str(text).lower()
        for candidate in (
            "force_guard",
            "torque_guard",
            "protective_stop",
            "safety_changed",
            "joint_fault",
            "route_identity_changed",
            "route_fault",
        ):
            if candidate in lowered:
                return candidate
        return "sensor_fault"

    # PREPARE is inert for the full 150 ms Primary barrier.  Start both the
    # host profile and the native-frame cursor only after that barrier closes,
    # then arm motion with sequence 2.  This preserves the 80 ms watchdog only
    # for the active-motion phase rather than timing out inside the barrier.
    sensor_cursor = int(
        kunwei.snapshot(max_age_s=contract.sensor_delivery_watchdog_s).sample_index
    )
    acquisition_started_s = time.monotonic()
    controller.start(
        lease_id=lease_id,
        episode_identity=episode_identity,
        route_identity=FORMAL_ROUTE_IDENTITY,
        monotonic_s=acquisition_started_s,
    )
    send_packet(
        safety_normal=True,
        host_latch=False,
        handoff_ack=False,
        command=ACQUISITION_COMMAND_START,
    )
    pending_sequence_started_s = time.monotonic()
    last_packet_transmit_s = pending_sequence_started_s
    deadline = acquisition_started_s + 65.0

    while time.monotonic() < deadline:
        now = time.monotonic()
        try:
            snapshots = kunwei.snapshots_since(
                sensor_cursor,
                max_age_s=contract.sensor_delivery_watchdog_s,
            )
        except Exception as exc:
            reason = fault_reason(exc)
            stop_fault(reason)
            raise RuntimeError(f"formal_acquisition_{reason}:{exc}") from exc
        for snapshot in snapshots:
            sensor_cursor = int(snapshot.sample_index)
            try:
                latched = controller.observe_kunwei(
                    _formal_acquisition_kunwei_sample(
                        snapshot,
                        lease_id=lease_id,
                        episode_identity=episode_identity,
                    )
                )
            except Exception as exc:
                reason = fault_reason(exc)
                stop_fault(reason)
                raise RuntimeError(f"formal_acquisition_{reason}:{exc}") from exc
            latch_sent = latch_sent or latched

        if not pending:
            pending = _formal_acquisition_receive_available(
                rtde,
                output_recipe,
                output_types,
                0.005,
            )
            if not pending:
                try:
                    controller.command_at(now)
                except Exception as exc:
                    stop_fault("sensor_fault")
                    raise RuntimeError(f"formal_acquisition_state_fault:{exc}") from exc

        for sample in pending:
            if int(sample["robot_mode"]) != legacy.ROBOT_MODE_RUNNING:
                stop_fault("safety_changed")
                raise RuntimeError("formal_acquisition_safety_changed")
            if int(sample["safety_mode"]) != legacy.SAFETY_MODE_NORMAL:
                stop_fault("safety_changed")
                raise RuntimeError("formal_acquisition_safety_changed")
            if int(sample["output_int_register_26"]) != 0:
                stop_fault("route_fault")
                raise RuntimeError("formal_acquisition_route_fault")
            output_state = int(sample["output_int_register_24"])
            output_ack = int(sample["output_int_register_25"])
            if output_ack > sequence:
                stop_fault("route_fault")
                raise RuntimeError("formal_acquisition_ack_ahead_of_host")
            if output_ack == sequence:
                last_ack_sequence = output_ack
            if output_state != 0 and (
                int(sample["output_int_register_27"]) != lease_id
                or int(sample["output_int_register_28"]) != episode_identity
                or int(sample["output_int_register_29"]) != ACQUISITION_ROUTE_TOKEN
            ):
                stop_fault("route_fault")
                raise RuntimeError("formal_acquisition_route_identity_changed")
            try:
                joint_modes = tuple(int(value) for value in sample["joint_mode"])
            except (KeyError, TypeError, ValueError):
                stop_fault("joint_fault")
                raise RuntimeError("formal_acquisition_joint_fault")
            if len(joint_modes) != 6 or any(mode != 253 for mode in joint_modes):
                stop_fault("joint_fault")
                raise RuntimeError("formal_acquisition_joint_fault")
            last_actual_pose = tuple(float(value) for value in sample["actual_TCP_pose"])
            last_actual_speed = tuple(float(value) for value in sample["actual_TCP_speed"])
            if not all(
                math.isfinite(value)
                for value in last_actual_pose + last_actual_speed
            ):
                stop_fault("sensor_fault")
                raise RuntimeError("formal_acquisition_kinematics_nonfinite")
            acquisition_rows.append(
                _formal_acquisition_evidence_row(
                    sample,
                    elapsed_s=time.monotonic() - acquisition_started_s,
                    attempt_id=attempt_id,
                    host_sequence=sequence,
                )
            )
            if controller.state in {
                AcquisitionState.STOPPING,
                AcquisitionState.STATIONARY_DWELL,
            }:
                try:
                    handoff = controller.observe_stationary(
                        _formal_acquisition_stationary_sample(
                            sample,
                            lease_id=lease_id,
                            episode_identity=episode_identity,
                        )
                    )
                except Exception as exc:
                    reason = fault_reason(exc)
                    stop_fault(reason)
                    raise RuntimeError(f"formal_acquisition_{reason}:{exc}") from exc
                if handoff is not None:
                    handoff_ack_sent = True
            if output_ack == sequence and not handoff_ack_packet_sent:
                send_packet(
                    safety_normal=True,
                    host_latch=latch_sent,
                    handoff_ack=handoff_ack_sent,
                )
                pending_sequence_started_s = time.monotonic()
                last_packet_transmit_s = pending_sequence_started_s
                handoff_ack_packet_sent = handoff_ack_sent
            if handoff_ack_sent:
                break
        pending = []

        if controller.state == AcquisitionState.SEARCH_EXHAUSTED:
            if last_ack_sequence == sequence:
                send_packet(
                    safety_normal=True,
                    host_latch=False,
                    handoff_ack=False,
                    command=ACQUISITION_COMMAND_STOP_NO_CONTACT,
                )
                raise RuntimeError("formal_contact_not_found")
        if controller.state == AcquisitionState.FAULT:
            raise RuntimeError(f"formal_acquisition_fault:{controller.fault_reason}")
        now = time.monotonic()
        if last_ack_sequence != sequence and now - last_packet_transmit_s >= 0.010:
            if last_packet_values is None:
                stop_fault("route_fault")
                raise RuntimeError("formal_acquisition_pending_packet_missing")
            # Resend the identical pending packet; never mint a new sequence
            # until the controller ACK catches up.  The total 80 ms ACK bound
            # remains anchored to the first transmission.
            rtde.send_inputs(input_recipe, input_types, last_packet_values)
            last_packet_transmit_s = now
        if (
            last_ack_sequence != sequence
            and now - pending_sequence_started_s
            > contract.sensor_delivery_watchdog_s
        ):
            stop_fault("route_fault")
            raise RuntimeError("formal_acquisition_ack_heartbeat_timeout")
        if handoff_ack_packet_sent:
            observed_terminal = False
            for terminal_sample in _formal_acquisition_receive_available(
                rtde,
                output_recipe,
                output_types,
                0.005,
            ):
                if int(terminal_sample["output_int_register_30"]) == 1 and int(
                    terminal_sample["output_int_register_24"]
                ) == 4:
                    observed_terminal = True
                    last_actual_pose = tuple(
                        float(value) for value in terminal_sample["actual_TCP_pose"]
                    )
                    last_actual_speed = tuple(
                        float(value) for value in terminal_sample["actual_TCP_speed"]
                    )
            if observed_terminal:
                handoff = controller.handoff
                return (
                    handoff,
                    handoff.anchor_pose_base,
                    (0.0,) * 6,
                    barrier,
                )
    stop_fault("sensor_fault")
    raise RuntimeError("formal_acquisition_deadline_exceeded")


def _run_contact_attempt_locked(
    *,
    args: argparse.Namespace,
    ledger: FormalCampaignLedgerV1,
    identity: Mapping[str, Any],
    dynamics_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    _require_exact_sensor_delivery_watchdog(args)
    attempt_ordinal, episode, attempt_dir = ledger.next_attempt(
        verify_artifacts=False
    )
    attempt_dir.mkdir(parents=True, exist_ok=False)
    attempt_id = attempt_dir.name
    contract = ContactAcquisitionContractV1()
    acquisition_source = build_formal_contact_acquisition_urscript(contract)
    parse_formal_contact_acquisition_urscript(acquisition_source)
    status = legacy.readonly_status(args.robot_host)
    _validate_common_preflight(status)
    entry_pose = tuple(float(value) for value in status["rtde"]["actual_TCP_pose"])
    tube = _contact_episode_tube(entry_pose, contract)
    receiver_tube = _contact_receiver_tube(entry_pose, contract)
    receiver_source: str | None = None
    receiver_sha: str | None = None
    calibration, calibration_sha = legacy.validate_calibration(
        args.kunwei_calibration.resolve()
    )
    source_hashes = {
        "source_content": str(identity["source_content_sha256"]),
        "acquisition_source": hashlib.sha256(
            acquisition_source.encode("utf-8")
        ).hexdigest(),
        "acquisition_route_identity": hashlib.sha256(
            FORMAL_ROUTE_IDENTITY.encode("utf-8")
        ).hexdigest(),
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
            "phase": FormalAttemptPhase.ACQUISITION.value,
            "reference_sample_id": f"{attempt_id}:acquisition:0",
            "desired_pose_base": entry_pose,
            "desired_twist_base": (0.0,) * 6,
            "desired_acceleration_base": (0.0,) * 6,
        }
    }
    scheduler = legacy.AckPacedScheduler()
    rows: list[dict[str, Any]] = []
    acquisition_rows: list[dict[str, Any]] = []
    kunwei_summary: dict[str, Any] = {}
    equipment_receipt: dict[str, Any] | None = None
    phase = FormalAttemptPhase.ACQUISITION
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
    end_sent = False
    observed_complete = False
    failure: str | None = None
    fault_class: str | None = None
    auto_return_performed = False
    auto_return_evidence: dict[str, Any] | None = None
    start_s: float | None = None
    primary_barrier: Mapping[str, Any] | None = None
    tracking_started = False
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
                active_force_limit_n=50.0,
                active_torque_limit_nm=4.0,
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
            acquisition_controller = FormalContactAcquisitionControllerV1(contract)
            (
                handoff,
                last_actual_pose,
                last_actual_speed,
                acquisition_barrier,
            ) = _run_formal_acquisition_phase(
                args=args,
                rtde=rtde,
                input_recipe=input_recipe,
                input_types=input_types,
                output_recipe=output_recipe,
                output_types=output_types,
                contract=contract,
                controller=acquisition_controller,
                kunwei=kunwei,
                lease_id=lease_id,
                episode_identity=episode_identity,
                acquisition_source=acquisition_source,
                attempt_id=attempt_id,
                acquisition_rows=acquisition_rows,
            )
            contact_pose = handoff.anchor_pose_base
            contact_latch_sample_index = handoff.contact_latch_sample_index
            handoff_guard = kunwei.snapshot(
                max_age_s=contract.sensor_delivery_watchdog_s
            )
            outgoing = _formal_handoff_idle_packet(
                handoff=handoff,
                guard=handoff_guard,
                lease_id=lease_id,
                episode_identity=episode_identity,
            )
            lineages = {0: outgoing.lineage}
            command_metadata = {
                0: {
                    "phase": FormalAttemptPhase.CONTACT_SETTLE.value,
                    "reference_sample_id": f"{attempt_id}:handoff_idle:0",
                    "desired_pose_base": handoff.anchor_pose_base,
                    "desired_twist_base": (0.0,) * 6,
                    "desired_acceleration_base": (0.0,) * 6,
                }
            }
            receiver_source = build_formal_direct_torque_tracking_source_after_handoff(
                receiver_tube,
                handoff,
                contract,
                fresh_actual_pose_base=handoff.anchor_pose_base,
            )
            receiver_contract = parse_live_receiver_source(receiver_source)
            if (
                receiver_contract.guard_force_limit_n != 50.0
                or receiver_contract.guard_torque_limit_nm != 4.0
                or receiver_contract.formal_handoff_required is not True
                or receiver_contract.model_inactive_expert_feedforward_allowed
                is not True
                or receiver_contract.formal_contact_entry_transition_profile
                != "formal_contact_entry_transition_v1"
                or receiver_contract.formal_contact_entry_transition_ticks != 25
            ):
                raise RuntimeError("formal_contact_receiver_handoff_or_guard_mismatch")
            receiver_sha = hashlib.sha256(receiver_source.encode("utf-8")).hexdigest()
            source_hashes["receiver_source"] = receiver_sha
            source_hashes["handoff_anchor"] = hashlib.sha256(
                json.dumps(handoff.as_json(), sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest()
            source_hashes["handoff_guard"] = hashlib.sha256(
                json.dumps(
                    {
                        "sample_index": handoff_guard.sample_index,
                        "receive_batch_id": handoff_guard.receive_batch_id,
                        "nominal_sensor_time_s": handoff_guard.nominal_sensor_time_s,
                        "t_monotonic_s": handoff_guard.t_monotonic_s,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            source_hashes["handoff_idle_lineage"] = hashlib.sha256(
                json.dumps(
                    {
                        "sequence": outgoing.lineage.command_sequence,
                        "pose": list(outgoing.lineage.desired_pose),
                        "stiffness": list(outgoing.lineage.commanded_k),
                        "raw_feedforward": list(outgoing.lineage.commanded_raw_f_ff),
                        "kunwei_sample_index": outgoing.lineage.kunwei_sample_index,
                        "kunwei_receive_batch_id": outgoing.lineage.kunwei_receive_batch_id,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            formal_manifest = _formal_manifest_for_attempt(
                attempt_id=attempt_id, source_hashes=source_hashes
            )
            legacy._prime_idle_inputs(
                rtde,
                input_recipe,
                input_types,
                outgoing.values,
                output_recipe,
                output_types,
                legacy.OUTPUT_FIELDS,
            )
            phase = FormalAttemptPhase.CONTACT_SETTLE
            phase_started_s = None
            primary_barrier = {"acquisition": acquisition_barrier}
            tracking_started = True
            tracking_barrier = legacy._send_urscript_with_primary_start_barrier(
                args.robot_host,
                receiver_source,
                timeout_s=args.connect_timeout_s,
            )
            primary_barrier["tracking"] = tracking_barrier
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
                    row_host_monotonic_s = time.monotonic()
                    row = legacy._output_row(
                        sample,
                        row_host_monotonic_s - start_s,
                        outgoing=outgoing.lineage,
                        acked=acked,
                    )
                    row["host_monotonic_s"] = row_host_monotonic_s
                    _annotate_contact_row(row, command_metadata.get(ack))
                    rows.append(row)
                    if state == legacy.STATE_COMPLETE:
                        observed_complete = True
                if observed_complete:
                    break

                now = time.monotonic()
                if torque_started_s is not None and phase_started_s is not None:
                    elapsed = now - phase_started_s
                    if phase == FormalAttemptPhase.CONTACT_SETTLE and elapsed >= contract.settle_duration_s:
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

                if end_sent:
                    pending = []
                    continue
                if scheduler.release_due(now):
                    next_sequence = scheduler.next_sequence(
                        int(last_sample["output_int_register_25"])
                    )
                    if next_sequence is not None:
                        desired_pose = (
                            contact_pose if contact_pose is not None else entry_pose
                        )
                        desired_twist = (0.0,) * 6
                        desired_acceleration = (0.0,) * 6
                        reference_sample_id = f"{attempt_id}:{phase.value}:{next_sequence}"
                        feedforward = (0.0,) * 6
                        stiffness = previous_k
                        progress_s = 0.0
                        if torque_started_s is not None and phase_started_s is not None:
                            elapsed = max(0.0, now - phase_started_s)
                            if phase == FormalAttemptPhase.CONTACT_SETTLE:
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
            if observed_complete and track_completed and end_sent:
                auto_return_evidence = _run_monitored_formal_position_return(
                    args=args,
                    rtde=rtde,
                    output_recipe=output_recipe,
                    output_types=output_types,
                    kunwei=kunwei,
                    current_pose_base=last_actual_pose,
                    entry_pose_base=entry_pose,
                )
                auto_return_performed = True
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        fault_class, _ = classify_fault(exc)
        if "formal_contact_not_found" in str(exc):
            contact_not_found = True
            fault_class = "recoverable_runtime"
        # Fault exits Direct Torque only. No automatic retract/home is sent
        # from this handler, including sensor/guard/protective/safety faults.
        try:
            if tracking_started and "rtde" in locals() and outgoing is not None:
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
    acquisition_csv_path = attempt_dir / "contact_acquisition_rtde.csv"
    if acquisition_rows:
        _write_csv_new(acquisition_csv_path, acquisition_rows)
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
        "auto_return": auto_return_evidence,
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
        "entry_transition": _entry_transition_evidence(rows),
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
        "guard_force_limit_n": 50.0,
        "guard_torque_limit_nm": 4.0,
        "model_active": False,
        "shadow_only": True,
        "formal_result": formal_result_for_evidence,
        "rtde_row_count": len(rows),
        "raw_rtde_csv": str(csv_path) if rows else None,
        "acquisition_row_count": len(acquisition_rows),
        "acquisition_raw_rtde_csv": (
            str(acquisition_csv_path) if acquisition_rows else None
        ),
        "acquisition_training": False,
        "acquisition_protocol_evidence": False,
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


def _formal_position_return_source(
    *, current_pose_base: Sequence[float], entry_pose_base: Sequence[float]
) -> str:
    current = tuple(float(value) for value in current_pose_base)
    entry = tuple(float(value) for value in entry_pose_base)
    if (
        len(current) != 6
        or len(entry) != 6
        or not all(math.isfinite(value) for value in (*current, *entry))
    ):
        raise ValueError("formal position return poses must contain six finite values")
    if entry[2] <= current[2] or entry[2] - current[2] > 0.020:
        raise ValueError("formal position return must be bounded base +Z")
    if math.dist(entry[:2], current[:2]) > 0.002:
        raise ValueError("formal position return lateral delta exceeds 2 mm")
    values = ", ".join(f"{value:.17g}" for value in entry)
    return (
        "def tacdiffusion_formal_position_return_v1():\n"
        f"  movel(p[{values}], a=0.01, v=0.001, r=0.0)\n"
        "end\n"
    )


def _run_monitored_formal_position_return(
    *,
    args: argparse.Namespace,
    rtde: Any,
    output_recipe: int,
    output_types: Sequence[str],
    kunwei: Any,
    current_pose_base: Sequence[float],
    entry_pose_base: Sequence[float],
) -> dict[str, Any]:
    """Exit contact under the same Kunwei 50/4 and RTDE Safety owner."""

    source = _formal_position_return_source(
        current_pose_base=current_pose_base,
        entry_pose_base=entry_pose_base,
    )
    started = time.monotonic()
    deadline = started + 20.0
    saw_play = False
    final_pose = tuple(float(value) for value in current_pose_base)
    maximum_force_n = 0.0
    maximum_torque_nm = 0.0
    maximum_tcp_speed_m_s = 0.0
    stop_sent = False
    try:
        legacy._send_urscript(args.robot_host, source, args.connect_timeout_s)
        while time.monotonic() < deadline:
            guard = kunwei.snapshot(max_age_s=args.sensor_delivery_watchdog_s)
            maximum_force_n = max(maximum_force_n, float(guard.force_norm_n))
            maximum_torque_nm = max(maximum_torque_nm, float(guard.torque_norm_nm))
            batch = legacy._receive_available(
                rtde,
                output_recipe,
                output_types,
                legacy.OUTPUT_FIELDS,
                0.005,
            )
            for sample in batch:
                if (
                    int(sample["robot_mode"]) != legacy.ROBOT_MODE_RUNNING
                    or int(sample["safety_mode"]) != legacy.SAFETY_MODE_NORMAL
                ):
                    raise RuntimeError("formal_position_return_safety_changed")
                final_pose = tuple(float(value) for value in sample["actual_TCP_pose"])
                speed = tuple(float(value) for value in sample["actual_TCP_speed"])
                maximum_tcp_speed_m_s = max(
                    maximum_tcp_speed_m_s,
                    math.sqrt(sum(value * value for value in speed[:3])),
                )
                saw_play = saw_play or int(sample["runtime_state"]) == legacy.RUNTIME_PLAYING
            if (
                saw_play
                and batch
                and int(batch[-1]["runtime_state"]) == legacy.RUNTIME_STOPPED
            ):
                break
        else:
            raise RuntimeError("formal_position_return_deadline_exceeded")
        if math.dist(final_pose[:3], entry_pose_base[:3]) > 0.0005:
            raise RuntimeError("formal_position_return_target_not_reached")
    except Exception:
        try:
            legacy.dashboard_exchange(args.robot_host, ["stop"])
            stop_sent = True
        except Exception:
            pass
        raise
    return {
        "schema_version": "ur10e_tacdiffusion_formal_position_return/v1",
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "motion_primitive": "movel_base_positive_z_to_recorded_entry",
        "velocity_m_s": 0.001,
        "acceleration_m_s2": 0.01,
        "kunwei_guard_force_n": 50.0,
        "kunwei_guard_torque_nm": 4.0,
        "maximum_kunwei_force_n": maximum_force_n,
        "maximum_kunwei_torque_nm": maximum_torque_nm,
        "maximum_tcp_speed_m_s": maximum_tcp_speed_m_s,
        "final_pose_base": list(final_pose),
        "translation_error_m": math.dist(final_pose[:3], entry_pose_base[:3]),
        "saw_play": saw_play,
        "stop_sent": stop_sent,
        "safety_normal": True,
        "ur_internal_ft_used": False,
        "duration_s": time.monotonic() - started,
    }


def _entry_transition_evidence(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    transition_rows = [
        row
        for row in rows
        if int(row.get("receiver_state", -1)) in (legacy.STATE_STARTUP, legacy.STATE_TORQUE)
        and 1 <= int(round(float(row.get("control_update_count", 0.0)))) <= 25
    ]
    max_translation = 0.0
    max_rotation = 0.0
    max_joint_speed = 0.0
    max_joint_acceleration = 0.0
    previous: Mapping[str, Any] | None = None
    for row in transition_rows:
        tcp = tuple(float(row[f"actual_TCP_speed_{axis}"]) for axis in range(6))
        qd = tuple(float(row[f"actual_qd_{axis}"]) for axis in range(6))
        max_translation = max(max_translation, math.sqrt(sum(value * value for value in tcp[:3])))
        max_rotation = max(max_rotation, math.sqrt(sum(value * value for value in tcp[3:])))
        max_joint_speed = max(max_joint_speed, *(abs(value) for value in qd))
        if previous is not None:
            dt_s = float(row["controller_timestamp_s"]) - float(previous["controller_timestamp_s"])
            if dt_s > 0.0:
                max_joint_acceleration = max(
                    max_joint_acceleration,
                    *(
                        abs(qd[axis] - float(previous[f"actual_qd_{axis}"])) / dt_s
                        for axis in range(6)
                    ),
                )
        previous = row
    observed_counts = [
        int(round(float(row.get("control_update_count", 0.0)))) for row in transition_rows
    ]
    baseline_rows = [
        row
        for row in rows
        if int(row.get("receiver_state", -1)) in (legacy.STATE_STARTUP, legacy.STATE_TORQUE)
        and int(round(float(row.get("control_update_count", 0.0)))) >= 26
    ]
    return {
        "profile": "formal_contact_entry_transition_v1",
        "version": 1,
        "enabled_ticks": 25,
        "first_observed_control_update_count": min(observed_counts) if observed_counts else None,
        "last_observed_control_update_count": max(observed_counts) if observed_counts else None,
        "baseline_limits_observed_from_tick_26": bool(baseline_rows),
        "max_tcp_translation_speed_m_s": max_translation,
        "max_tcp_rotation_speed_rad_s": max_rotation,
        "max_abs_joint_speed_rad_s": max_joint_speed,
        "max_derived_abs_joint_acceleration_rad_s2": max_joint_acceleration,
        "hard_tcp_excursion_limit_m": 0.0003,
        "hard_joint_excursion_limit_rad": 0.0005,
    }


def _attempt_allows_automatic_continuation(result: Mapping[str, Any]) -> bool:
    """Only a counted episode at verified Home may release the next attempt."""

    return bool(
        result.get("outcome") == FormalAttemptOutcome.ELIGIBLE.value
        and result.get("task_ready_home") is True
        and result.get("auto_return_performed") is True
    )


def run_collect_campaign(args: argparse.Namespace) -> dict[str, Any]:
    _require_exact_sensor_delivery_watchdog(args)
    _require_frozen_live_endpoints(args)
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
            result = _run_contact_attempt_locked(
                args=args,
                ledger=ledger,
                identity=identity,
                dynamics_evidence=dynamics,
            )
            results.append(result)
            attempts_started += 1
            if not _attempt_allows_automatic_continuation(result):
                break
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


def _wait_for_qualification_stationary_preflight(
    robot_host: str, *, timeout_s: float = 2.0
) -> Mapping[str, Any]:
    """Retry only the transient stationarity rejection between families."""

    deadline = time.monotonic() + float(timeout_s)
    while True:
        status = legacy.readonly_status(robot_host)
        try:
            _validate_common_preflight(status)
        except RuntimeError as exc:
            if (
                str(exc) != "compile_probe_preflight_failed:robot_not_stationary"
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(0.05)
            continue
        return status


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
        expert_action_limits={
            "schema_version": FORMAL_EXPERT_ACTION_LIMITS_SCHEMA_V1,
            "frame_id": "tool0_tcp",
            "component_abs_max": list(FORMAL_EXPERT_ACTION_COMPONENT_ABS_MAX),
            "force_norm_max_n": FORMAL_EXPERT_ACTION_FORCE_NORM_MAX_N,
            "torque_norm_max_nm": FORMAL_EXPERT_ACTION_TORQUE_NORM_MAX_NM,
            "slew_per_s": list(FORMAL_EXPERT_ACTION_SLEW_PER_S),
        },
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
    raw_track_rows, selected_rows, row_selection = _select_formal_artifact_rows(
        rows
    )
    if len(raw_track_rows) < int(0.9 * 8.0 * 500.0):
        raise RuntimeError("formal_track_rows_insufficient")
    for row, _previous_runtime_row in selected_rows:
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
    control_clock, control_clock_evidence = _build_formal_control_clock(selected_rows)
    row_selection["control_clock"] = control_clock_evidence
    first_host = control_clock.host_anchor_s
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
        metadata=_formal_recorder_metadata(
            formal_manifest=formal_manifest,
            semantic_fingerprint_sha256=semantic_fingerprint_sha256,
            row_selection=row_selection,
        ),
    )
    composed_count = 0
    composition_sequence = 0
    recorder.start()
    try:
        for row, previous_runtime_row in selected_rows:
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
                sample_index=composition_sequence,
                control_sequence=composition_sequence,
                acked=acked,
                outgoing=acked,
                recorder_start_s=first_host,
                observation_history=observation_history,
                action_provider=None,
                desired_row=desired_row,
                kunwei_alignment_adapter=alignment,
                internal_wrench_provider=None,
                candidate_window=True,
                capture_phase="formal_track_state_torque",
                control_clock=control_clock,
            )
            previous_dynamics_sample = dynamics_runtime.previous_sample
            formal = composer.compose(
                base,
                previous_runtime_row=previous_runtime_row,
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
            if dynamics_runtime.previous_sample is not previous_dynamics_sample:
                composition_sequence += 1
            if formal is None:
                continue
            if not recorder.enqueue(formal):
                raise RuntimeError("formal_recorder_enqueue_failed")
            composed_count += 1
            # Post-run composition can enqueue while the fsync-10 sealer is
            # serializing a large V4 receipt batch.  Polling the 250 ms live
            # producer watchdog here misclassifies that active write as a
            # stall.  Enqueue remains bounded/fail-closed, and close(), the
            # cold-read validator, and final recorder health are authoritative.
        if alignment.fault is not None:
            raise RuntimeError(f"formal_causal_alignment_fault:{alignment.fault}")
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
        "row_selection": row_selection,
        "eligibility_window": {
            "formal_phase": FormalAttemptPhase.TRACK.value,
            "receiver_state": legacy.STATE_TORQUE,
            "acquisition_training": False,
        },
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
    status = _wait_for_qualification_stationary_preflight(args.robot_host)
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
    failure: str | None = None
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
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
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
            except Exception as abort_exc:
                failure += f"; abort_failed:{type(abort_exc).__name__}:{abort_exc}"
        finally:
            kunwei.stop()
            kunwei_summary = kunwei.summary()
    csv_path = output_dir / "direct_torque_rtde.csv"
    if rows:
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
        "ok": gate["ok"] and failure is None,
        "failure": failure,
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
        "data_csv": str(csv_path) if rows else None,
    }
    evidence_path = output_dir / "evidence.json"
    _write_json_new(evidence_path, evidence)
    return evidence


def run_qualify_seven(args: argparse.Namespace) -> dict[str, Any]:
    _require_exact_sensor_delivery_watchdog(args)
    _require_frozen_live_endpoints(args)
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
            result = _run_no_contact_episode(
                family=family,
                seed=args.seed + index,
                args=args,
                output_dir=root / f"{index:02d}_{family}",
            )
            results.append(result)
            if result.get("ok") is not True:
                break
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
    if not summary["ok"]:
        failed = results[-1] if results else {}
        raise RuntimeError(
            "formal_qualification_failed:"
            f"{failed.get('trajectory_family', 'none')}:"
            f"{failed.get('failure', 'strict_gate_failed')}:"
            f"{root / 'qualification_summary.json'}"
        )
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
    collect.add_argument(
        "--sensor-delivery-watchdog-s",
        type=_parse_exact_sensor_delivery_watchdog,
        default=FORMAL_SENSOR_DELIVERY_WATCHDOG_S,
    )
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
        "--sensor-delivery-watchdog-s",
        type=_parse_exact_sensor_delivery_watchdog,
        default=FORMAL_SENSOR_DELIVERY_WATCHDOG_S,
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
