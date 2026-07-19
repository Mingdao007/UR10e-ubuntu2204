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
)
from step5d_timing_acceptance import evaluate_step5d_v3_timing_raw


ROOT = Path(__file__).resolve().parents[1]
VALIDATION_RELATIVE = "config/step5/step5d_autotune_v3_offline_validation.json"
PROMOTION_RELATIVE = "config/step5/step5d_autotune_v3_live_promotion.json"
STAGE_TABLE_RELATIVE = "config/step5_stage_table.json"
STOPPING_RELATIVE = "config/step5/step5d_autotune_v3_stopping_bound_evidence.json"
RETURN_RELATIVE = "config/step5/step5d_autotune_v3_return_route_evidence.json"
URSIM_RETURN_RELATIVE = "config/step5/step5d_autotune_v3_ursim_return_trace.json"
TIMING_EQUIVALENCE_RELATIVE = (
    "config/step5/"
    "step5d_autotune_v3_formal_timing_raw_ede7bdb5.equivalence.json"
)
V1_STAGE_ID = "step5d_strict_rnn_autotune_v1"
V3_STAGE_ID = "step5d_strict_rnn_autotune_v3"
MACHINE_BINDING = "machine_generated_epoch_and_process_fingerprint"
PENDING_TIMING_BLOCKER = "requires_current_source_formal_500hz_timing"
ATTENDED_BLOCKERS = [
    "requires_attended_tp_upload_readback",
    "requires_current_poweroff_controller_identity",
    "requires_certification_motion_authorization",
    "requires_certified_stopping_bound",
    "requires_certified_return_route_angular_envelope",
    "requires_attended_sol_xhigh_pre_live_audit",
    "requires_fresh_campaign_authorization",
]
PRE_LIVE_DECISION = {
    "acceptance_scope": "offline_pre_live_only",
    "offline_implementation": "pass",
    "hardware_promotion": "blocked",
    "current_selector": V3_STAGE_ID,
    "v3_active": True,
    "robot_power_state": "POWER_OFF_AT_AUDIT_NOT_CURRENT_ASSERTION",
    "certification_motion_authorization_required": True,
    "campaign_authorization_required": True,
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
        equivalence_path = root / TIMING_EQUIVALENCE_RELATIVE
        equivalence = _read(equivalence_path)
        if (
            equivalence.get("schema")
            != "step5d.autotune-v3/timing-active-surface-equivalence-v1"
        ):
            raise ValueError("timing equivalence schema differs")
        layered_identity = active_identity_snapshot(experiment_root=root)
        subjects = equivalence.get("subject_identity") or {}
        tick = subjects.get("tick_semantics") or {}
        harness = subjects.get("timing_harness") or {}
        runtime = subjects.get("runtime_environment") or {}
        if (
            tick.get("target_layered_fingerprint")
            != layered_identity["tick_semantics_fingerprint"]
            or harness.get("target_layered_fingerprint")
            != layered_identity["timing_harness_fingerprint"]
            or not isinstance(runtime.get("fingerprint"), str)
            or len(runtime["fingerprint"]) != 64
        ):
            raise ValueError("timing equivalence target identity differs")
        reuse = {
            lane: (row or {}).get("reusable")
            for lane, row in (equivalence.get("lane_reuse") or {}).items()
        }
        if reuse != {"solver": True, "safe_hold": True, "full_tick": False}:
            raise ValueError("timing equivalence lane reuse differs")
        equivalence_ref = _reference(root, equivalence_path)
        seam = {
            "status": "pass_reused_safe_hold_active_surface_equivalence",
            "claim_class": "formal_lane_reuse_not_full_acceptance",
            "attempt_count": 0,
            "scheduler_policy_required": "SCHED_OTHER",
            "scheduler_priority_required": 0,
            "nice_required": 0,
            "blocker": "requires_final_source_exact_full_tick_30k",
            "equivalence_attestation": equivalence_ref,
            "retained_prior_attempt": {
                "path": (
                    "config/step5/"
                    "step5d_autotune_v3_sphere_seam_timing_c1c066f7.json"
                ),
                "sha256": _sha256(
                    root
                    / "config/step5/"
                    "step5d_autotune_v3_sphere_seam_timing_c1c066f7.json"
                ),
            },
        }
        formal = {
            "status": "blocked_final_full_tick_pending",
            "release_gate_satisfied": False,
            "attempt_count": 0,
            "acceptance_classification": "partial_lane_reuse_attested",
            "equivalence_attestation": equivalence_ref,
            "bound_identity": {
                "tick_semantics_fingerprint": layered_identity[
                    "tick_semantics_fingerprint"
                ],
                "timing_harness_fingerprint": layered_identity[
                    "timing_harness_fingerprint"
                ],
                "runtime_environment_fingerprint": runtime["fingerprint"],
            },
            "required_lanes": {
                "solver": {
                    "required_samples": 10_000,
                    "status": "pass_reused_active_surface_equivalence",
                },
                "full_tick_with_sphere": {
                    "required_samples": 30_000,
                    "status": "pending_final_source_exact_capture",
                },
                "safe_hold": {
                    "required_samples": 30_000,
                    "status": "pass_reused_active_surface_equivalence",
                },
            },
            "blocker": "requires_final_source_exact_full_tick_30k",
            "retained_prior_failed_attempt": {
                "classification": "diagnostic_only_not_v3_acceptance",
                "blockers": [
                    "base_formal_timing_not_accepted",
                    "v3_requires_production_sched_other_timing_contract",
                ],
                "raw_artifact": {
                    "path": (
                        "config/step5/"
                        "step5d_autotune_v3_formal_timing_raw_c1c066f7.json"
                    ),
                    "sha256": _sha256(
                        root
                        / "config/step5/"
                        "step5d_autotune_v3_formal_timing_raw_c1c066f7.json"
                    ),
                },
                "evaluation_artifact": {
                    "path": (
                        "config/step5/"
                        "step5d_autotune_v3_formal_timing_evaluation_c1c066f7.json"
                    ),
                    "sha256": _sha256(
                        root
                        / "config/step5/"
                        "step5d_autotune_v3_formal_timing_evaluation_c1c066f7.json"
                    ),
                },
            },
        }
        return seam, formal, False

    raw = raw_path.resolve(strict=True)
    evaluation = evaluation_path.resolve(strict=True)
    persisted = _read(evaluation)
    recomputed = evaluate_step5d_v3_timing_raw(root, raw)
    if persisted != recomputed or persisted.get("accepted") is not True:
        raise ValueError("formal timing evaluation is stale or not accepted")
    raw_payload = _read(raw)
    runtime = raw_payload.get("runtime_environment") or {}
    if (
        runtime.get("scheduler_policy_name") != "SCHED_OTHER"
        or runtime.get("scheduler_priority") != 0
        or runtime.get("nice") != 0
    ):
        raise ValueError("formal timing host contract is not SCHED_OTHER/0 NI=0")
    raw_ref = _reference(root, raw)
    evaluation_ref = _reference(root, evaluation)
    metadata_path = raw.with_suffix(".metadata.json")
    metadata = _read(metadata_path)
    if (
        metadata.get("formal") is not True
        or metadata.get("step5d_v3_moving_sphere") is not True
        or metadata.get("source_fingerprint_stable") is not True
        or metadata.get("harness_exit_code") != 0
    ):
        raise ValueError("formal timing execution metadata is not acceptance-grade")
    metadata_ref = _reference(root, metadata_path)
    lanes = {
        "solver": (raw_payload.get("solver") or {}).get("samples"),
        "full_tick_with_sphere": (raw_payload.get("full_tick") or {}).get(
            "samples"
        ),
        "safe_hold": (raw_payload.get("safe_hold") or {}).get("samples"),
    }
    if lanes != {
        "solver": 10_000,
        "full_tick_with_sphere": 30_000,
        "safe_hold": 30_000,
    }:
        raise ValueError("formal timing lane counts differ")
    current_attempt = {
        "raw_artifact": raw_ref,
        "evaluation_artifact": evaluation_ref,
        "execution_metadata": metadata_ref,
        "classification": persisted["classification"],
        "scheduler_policy": "SCHED_OTHER",
        "scheduler_priority": 0,
        "nice": 0,
        "cpu_affinity": runtime.get("cpu_affinity"),
    }
    seam = {
        "status": "pass_via_combined_formal_full_tick_with_sphere",
        "claim_class": "current_source_formal_timing_component",
        "attempt_count": 1,
        "scheduler_policy_required": "SCHED_OTHER",
        "scheduler_priority_required": 0,
        "nice_required": 0,
        "current_attempt": current_attempt,
    }
    formal = {
        "status": "pass_current_source_formal_500hz_timing",
        "release_gate_satisfied": True,
        "attempt_count": 1,
        "acceptance_classification": persisted["classification"],
        "required_lanes": {
            name: {"required_samples": count, "status": "pass"}
            for name, count in lanes.items()
        },
        "current_attempt": current_attempt,
    }
    return seam, formal, True


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
    validation["decision"] = PRE_LIVE_DECISION
    gates = validation["gates"]
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

    validation_bytes = (
        json.dumps(validation, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    validation_sha = hashlib.sha256(validation_bytes).hexdigest()
    blockers = ([] if timing_passed else [PENDING_TIMING_BLOCKER]) + ATTENDED_BLOCKERS
    public_signal = blockers[0]
    blocker_text = "_".join(blockers)

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
        "certification_motion_authorization_required": True,
        "campaign_authorization_required": True,
        "live_runtime_promoted": False,
        "blocker": blocker_text,
    }

    table = copy.deepcopy(_read(root / STAGE_TABLE_RELATIVE))
    rows = [row for row in table["stages"] if row.get("id") == V3_STAGE_ID]
    if len(rows) != 1:
        raise ValueError("V3 stage table row must exist exactly once")
    row = rows[0]
    row["block_reason"] = (
        "V3 is the unique selected/current release and remains pre-live blocked on "
        + ", ".join(blockers)
        + "; V1 is retained control-profile provenance only"
    )
    row["offline_validation"].update(
        {
            "report": VALIDATION_RELATIVE,
            "report_sha256": validation_sha,
            "formal_500hz_timing": formal_timing["status"],
            "source_exact_sphere_seam_timing": seam_timing["status"],
            "simulation": simulation["status"],
        }
    )
    row["execution_readiness"] = {
        "schema": "step5d.autotune-v3/execution-readiness-v3",
        "state": "pre_live_blocked",
        "public_success_signal": public_signal,
        "deterministic_validation_complete": True,
        "package_delivery_complete": False,
        "candidate_current": True,
        "live_runtime_promoted": False,
        "same_process_startup_gate_complete": False,
        "ready_to_execute": False,
        "ready_to_load_play": False,
        "ready_to_start_bridge": False,
        "ready_to_arm": False,
        "ready_for_contact_or_motion": False,
        "blockers": blockers,
        "next_owner": "ur10e-contact-control-prep",
        "operator_trigger": {
            "candidate_stage_id": V3_STAGE_ID,
            "user_confirmation_required": True,
            "certification_motion_authorization_required": True,
            "campaign_authorization_required": True,
            "internal_launch_binding": MACHINE_BINDING,
            "tp_action": "attended_upload_readback_required",
            "play_effect": "forbidden_in_offline_tranche",
        },
    }
    row["operator_lifecycle"]["live_readiness_state"] = public_signal
    return {
        root / STOPPING_RELATIVE: stopping,
        root / RETURN_RELATIVE: returned,
        validation_path: validation,
        root / PROMOTION_RELATIVE: promotion,
        root / STAGE_TABLE_RELATIVE: table,
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
