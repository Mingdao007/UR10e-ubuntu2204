#!/usr/bin/env python3
"""Build the canonical, fail-closed v30 offline-readiness artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from step5d_v30_timing import SOURCE_BINDING_FILES, summarize_preaggregated
from step5d_review_v2 import full_review_index_projection_sha256
from validate_step5d_review_v2 import validate_manifest, validate_packet


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "config" / "step5d_v30_offline_readiness.json"
STATUS_READY = "v30_offline_ready"
STATUS_BLOCKED = "v30_offline_blocked"
V30_PROFILE = "step5d_strict_rnn_ablation_v30"
P0_V8_PROFILE = "step5d_strict_rnn_no_contact_p0_v8"
EXTENSIONS = (".script", ".txt", ".urp")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def stage_row(stage_id: str) -> dict[str, Any]:
    table = load(ROOT / "config" / "step5_stage_table.json")
    row = next(
        (item for item in table.get("stages", []) if item.get("id") == stage_id),
        None,
    )
    if not isinstance(row, dict):
        raise ValueError(f"missing Step5 stage row: {stage_id}")
    return row


def validate_inactive_package_delivery(
    row: dict[str, Any], marker: dict[str, Any]
) -> dict[str, Any]:
    """Bind the inactive triplet and, when present, its controller readback."""

    blockers: list[str] = []
    delivery = row.get("package_delivery") or {}
    marker_sha = marker.get("sha256") or {}
    expected_sha = {
        ext: sha256(ROOT / "programs" / "step5" / "step5d" / f"{V30_PROFILE}{ext}")
        for ext in EXTENSIONS
    }
    if marker.get("program") != V30_PROFILE or marker.get("local_only") is not True:
        blockers.append("v30_local_package_boundary_invalid")
    if marker_sha != expected_sha or delivery.get("sha256") != expected_sha:
        blockers.append("v30_package_hash_binding_invalid")
    if marker.get("semantic_fingerprint") != delivery.get("semantic_fingerprint"):
        blockers.append("v30_semantic_fingerprint_mismatch")

    readback_verified = delivery.get("controller_readback_verified") is True
    manifest_rel = delivery.get("controller_readback_manifest")
    manifest_sha256 = None
    if readback_verified:
        if delivery.get("controller_uploaded") is not True:
            blockers.append("v30_readback_without_upload_state")
        if not manifest_rel:
            blockers.append("v30_controller_readback_manifest_missing")
        else:
            manifest_path = ROOT / str(manifest_rel)
            if not manifest_path.is_file():
                blockers.append("v30_controller_readback_manifest_unavailable")
            else:
                manifest_sha256 = sha256(manifest_path)
                manifest = load(manifest_path)
                manifest_sha = manifest.get("sha256") or {}
                validation = manifest.get("validation") or {}
                target = delivery.get("controller_target")
                expected_target = f"/programs/andyl/kunwei/step5/{V30_PROFILE}.urp"
                if (
                    manifest.get("status") != "controller read-back verified"
                    or validation.get("program") != V30_PROFILE
                    or target != expected_target
                    or validation.get("script_node_path")
                    != f"/programs/andyl/kunwei/step5/{V30_PROFILE}.script"
                    or manifest_sha.get("local") != expected_sha
                    or manifest_sha.get("controller") != expected_sha
                    or manifest_sha.get("readback") != expected_sha
                ):
                    blockers.append("v30_controller_readback_binding_invalid")
        if delivery.get("status") not in {
            "controller_readback_verified_inactive",
            "inactive_controller_readback_verified",
        }:
            blockers.append("v30_controller_readback_status_invalid")
    elif manifest_rel or delivery.get("controller_uploaded") is True:
        blockers.append("v30_partial_delivery_state_inconsistent")

    return {
        "valid": not blockers,
        "blockers": sorted(set(blockers)),
        "triplet_sha256": expected_sha,
        "controller_readback_verified": readback_verified and not blockers,
        "controller_readback_manifest": manifest_rel,
        "controller_readback_manifest_sha256": manifest_sha256,
    }


def validate_p0_v8_gate(current: dict[str, Any]) -> dict[str, Any]:
    candidate = current.get("p0_v8_candidate") or {}
    artifact_rel = candidate.get("passed_artifact")
    blockers: list[str] = []
    if candidate.get("profile") != P0_V8_PROFILE:
        blockers.append("p0_v8_profile_mismatch")
    if candidate.get("p0_v8_passed") is not True:
        blockers.append("p0_v8_final_60s_not_passed")
    artifact_sha256 = None
    if not artifact_rel:
        blockers.append("p0_v8_passed_artifact_missing")
    else:
        artifact_path = ROOT / str(artifact_rel)
        if not artifact_path.is_file():
            blockers.append("p0_v8_passed_artifact_unavailable")
        else:
            artifact_sha256 = sha256(artifact_path)
            artifact = load(artifact_path)
            if (
                artifact.get("p0_v8_passed") is not True
                or float(artifact.get("phase_s", 0.0) or 0.0) != 60.0
                or (artifact.get("binding") or {}).get("composite_fingerprint")
                != candidate.get("composite_fingerprint")
            ):
                blockers.append("p0_v8_passed_artifact_binding_invalid")
    return {
        "profile": candidate.get("profile"),
        "passed": not blockers,
        "passed_artifact": artifact_rel,
        "passed_artifact_sha256": artifact_sha256,
        "composite_fingerprint": candidate.get("composite_fingerprint"),
        "blockers": sorted(set(blockers)),
    }


def review_v2_gate(row: dict[str, Any]) -> dict[str, Any]:
    gate = row.get("review_v2") or {}
    packet_rel = gate.get("packet")
    manifest_rel = gate.get("manifest")
    result = {
        "policy_id": "ur10e_review_policy_v2",
        "required_stack": "2+1",
        "status": str(gate.get("status") or "not_due"),
        "evidence_frozen": gate.get("evidence_frozen") is True,
        "composite_fingerprint": gate.get("composite_fingerprint"),
        "packet": packet_rel,
        "manifest": manifest_rel,
        "accepted": False,
        "blockers": [],
    }
    if not packet_rel or not manifest_rel:
        result["blockers"].append("review_v2_packet_or_manifest_missing")
        return result
    packet_path = ROOT / str(packet_rel)
    manifest_path = ROOT / str(manifest_rel)
    if not packet_path.is_file() or not manifest_path.is_file():
        result["blockers"].append("review_v2_packet_or_manifest_unavailable")
        return result
    packet = load(packet_path)
    manifest = load(manifest_path)
    packet_result = validate_packet(packet, root=ROOT)
    manifest_result = validate_manifest(
        manifest,
        packet,
        root=ROOT,
        manifest_sha256=sha256(manifest_path),
    )
    result.update(
        {
            "packet_sha256": sha256(packet_path),
            "manifest_sha256": sha256(manifest_path),
            "packet_validation": packet_result,
            "manifest_validation": manifest_result,
        }
    )
    fingerprint = (packet.get("fingerprints") or {}).get("composite")
    if fingerprint != gate.get("composite_fingerprint"):
        result["blockers"].append("review_v2_composite_fingerprint_stale")
    result["blockers"].extend(packet_result.get("issues", []))
    result["blockers"].extend(manifest_result.get("blockers", []))
    result["blockers"] = sorted(set(result["blockers"]))
    if result["evidence_frozen"] is not True:
        result["blockers"].append("review_v2_manifest_without_evidence_freeze")
    if gate.get("status") != "accepted":
        result["blockers"].append("review_v2_stage_status_not_accepted")
    result["blockers"] = sorted(set(result["blockers"]))
    result["accepted"] = not result["blockers"] and manifest_result.get("accepted") is True
    result["status"] = "accepted" if result["accepted"] else "blocked"
    return result


def timing_history_entry(
    path: Path,
    *,
    role: str,
    expected_source_binding: dict[str, str],
    expected_replay_sha256: str,
    expected_paper_truth_sha256: str,
    expected_profile_selection_sha256: str | None = None,
) -> dict[str, Any]:
    payload = load(path)
    solver = payload.get("solver") or {}
    full_tick = payload.get("full_tick") or {}
    safe_hold = payload.get("safe_hold") or {}
    solver_misses = int(solver.get("compute_deadline_miss_count", 0) or 0)
    evaluation = summarize_preaggregated(
        payload,
        expected_source_binding=expected_source_binding,
        expected_replay_sha256=expected_replay_sha256,
        expected_paper_truth_sha256=expected_paper_truth_sha256,
    )
    maximum = solver.get("max_ms")
    hard_solver_failure = bool(
        solver_misses
        or (
            isinstance(maximum, (int, float))
            and float(maximum) >= 2.0
        )
    )
    acceptance_eligible = evaluation.get("acceptance_eligible") is True
    selection_binding = (
        (payload.get("artifact_binding") or {}).get("profile_selection") or {}
    )
    selection_binding_matches_current = (
        expected_profile_selection_sha256 is None
        or selection_binding.get("sha256") == expected_profile_selection_sha256
    )
    if not selection_binding_matches_current:
        acceptance_eligible = False
        evaluation.setdefault("blockers", []).append(
            "remote_timing_profile_selection_sha_mismatch"
        )
    classification = (
        "failed_hard_solver_deadline"
        if hard_solver_failure
        else (
            "acceptance_eligible"
            if acceptance_eligible
            else "diagnostic_only_not_acceptance"
        )
    )
    raw_sha256 = sha256(path)
    raw_profile = payload.get("profile")
    profile = raw_profile if isinstance(raw_profile, dict) else {}
    inner_iterations = profile.get("inner_iterations")
    evidence_status = (
        "historical_superseded_by_v30_rnn512_profile_selection"
        if not isinstance(raw_profile, dict) or inner_iterations not in {256, 512}
        else "diagnostic_not_selected_by_v30_rnn512_profile_selection"
        if inner_iterations == 256
        else "diagnostic_selection_evidence_not_formal_timing"
        if (payload.get("profile_selection") or {}).get(
            "diagnostic_override_requested"
        )
        is True
        else "current_canonical_profile_candidate"
    )
    return {
        "role": role,
        "path": str(path.relative_to(ROOT)),
        "sha256": raw_sha256,
        "profile": raw_profile,
        "profile_selection": payload.get("profile_selection"),
        "evidence_status": evidence_status,
        "source_binding": payload.get("source_binding"),
        "artifact_binding": payload.get("artifact_binding"),
        "solver": solver,
        "full_tick": full_tick,
        "safe_hold": safe_hold,
        "paced_500hz": payload.get("paced_500hz"),
        "elapsed_full_tick_wall_s": payload.get("elapsed_full_tick_wall_s"),
        "runtime_path": payload.get("runtime_path"),
        "component_diagnostics": payload.get("cupy_component_diagnostics"),
        "classification": classification,
        "acceptance_eligible": acceptance_eligible,
        "profile_selection_binding_matches_current": (
            selection_binding_matches_current
        ),
        "acceptance_evaluation": {
            "recomputed_from_single_hash_bound_raw_artifact": True,
            "raw_sha256": raw_sha256,
            "blockers": evaluation.get("blockers", []),
            "first_post_warm_ms": evaluation.get("first_post_warm_ms"),
            "unmeasured_pipeline_warmup": evaluation.get(
                "unmeasured_pipeline_warmup"
            ),
            "pipeline_warmup_contract_proven": evaluation.get(
                "pipeline_warmup_contract_proven"
            ),
            "solver": evaluation.get("solver"),
            "solver_batch_reentry": evaluation.get("solver_batch_reentry"),
            "solver_batch_reentry_evidence": evaluation.get(
                "solver_batch_reentry_evidence"
            ),
            "full_tick": evaluation.get("full_tick"),
            "safe_hold": evaluation.get("safe_hold"),
            "full_tick_schedule_deadline_miss_count": evaluation.get(
                "full_tick_schedule_deadline_miss_count"
            ),
            "safe_hold_schedule_deadline_miss_count": evaluation.get(
                "safe_hold_schedule_deadline_miss_count"
            ),
            "elapsed_full_tick_wall_s": evaluation.get(
                "elapsed_full_tick_wall_s"
            ),
            "elapsed_safe_hold_wall_s": evaluation.get(
                "elapsed_safe_hold_wall_s"
            ),
            "pacing_provenance": evaluation.get("pacing_provenance"),
            "deadline_robustness": evaluation.get("deadline_robustness"),
        },
    }


def build(*, generated_at: str) -> dict[str, Any]:
    current = load(ROOT / "config" / "current_stage.json")
    v30_row = stage_row("step5d_strict_rnn_ablation_v30")
    p0_v8 = validate_p0_v8_gate(current)
    replay_path = ROOT / "config" / "step5d_v30_replay_summary.json"
    replay = load(replay_path)
    timing_summary_path = ROOT / "config" / "step5d_v30_timing_summary.json"
    timing_summary = load(timing_summary_path)
    profile_selection_path = (
        ROOT / "config" / "step5d_v30_profile_selection.json"
    )
    profile_selection = load(profile_selection_path)
    marker_path = (
        ROOT
        / "programs"
        / "step5"
        / "step5d"
        / ".step5d_strict_rnn_ablation_v30.local_candidate.json"
    )
    marker = load(marker_path)
    expected_source_binding = {
        field: sha256(ROOT / relative)
        for field, relative in SOURCE_BINDING_FILES.items()
    }
    source_hashes = {
        field: {
            "path": relative,
            "sha256": expected_source_binding[field],
        }
        for field, relative in SOURCE_BINDING_FILES.items()
    }
    remote_evidence = load(
        ROOT / "config" / "step5d_v29_remote_evidence_sha256.json"
    )
    expected_replay_sha256 = remote_evidence["sha256"]["bridge_rtde_500hz.csv"]
    expected_paper_truth_sha256 = sha256(
        ROOT / "config" / "step5d_liveprep_solver_gate.json"
    )
    history_paths = [
        (ROOT / "config" / "step5d_v30_timing_raw.json", "preoptimization_full_attempt"),
        (
            ROOT / "config" / "step5d_v30_optimized_solver_10k_raw.json",
            "six_lane_optimized_solver_10k_with_20_tick_smoke",
        ),
        (
            ROOT / "config" / "step5d_v30_current_source_solver_10k_raw.json",
            "current_source_solver_10k_with_20_tick_runtime_smoke",
        ),
    ]
    for pattern, role in (
        ("step5d_v30_runtime*_smoke.json", "runtime_shaped_smoke_not_acceptance"),
        ("step5d_v30_component_diagnostic*.json", "component_diagnostic_not_acceptance"),
        (
            "step5d_v30_rnn*_component_diagnostic_raw.json",
            "parameter_profile_component_diagnostic_not_acceptance",
        ),
        (
            "step5d_v30_rnn*_formal_timing_raw.json",
            "parameter_selected_formal_timing_candidate",
        ),
        (
            "step5d_v30_rnn*_profile_sweep_raw.json",
            "parameter_profile_selection_diagnostic_not_acceptance",
        ),
    ):
        history_paths.extend((path, role) for path in sorted((ROOT / "config").glob(pattern)))
    history = [
        timing_history_entry(
            path,
            role=role,
            expected_source_binding=expected_source_binding,
            expected_replay_sha256=expected_replay_sha256,
            expected_paper_truth_sha256=expected_paper_truth_sha256,
            expected_profile_selection_sha256=sha256(profile_selection_path),
        )
        for path, role in history_paths
        if path.is_file()
    ]

    legacy_review_path = ROOT / "config" / "step5d_v30_milestone_reviews.json"
    review_policy_path = ROOT / "config" / "step5d_review_policy_v2.json"
    review_index_path = ROOT / "config" / "step5d_review_index_v2.json"
    review_index = load(review_index_path)
    current_review = review_v2_gate(v30_row)
    v30_script_path = (
        ROOT / "programs" / "step5" / "step5d" / f"{V30_PROFILE}.script"
    )
    p0_script_path = (
        ROOT / "programs" / "step5" / "step5d" / f"{P0_V8_PROFILE}.script"
    )
    v30_script = v30_script_path.read_text(encoding="utf-8")
    p0_script = p0_script_path.read_text(encoding="utf-8")
    common_deadline_tokens = (
            "DEADLINE_OVERRUN_LAST_COMMAND_HOLD",
            "if not heartbeat_fresh:",
            "local stage25_have_accepted_command = False",
    )
    deadline_overrun_static_prepared = (
        all(token in v30_script for token in common_deadline_tokens)
        and "if stale_s2 > 0.020:" in v30_script
        and all(token in p0_script for token in common_deadline_tokens)
        and "if stale_s2 > 0.250:" in p0_script
    )
    blockers: list[str] = []
    if (
        profile_selection.get("selected_inner_iterations") != 512
        or (profile_selection.get("selected_profile") or {}).get("epsilon")
        != 0.010
        or (profile_selection.get("selected_profile") or {}).get(
            "sigr_exponent_r"
        )
        != 0.8
        or (profile_selection.get("selected_profile") or {}).get(
            "qdot_cap_rad_s"
        )
        != 0.05
        or (profile_selection.get("selected_profile") or {}).get("backend")
        != "cupy"
    ):
        blockers.append("v30_rnn512_profile_selection_invalid")
    if replay.get("acceptance_pass") is not True:
        blockers.append("v29_replay_acceptance_incomplete")
    hard_acceptance_entries = [
        entry
        for entry in history
        if entry["acceptance_eligible"]
        and (
            entry["acceptance_evaluation"].get("deadline_robustness") or {}
        ).get("hard_realtime_pass")
        is True
    ]
    bounded_hold_acceptance_entries = [
        entry
        for entry in history
        if entry["acceptance_eligible"]
        and (
            entry["acceptance_evaluation"].get("deadline_robustness") or {}
        ).get("bounded_last_command_hold_pass")
        is True
    ]
    acceptance_entries = (
        hard_acceptance_entries + bounded_hold_acceptance_entries
    )
    timing_acceptance_mode = (
        "hard_realtime"
        if hard_acceptance_entries
        else "bounded_last_command_hold"
        if bounded_hold_acceptance_entries
        else "none"
    )
    if not acceptance_entries:
        blockers.append("timing_acceptance_failed")
    current_source = next(
        (
            entry
            for entry in reversed(history)
            if int(entry["solver"].get("samples", 0) or 0) >= 10_000
            and isinstance(entry.get("source_binding"), dict)
            and all(
                entry["source_binding"].get(field) == expected
                for field, expected in expected_source_binding.items()
            )
        ),
        None,
    )
    if current_source is None or int(current_source["solver"].get("samples", 0) or 0) < 10_000:
        blockers.append("current_source_solver_10k_not_run")
    elif not (
        current_source["acceptance_eligible"]
        or (
            current_source["acceptance_evaluation"].get("deadline_robustness")
            or {}
        ).get("bounded_last_command_hold_pass")
        is True
    ):
        blockers.append(
            "current_source_solver_10k_ran_but_60s_paced_runtime_shaped_not_run"
        )
        current_source_blockers = {
            str(value)
            for value in current_source["acceptance_evaluation"]["blockers"]
        }
        if any(value.startswith("safe_hold_") for value in current_source_blockers):
            blockers.append("runtime_shaped_safe_hold_acceptance_not_run")
    if not acceptance_entries:
        blockers.append("runtime_shaped_60s_500hz_acceptance_not_run")
    if not deadline_overrun_static_prepared:
        blockers.append("deadline_overrun_tp_last_command_hold_not_prepared")
    offline_prewarm = (
        (v30_row.get("runtime_scheduler") or {}).get(
            "offline_pipeline_prewarm"
        )
        or {}
    )
    live_runtime_prewarm_verified = bool(
        isinstance(offline_prewarm, dict)
        and offline_prewarm.get("live_runtime_integration_verified") is True
    )
    if not live_runtime_prewarm_verified:
        blockers.append("live_runtime_prewarm_not_integrated_or_verified")
    hard_deadline_evidence = current_source or next(
        (entry for entry in history if entry["role"].startswith("six_lane_optimized")),
        None,
    )
    if hard_deadline_evidence and int(
        hard_deadline_evidence["solver"].get("compute_deadline_miss_count", 0) or 0
    ):
        blockers.append("system_level_host_driver_timing_outliers_unresolved")
    delivery = v30_row.get("package_delivery") or {}
    package_validation = validate_inactive_package_delivery(v30_row, marker)
    readback_verified = package_validation["controller_readback_verified"] is True
    blockers.extend(package_validation["blockers"])
    if not readback_verified:
        blockers.append("v30_controller_readback_not_frozen")
    numeric_row = v30_row.get("numeric_sanity") or {}
    numeric_path_rel = str(numeric_row.get("artifact") or "")
    numeric_path = ROOT / numeric_path_rel
    numeric_payload = load(numeric_path) if numeric_path.is_file() else {}
    numeric_sanity = {
        "path": numeric_path_rel,
        "sha256": sha256(numeric_path) if numeric_path.is_file() else None,
        "source_full_chain_artifact": numeric_row.get(
            "source_full_chain_artifact"
        ),
        "source_full_chain_sha256": numeric_row.get("source_full_chain_sha256"),
        "exact_profile_bound": (numeric_payload.get("gates") or {}).get(
            "exact_profile_bound"
        ),
        "package_hashes_bound": (numeric_payload.get("gates") or {}).get(
            "package_hashes_bound"
        ),
        "overall_pass": numeric_payload.get("overall_pass"),
        "claim_effect": numeric_row.get("claim_effect"),
    }
    if (
        not numeric_path.is_file()
        or numeric_sanity["sha256"] != numeric_row.get("sha256")
        or numeric_payload.get("stage_id") != V30_PROFILE
        or numeric_sanity["exact_profile_bound"] is not True
        or numeric_sanity["package_hashes_bound"] is not True
        or numeric_sanity["overall_pass"] is not True
    ):
        blockers.append("v30_numeric_sanity_binding_invalid")
    blockers.extend(p0_v8["blockers"])
    deterministic_blockers = sorted(set(blockers))
    evidence_frozen = not deterministic_blockers
    if not evidence_frozen:
        blockers.append("review_v2_not_due_until_evidence_freeze")
    elif not current_review["accepted"]:
        blockers.append("review_v2_2_plus_1_not_accepted_for_current_fingerprint")
    blockers = sorted(set(blockers))
    status = STATUS_READY if not blockers else STATUS_BLOCKED

    return {
        "schema_version": "step5d_v30_offline_readiness_v2",
        "generated_at": generated_at,
        "status": status,
        "profile": {
            "backend": "cupy",
            "inner_iterations": 512,
            "epsilon": 0.010,
            "sigr_exponent_r": 0.8,
            "qdot_cap_rad_s": 0.05,
            "control_hz": 500.0,
            "hard_deadline_ms": 2.0,
        },
        "current_pointer": {
            "program": current.get("program"),
            "v30_is_current": current.get("program")
            == "step5d_strict_rnn_ablation_v30",
        },
        "authorization": {
            "live_motion_authorized": False,
            "controller_upload_authorized": False,
            "bridge_start_authorized": False,
            "tp_play_authorized": False,
            "delivery_preparation_allowed": True,
        },
        "replay": {
            "path": str(replay_path.relative_to(ROOT)),
            "sha256": sha256(replay_path),
            "acceptance_pass": replay.get("acceptance_pass"),
            "accepted_ratio": replay.get("accepted_ratio"),
            "normal_mismatch_rows": replay.get("normal_mismatch_rows"),
            "nonfinite_rows": replay.get("nonfinite_rows"),
            "qdot_over_bound_rows": replay.get("qdot_over_bound_rows"),
        },
        "timing": {
            "profile_selection": {
                "path": str(profile_selection_path.relative_to(ROOT)),
                "sha256": sha256(profile_selection_path),
                "selected_inner_iterations": profile_selection.get(
                    "selected_inner_iterations"
                ),
                "classification": profile_selection.get("classification"),
                "formal_timing_satisfied": bool(acceptance_entries),
            },
            "summary_path": str(timing_summary_path.relative_to(ROOT)),
            "summary_sha256": sha256(timing_summary_path),
            "overall_pass": bool(acceptance_entries),
            "acceptance_mode": timing_acceptance_mode,
            "hard_realtime_pass": bool(hard_acceptance_entries),
            "bounded_last_command_hold_pass": bool(
                bounded_hold_acceptance_entries
            ),
            "current_source_evidence": (
                {
                    "path": current_source["path"],
                    "sha256": current_source["sha256"],
                    "acceptance_eligible": current_source["acceptance_eligible"],
                    "bounded_last_command_hold_pass": (
                        current_source["acceptance_evaluation"].get(
                            "deadline_robustness"
                        )
                        or {}
                    ).get("bounded_last_command_hold_pass")
                    is True,
                }
                if current_source is not None
                else None
            ),
            "acceptance_decision_source": (
                "per-artifact recomputation from one hash-bound raw artifact; "
                "the aggregate summary is diagnostic only"
            ),
            "acceptance_raw_evidence": (
                {
                    "path": acceptance_entries[0]["path"],
                    "sha256": acceptance_entries[0]["sha256"],
                }
                if acceptance_entries
                else None
            ),
            "diagnostic_summary_reported_overall_pass": timing_summary.get(
                "overall_pass"
            ),
            "diagnostic_summary_blockers": timing_summary.get("blockers", []),
            "history": history,
            "diagnostic_conclusion": (
                "Timing acceptance is recomputed from versioned raw artifacts bound to "
                "the selected profile and current sources. Historical attempts remain "
                "diagnostic and cannot satisfy the current gate."
            ),
        },
        "control_pipeline": {
            "path": (
                "Step5dObservation -> SlewCompatibleReference -> "
                "StrictRnnControlPolicy -> ControlCandidate -> "
                "step5d_v30_contract_pipeline -> DLS shadow -> SafetyEnvelope -> "
                "RegisterCommand -> DeferredV30Diagnostics"
            ),
            "dls_shadow_runtime_fallback_allowed": False,
            "dls_shadow_command_inert_test": "tests/test_step5d_v30_control_contract.py",
            "normal_contract": "n_reaction = -n_approach in one canonical command frame",
        },
        "runtime_prewarm": {
            "offline_timing_contract": offline_prewarm,
            "offline_timing_contract_proven": bool(
                acceptance_entries
                and (
                    acceptance_entries[0]["acceptance_evaluation"].get(
                        "pipeline_warmup_contract_proven"
                    )
                    is True
                )
            ),
            "live_runtime_integration_verified": live_runtime_prewarm_verified,
            "claim_boundary": (
                "offline no-output prewarm evidence does not prove that the "
                "future live bridge performs the same prewarm before authorization"
            ),
        },
        "deadline_overrun_policy": {
            "hard_realtime_claim_requires_zero_deadline_miss": True,
            "bounded_hold_candidate_ratio_max": 0.01,
            "bounded_hold_lateness_max_ms": 1.5,
            "bounded_hold_max_consecutive_misses": 10,
            "late_candidate_publish_policy": "discard_without_publish",
            "p0_v8_late_candidate_publish_policy": "publish_when_guard_approved",
            "tp_stale_tick_policy": (
                "same_heartbeat_last_published_guard_approved_qdot_consumed"
            ),
            "pre_first_command_policy": "invalid_packet_tp_sync_no_speed_command",
            "intentional_stop_policy": "zero_qdot_stop_dominates_hold",
            "continuous_stale_stop_s": 0.020,
            "p0_v8_continuous_stale_stop_s": 0.250,
            "next_fresh_tick_may_recover": True,
            "package_static_prepared": deadline_overrun_static_prepared,
            "v30_script": {
                "path": str(v30_script_path.relative_to(ROOT)),
                "sha256": sha256(v30_script_path),
            },
            "p0_v8_script": {
                "path": str(p0_script_path.relative_to(ROOT)),
                "sha256": sha256(p0_script_path),
            },
            "controller_or_ursim_execution_verified": False,
            "source_bound_bridge_hold_fault_injection_verified": bool(
                bounded_hold_acceptance_entries
            ),
            "bounded_last_command_hold_claim_allowed": bool(
                bounded_hold_acceptance_entries
            ),
            "claim_boundary": (
                "offline package/static preparation only; no controller timing or motion proof"
            ),
        },
        "package": {
            "marker_path": str(marker_path.relative_to(ROOT)),
            "marker_sha256": sha256(marker_path),
            "local_only": marker.get("local_only"),
            "not_delivered": marker.get("not_delivered"),
            "controller_readback_verified": readback_verified,
            "controller_readback_manifest": delivery.get(
                "controller_readback_manifest"
            ),
            "controller_readback_manifest_sha256": package_validation[
                "controller_readback_manifest_sha256"
            ],
            "binding_valid": package_validation["valid"],
            "binding_blockers": package_validation["blockers"],
            "delivery_preparation_allowed_before_p0_v8": True,
            "semantic_fingerprint": marker.get("semantic_fingerprint"),
            "triplet_sha256": package_validation["triplet_sha256"],
        },
        "numeric_sanity": numeric_sanity,
        "source_contract": source_hashes,
        "p0_v8_gate": p0_v8,
        "review_v2": {
            **current_review,
            "evidence_freeze_ready": evidence_frozen,
            "deterministic_freeze_blockers": deterministic_blockers,
            "policy_path": str(review_policy_path.relative_to(ROOT)),
            "policy_sha256": sha256(review_policy_path),
            "index_path": str(review_index_path.relative_to(ROOT)),
            "index_sha256": full_review_index_projection_sha256(review_index),
        },
        "historical_review": {
            "path": str(legacy_review_path.relative_to(ROOT)),
            "sha256": sha256(legacy_review_path),
            "status": "historical_superseded_by_review_policy_v2",
            "counts_as_review_v2": False,
        },
        "blockers": blockers,
        "claim_boundary": {
            "package_accepted": False,
            "live_accepted": False,
            "reproduction_complete": False,
            "allowed_claim": "inactive v30 offline candidate with partial replay/timing evidence",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--generated-at", default=datetime.now().isoformat(timespec="seconds"))
    args = parser.parse_args()
    payload = build(generated_at=args.generated_at)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "blockers": payload["blockers"]}))
    return 0 if payload["status"] == STATUS_READY else 3


if __name__ == "__main__":
    raise SystemExit(main())
