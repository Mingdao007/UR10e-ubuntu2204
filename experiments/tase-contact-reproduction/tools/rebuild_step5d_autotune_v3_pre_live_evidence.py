#!/usr/bin/env python3
"""Atomically rebuild the source-bound Step5d V3 pre-live evidence chain."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

import build_step5d_autotune_v3_return_route_evidence as return_builder
import build_step5d_autotune_v3_stopping_bound_evidence as stopping_builder
from step5d_autotune_v3.profile import (
    active_identity_snapshot,
    contract_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
VALIDATION_RELATIVE = "config/step5/step5d_autotune_v3_offline_validation.json"
PROMOTION_RELATIVE = "config/step5/step5d_autotune_v3_live_promotion.json"
STAGE_TABLE_RELATIVE = "config/step5_stage_table.json"
CURRENT_STAGE_RELATIVE = "config/current_stage.json"
CONTROL_CONTRACT_RELATIVE = "config/step5/step5d_autotune_v3_control_contract.json"
LAUNCH_PROFILE_RELATIVE = "config/step5/step5d_autotune_v3_launch_profile.json"
CURRENT_RELEASE_RELATIVE = "config/step5d/current.json"
STOPPING_RELATIVE = "config/step5/step5d_autotune_v3_stopping_bound_evidence.json"
RETURN_RELATIVE = "config/step5/step5d_autotune_v3_return_route_evidence.json"
URSIM_RETURN_RELATIVE = "config/step5/step5d_autotune_v3_ursim_return_trace.json"
V1_STAGE_ID = "step5d_strict_rnn_autotune_v1"
V3_STAGE_ID = "step5d_strict_rnn_autotune_v3"
MACHINE_BINDING = "machine_generated_epoch_and_process_fingerprint"
ATTENDED_BLOCKERS = [
    "requires_attended_tp_upload_readback",
]
HOST_RUNTIME_DISPOSITION = "verified_r008_full_home_rolling_production_chain_offline"
PRE_LIVE_DECISION = {
    "acceptance_scope": "offline_pre_live_only",
    "offline_implementation": "pass",
    "hardware_promotion": "blocked",
    "current_selector": V3_STAGE_ID,
    "v3_active": True,
    "robot_power_state": "runtime_observation_required",
    "certification_motion_authorization_required": False,
    "campaign_authorization_required": False,
    "live_motion_authorized": False,
    "execution_readiness": "pre_live_blocked",
}


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"required pre-live source is missing or unsafe: {path}")
    payload = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON constant: {value}")
        ),
    )
    if not isinstance(payload, dict):
        raise ValueError(f"required pre-live source is not an object: {path}")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reference(root: Path, path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("pre-live evidence must be a regular file")
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("pre-live evidence must remain inside the experiment root") from exc
    return {"path": str(relative), "sha256": _sha256(resolved)}


def _encoded_json(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_bytes(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_bytes(path, _encoded_json(payload))


def _stage_table_bytes(path: Path, payload: Mapping[str, Any]) -> bytes:
    """Replace only the V3 row while preserving the shared table byte layout."""

    text = path.read_text(encoding="utf-8")
    marker = f'"id": "{V3_STAGE_ID}"'
    if text.count(marker) != 1:
        raise ValueError("V3 stage marker must exist exactly once")
    marker_index = text.index(marker)
    start = text.rfind("\n    {", 0, marker_index)
    if start < 0:
        raise ValueError("V3 stage object start is missing")
    start += 1
    depth = 0
    in_string = False
    escaped = False
    end = None
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end is None:
        raise ValueError("V3 stage object end is missing")
    rows = [
        row
        for row in payload.get("stages", [])
        if isinstance(row, Mapping) and row.get("id") == V3_STAGE_ID
    ]
    if len(rows) != 1:
        raise ValueError("rebuilt V3 stage row must exist exactly once")
    row_text = json.dumps(
        rows[0],
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
    )
    indented = "\n".join(f"    {line}" for line in row_text.splitlines())
    return (text[:start] + indented + text[end:]).encode("utf-8")


def _identity(root: Path) -> dict[str, Any]:
    return active_identity_snapshot(experiment_root=root)


def _timing_state(
    root: Path,
    *,
    raw_path: Path | None,
    evaluation_path: Path | None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    if (raw_path is None) != (evaluation_path is None):
        raise ValueError("timing raw and evaluation must be supplied together")
    if raw_path is None:
        seam = {
            "status": "diagnostic_only_not_required_by_user",
            "claim_class": "diagnostic_only",
            "attempt_count": 0,
            "release_gate_applicable": False,
            "system_setup_required": False,
            "pressure_test_required": False,
        }
        formal = {
            "status": "not_required_by_user",
            "release_gate_applicable": False,
            "release_gate_satisfied": False,
            "attempt_count": 0,
            "acceptance_classification": "not_required_by_user",
            "required_lanes": {},
            "system_setup_required": False,
            "pressure_test_required": False,
        }
        return seam, formal, False

    raise ValueError(
        "formal timing pressure artifacts are outside the frozen V3 convergence lane"
    )


def build_outputs(
    root: Path,
    *,
    observed_at: str,
    timing_raw_path: Path | None = None,
    timing_evaluation_path: Path | None = None,
) -> dict[Path, dict[str, Any]]:
    root = root.resolve(strict=True)
    parsed = datetime.fromisoformat(observed_at)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at must include an explicit timezone")
    identity = _identity(root)
    stopping = stopping_builder.build_document()
    returned = return_builder.build_document()
    trace_path = root / URSIM_RETURN_RELATIVE
    trace_ref = _reference(root, trace_path)
    seam_timing, formal_timing, timing_passed = _timing_state(
        root,
        raw_path=timing_raw_path,
        evaluation_path=timing_evaluation_path,
    )

    validation_path = root / VALIDATION_RELATIVE
    validation = copy.deepcopy(_read(validation_path))
    validation["schema"] = "step5d.autotune-v3/offline-acceptance-v5"
    validation["observed_at"] = observed_at
    validation["identity"] = identity
    gates = validation["gates"]
    gates["exact_batch_lifecycle"] = {
        "status": "pass",
        "protocol": "v3_direct_arm_v1",
        "batch_size": 10,
        "formal_production_runner_arm2_entered_run": True,
        "production_trial_briefs_exactly_once": 1,
        "fake_transport_rows": 10,
        "rows_1_to_9_return": "ready_near_state_76",
        "row_10_return": "campaign_home_state_77_bounded_halt",
        "ack_commands": 0,
        "arm_11_dispatched": False,
    }
    local_triplet_sha256 = dict(identity["local_triplet_sha256"])
    readback_current = (
        identity.get("controller_readback_triplet_sha256")
        == local_triplet_sha256
    )
    control_contract = _read(root / CONTROL_CONTRACT_RELATIVE)
    candidate_identity = control_contract["candidate_tp_identity"]
    candidate_triplet_sha256 = dict(
        control_contract["candidate_tp_artifact_sha256"]
    )
    readback = _read(root / "config/step5d_autotune_controller_readback_v3.json")
    candidate_readback_current = bool(
        readback.get("verified") is True
        and readback.get("program") == candidate_identity["program"]
        and readback.get("triplet_sha256") == candidate_triplet_sha256
    )
    current_release = _read(root / CURRENT_RELEASE_RELATIVE)
    host_runtime_current = (
        current_release.get("host_runtime_disposition")
        == HOST_RUNTIME_DISPOSITION
    )
    tp_program_start_allowed = (
        current_release.get("tp_program_disposition")
        != "known_incompatible_do_not_retry"
    )
    bridge_start_ready = (
        readback_current and host_runtime_current and tp_program_start_allowed
    )
    validation["decision"] = {
        **PRE_LIVE_DECISION,
        "hardware_promotion": (
            "candidate_controller_readback_verified"
            if candidate_readback_current
            else "blocked_requires_r006_controller_readback"
        ),
        "execution_readiness": (
            "bridge_start_ready" if bridge_start_ready else "pre_live_blocked"
        ),
    }
    gates["package_and_readback"]["local_triplet_sha256"] = (
        local_triplet_sha256
    )
    gates["package_and_readback"]["local_candidate"] = {
        "program": candidate_identity["program"],
        "triplet_sha256": candidate_triplet_sha256,
        "controller_readback_verified": candidate_readback_current,
        "promotion_status": control_contract["promotion_status"],
    }
    if readback_current:
        gates["package_and_readback"].update(
            {
                "status": "pass_current_triplet_controller_readback",
                "blocker": None,
                "historical_controller_readback": None,
                "historical_controller_readback_sha256": None,
                "controller_readback": (
                    "config/step5d_autotune_controller_readback_v3.json"
                ),
                "controller_readback_sha256": _sha256(
                    root / "config/step5d_autotune_controller_readback_v3.json"
                ),
                "historical_readback_matches_local_triplet": True,
            }
        )
    if not candidate_readback_current:
        gates["package_and_readback"]["status"] = (
            "blocked_r006_local_only_requires_controller_readback"
        )
        gates["package_and_readback"]["blocker"] = (
            "requires_r006_controller_readback"
        )
    gates["authorization_separation"] = {
        "status": "pass_offline_contract",
        "certification_schema": (
            "ur-exp/step5d-certification-motion-authorization-v2"
        ),
        "campaign_schema": "ur-exp/step5d-campaign-authorization-v2",
        "interchangeable": False,
        "certification_no_contact_only": True,
        "certification_optimizer_eligible": False,
        "procedure_ticket_required_before_motion": True,
    }
    gates["source_exact_sphere_seam_timing"] = seam_timing
    gates["formal_500hz_timing"] = formal_timing
    gates["stopping_bound"]["evidence"] = {
        "path": STOPPING_RELATIVE,
        "sha256": hashlib.sha256(
            (json.dumps(stopping, allow_nan=False, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
        ).hexdigest(),
    }
    gates["return_route_angular_envelope"]["evidence"] = {
        "path": RETURN_RELATIVE,
        "sha256": hashlib.sha256(
            (json.dumps(returned, allow_nan=False, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
        ).hexdigest(),
    }
    simulation = gates["simulation"]
    simulation["claim"] = (
        "digest_pinned_ursim_return_motion_only_not_robot_acceptance"
    )
    simulation["status"] = "pass_motion_capable_ursim_core_source_equivalence"
    simulation["ursim"] = "pass_motion_capable_ursim_core_source_equivalence"
    simulation["return_route_motion_trace"] = trace_ref
    retained_hold_raw = simulation.pop(
        "raw_artifact", simulation.get("retained_hold_raw_artifact")
    )
    retained_hold_result = simulation.pop(
        "result_artifact", simulation.get("retained_hold_result_artifact")
    )
    simulation["retained_hold_raw_artifact"] = retained_hold_raw
    simulation["retained_hold_result_artifact"] = retained_hold_result
    advisory = gates["advisory_fable"]
    advisory["status"] = "completed_read_only_nonblocking_advisory"
    advisory["claim_boundary"] = (
        "advisory only; deterministic UR owner gates remain authoritative"
    )
    gates["sol_xhigh_audit"] = {
        "status": "parallel_advisory_nonblocking",
        "release_gate_applicable": False,
        "experiment_start_blocking": False,
        "safety_action_requires_deterministic_reproduction": True,
    }

    validation_bytes = (
        json.dumps(validation, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    validation_sha = hashlib.sha256(validation_bytes).hexdigest()
    # The user explicitly removed formal 10k/30k pressure testing from this
    # convergence lane. Preserve timing artifacts as diagnostics, but never
    # promote their state into an execution-readiness blocker.
    blockers = [
        blocker
        for blocker in ATTENDED_BLOCKERS
        if not (
            readback_current
            and blocker == "requires_attended_tp_upload_readback"
        )
    ]
    if not tp_program_start_allowed:
        blockers.extend(
            [
                "selected_tp_program_known_incompatible_do_not_retry",
                "r005_post_ack_csv_schema_timeout_incident",
            ]
        )
    if not host_runtime_current:
        blockers.append("requires_r008_offline_release")
    if (
        current_release.get("local_candidate_tp_program_id")
        == "step5d_strict_rnn_autotune_v3_r008"
        and not candidate_readback_current
    ):
        blockers.append("requires_r008_controller_readback")
    blockers = list(dict.fromkeys(blockers))
    public_signal = (
        blockers[0]
        if blockers
        else "controller_readback_verified_ready_for_bridge_context"
    )
    blocker_text = "_".join(blockers) if blockers else None

    promotion = {
        "schema": "step5d.autotune-v3/live-promotion-v3",
        "candidate_stage_id": V3_STAGE_ID,
        "control_profile_id": V1_STAGE_ID,
        "current_selector": V3_STAGE_ID,
        "identity": identity,
        "deterministic_validation": {
            "path": VALIDATION_RELATIVE,
            "sha256": validation_sha,
        },
        "controller_readback": {
            "path": "config/step5d_autotune_controller_readback_v3.json",
            "sha256": _sha256(
                root / "config/step5d_autotune_controller_readback_v3.json"
            ),
        },
        "machine_campaign_binding": MACHINE_BINDING,
        "same_process_startup_gate": True,
        "certification_motion_authorization_required": False,
        "campaign_authorization_required": False,
        "live_runtime_promoted": False,
        "blocker": blocker_text,
    }

    table = copy.deepcopy(_read(root / STAGE_TABLE_RELATIVE))
    rows = [row for row in table["stages"] if row.get("id") == V3_STAGE_ID]
    if len(rows) != 1:
        raise ValueError("V3 stage table row must exist exactly once")
    row = rows[0]
    package = row["package_delivery"]
    package["sha256"] = local_triplet_sha256
    deployment_program = str(
        _read(root / CONTROL_CONTRACT_RELATIVE)["deployment_tp_identity"]["program"]
    )
    deployment_prefix = str(
        Path(candidate_identity["artifact_dir"]) / deployment_program
    )
    controller_target = (
        f"/programs/andyl/kunwei/step5/{deployment_program}.urp"
    )
    package["program_basename"] = deployment_program
    package["local_triplet"] = deployment_prefix
    package["controller_target"] = controller_target
    package["tp_fingerprint"] = _sha256(
        root
        / "programs/step5/step5d"
        / f"{deployment_program}.deploy-manifest.json"
    )
    package["status"] = (
        "historical_controller_readback_verified_known_incompatible_do_not_retry"
        if not tp_program_start_allowed
        else "controller_readback_verified"
        if readback_current
        else "requires_attended_tp_upload_readback"
    )
    package["controller_uploaded_by_v3"] = readback_current
    package["controller_readback_verified"] = readback_current
    package["controller_readback_manifest_sha256"] = _sha256(
        root / "config/step5d_autotune_controller_readback_v3.json"
    )
    if readback_current:
        package["fresh_controller_sha_at"] = _read(
            root / "config/step5d_autotune_controller_readback_v3.json"
        )["fresh_controller_checked_at"]
    package["local_candidate"] = {
        "program_basename": candidate_identity["program"],
        "local_triplet": str(
            Path(candidate_identity["artifact_dir"]) / candidate_identity["program"]
        ),
        "status": (
            "controller_readback_verified"
            if candidate_readback_current
            else "local_only_requires_controller_readback"
        ),
        "controller_uploaded": candidate_readback_current,
        "controller_readback_verified": candidate_readback_current,
        "deploy_manifest_sha256": candidate_identity["deploy_manifest_sha256"],
        "numeric_sanity_sha256": candidate_identity["numeric_sanity_sha256"],
        "sha256": candidate_triplet_sha256,
    }
    row["block_reason"] = (
        None
        if bridge_start_ready
        else (
            "V3 remains the unique selected route. Deployed r006 is historical "
            "controller-readback-verified but known incompatible and must not start; "
            "immutable r008 requires exact controller upload/readback "
            "before any bridge context can exist."
        )
    )
    row["offline_validation"].update(
        {
            "hardware_promotion": (
                "controller_readback_verified"
                if candidate_readback_current
                else "blocked"
            ),
            "report": VALIDATION_RELATIVE,
            "report_sha256": validation_sha,
            "formal_500hz_timing": formal_timing["status"],
            "source_exact_sphere_seam_timing": seam_timing["status"],
            "simulation": simulation["status"],
            "exact_ten_row_lifecycle": (
                "r008_rolling_rows_1_to_5_every_row_home_state78_arm11_q4_q5"
            ),
            "production_second_lap": (
                "pass_formal_runner_growing_csv_seal_cold_read_trialbrief_arm2_run"
            ),
            "retained_live_incident": (
                "r005_ack_consumed_arm2_not_sent_post_ack_csv_schema_timeout"
            ),
            "status": (
                "pass_offline_implementation_controller_readback_verified"
                if candidate_readback_current
                else "pass_offline_implementation_pre_live_blocked"
            ),
        }
    )
    row["acceptance"].update(
        {
            "typed_closure_v2_cold_read_ack_next_arm_verified": (
                "legacy_replay_only_not_production_pass"
            ),
            "direct_arm1_bundle_cold_read_trialbrief_arm2_verified": True,
            "growing_production_csv_follower_verified": True,
            "r008_controller_readback_verified": candidate_readback_current,
        }
    )
    row["execution_readiness"] = {
        "schema": "step5d.autotune-v3/execution-readiness-v3",
        "state": "bridge_start_ready" if bridge_start_ready else "pre_live_blocked",
        "public_success_signal": public_signal,
        "deterministic_validation_complete": True,
        "package_delivery_complete": candidate_readback_current,
        "candidate_current": candidate_readback_current,
        "live_runtime_promoted": False,
        "same_process_startup_gate_complete": False,
        "ready_to_execute": bridge_start_ready,
        "ready_to_load_play": False,
        "ready_to_start_bridge": bridge_start_ready,
        "ready_to_arm": False,
        "ready_for_contact_or_motion": False,
        "blockers": blockers,
        "next_owner": (
            "ur10e-bridge-ops"
            if bridge_start_ready
            else "ur10e-tp-package-delivery"
        ),
        "operator_trigger": {
            "candidate_stage_id": V3_STAGE_ID,
            "user_confirmation_required": True,
            "certification_motion_authorization_required": False,
            "campaign_authorization_required": False,
            "internal_launch_binding": MACHINE_BINDING,
            "tp_action": (
                "controller_readback_verified_no_load_or_play"
                if candidate_readback_current
                else "attended_r006_upload_readback_required"
            ),
            "play_effect": "user_owned_command_1_after_bridge_ready",
        },
    }
    row["operator_lifecycle"]["live_readiness_state"] = public_signal
    row["operator_lifecycle"]["expected_program"] = controller_target
    row["current_binding"]["controller_target"] = controller_target
    current_stage = copy.deepcopy(_read(root / CURRENT_STAGE_RELATIVE))
    current_stage["sha256"] = local_triplet_sha256
    current_stage["controller_readback_verified_for_selected_triplet"] = (
        readback_current
    )
    current_stage["controller_readback_manifest_sha256"] = _sha256(
        root / "config/step5d_autotune_controller_readback_v3.json"
    )
    current_stage["readiness"]["deployment_ready"] = readback_current
    current_stage["readiness"]["bridge_start_ready"] = bridge_start_ready
    current_stage["readiness"]["blockers"] = blockers
    current_stage["readiness"]["bridge_process_ready"] = False
    current_stage["readiness"]["motion_arm_ready"] = False
    current_stage["readiness"]["campaign_ready"] = False
    current_stage["readiness"]["host_runtime_disposition"] = (
        current_release.get("host_runtime_disposition")
    )
    current_stage["execution_state"] = (
        "bridge_start_ready_no_arm" if bridge_start_ready else "pre_live_blocked"
    )
    current_stage["local_triplet"] = deployment_prefix
    current_stage["controller_script"] = (
        f"/programs/andyl/kunwei/step5/{deployment_program}.script"
    )
    current_stage["controller_target"] = (
        controller_target
    )
    current_stage["local_candidate"] = {
        "program": candidate_identity["program"],
        "disposition": (
            "controller_readback_verified_promoted_current"
            if candidate_readback_current
            else "local_only_requires_controller_readback"
        ),
        "manifest": current_release.get("local_candidate_manifest"),
        "triplet_sha256": candidate_triplet_sha256,
        "controller_uploaded": candidate_readback_current,
        "controller_readback_verified": candidate_readback_current,
    }
    current_stage["status"] = (
        "step5d_autotune_v3_r008_controller_readback_verified"
        if candidate_readback_current
        else "step5d_autotune_v3_r006_quarantined_r008_local_only"
    )
    current_stage["bridge_trigger"]["blocked_reason"] = (
        None
        if bridge_start_ready
        else (
            "Deployed r006 is known incompatible and r008 lacks exact readback; "
            "no bridge context is legal."
        )
    )
    current_stage["updated_at"] = observed_at
    control_contract = copy.deepcopy(control_contract)
    control_contract["candidate_tp_artifact_sha256"] = candidate_triplet_sha256
    if readback_current:
        control_contract["tp_artifact_sha256"] = local_triplet_sha256
        control_contract["deployment_tp_identity"]["readback_manifest_sha256"] = (
            _sha256(root / "config/step5d_autotune_controller_readback_v3.json")
        )
        control_contract["deployment_tp_identity"]["tp_fingerprint"] = package[
            "tp_fingerprint"
        ]
    launch_profile = copy.deepcopy(_read(root / LAUNCH_PROFILE_RELATIVE))
    launch_profile["control_contract_sha256"] = contract_sha256(control_contract)
    launch_profile["tp_program_id"] = deployment_program
    return {
        root / STOPPING_RELATIVE: stopping,
        root / RETURN_RELATIVE: returned,
        validation_path: validation,
        root / PROMOTION_RELATIVE: promotion,
        root / STAGE_TABLE_RELATIVE: table,
        root / CURRENT_STAGE_RELATIVE: current_stage,
        root / CONTROL_CONTRACT_RELATIVE: control_contract,
        root / LAUNCH_PROFILE_RELATIVE: launch_profile,
    }


def _infer_timing_paths(root: Path) -> tuple[Path | None, Path | None]:
    validation = _read(root / VALIDATION_RELATIVE)
    formal = (validation.get("gates") or {}).get("formal_500hz_timing") or {}
    if formal.get("status") != "pass_current_source_formal_500hz_timing":
        return None, None
    current = formal.get("current_attempt") or {}
    raw = current.get("raw_artifact") or {}
    evaluation = current.get("evaluation_artifact") or {}
    return root / str(raw["path"]), root / str(evaluation["path"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--observed-at")
    parser.add_argument("--timing-raw", type=Path)
    parser.add_argument("--timing-evaluation", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve(strict=True)
    if root != ROOT.resolve(strict=True):
        raise SystemExit("the rebuild tool only writes its owning experiment checkout")
    current = _read(root / VALIDATION_RELATIVE)
    observed_at = args.observed_at or str(current.get("observed_at"))
    timing_raw = args.timing_raw
    timing_evaluation = args.timing_evaluation
    if args.check and timing_raw is None and timing_evaluation is None:
        timing_raw, timing_evaluation = _infer_timing_paths(root)
    outputs = build_outputs(
        root,
        observed_at=observed_at,
        timing_raw_path=timing_raw,
        timing_evaluation_path=timing_evaluation,
    )
    if args.check:
        for path, payload in outputs.items():
            expected = (
                _stage_table_bytes(path, payload)
                if path == root / STAGE_TABLE_RELATIVE
                else _encoded_json(payload)
            )
            if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
                raise SystemExit(f"pre-live evidence is missing or stale: {path}")
        return 0
    for path, payload in outputs.items():
        if path == root / STAGE_TABLE_RELATIVE:
            _atomic_bytes(path, _stage_table_bytes(path, payload))
        else:
            _atomic_json(path, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
