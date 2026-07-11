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


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "config" / "step5d_v30_offline_readiness.json"
STATUS_READY = "v30_offline_ready"
STATUS_BLOCKED = "v30_offline_blocked"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def timing_history_entry(
    path: Path,
    *,
    role: str,
    expected_source_binding: dict[str, str],
    expected_replay_sha256: str,
    expected_paper_truth_sha256: str,
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
    return {
        "role": role,
        "path": str(path.relative_to(ROOT)),
        "sha256": raw_sha256,
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
        "acceptance_evaluation": {
            "recomputed_from_single_hash_bound_raw_artifact": True,
            "raw_sha256": raw_sha256,
            "blockers": evaluation.get("blockers", []),
            "first_post_warm_ms": evaluation.get("first_post_warm_ms"),
            "solver": evaluation.get("solver"),
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
        },
    }


def build(*, generated_at: str) -> dict[str, Any]:
    current = load(ROOT / "config" / "current_stage.json")
    replay_path = ROOT / "config" / "step5d_v30_replay_summary.json"
    replay = load(replay_path)
    timing_summary_path = ROOT / "config" / "step5d_v30_timing_summary.json"
    timing_summary = load(timing_summary_path)
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
    ):
        history_paths.extend((path, role) for path in sorted((ROOT / "config").glob(pattern)))
    history = [
        timing_history_entry(
            path,
            role=role,
            expected_source_binding=expected_source_binding,
            expected_replay_sha256=expected_replay_sha256,
            expected_paper_truth_sha256=expected_paper_truth_sha256,
        )
        for path, role in history_paths
        if path.is_file()
    ]

    review_manifest_path = ROOT / "config" / "step5d_v30_milestone_reviews.json"
    review_manifest = load(review_manifest_path) if review_manifest_path.is_file() else {}
    reviews = review_manifest.get("reviewers") or {
        "codex_control_claim_high": {"status": "pending"},
        "codex_timing_runtime_high": {"status": "pending"},
        "fable5_physical_operator_safety_high": {"status": "pending"},
    }
    blockers: list[str] = []
    if replay.get("acceptance_pass") is not True:
        blockers.append("v29_replay_acceptance_incomplete")
    acceptance_entries = [entry for entry in history if entry["acceptance_eligible"]]
    if not acceptance_entries:
        blockers.append("timing_acceptance_failed")
    current_source = next(
        (entry for entry in history if entry["role"].startswith("current_source_solver_10k")),
        None,
    )
    if current_source is None or int(current_source["solver"].get("samples", 0) or 0) < 10_000:
        blockers.append("current_source_solver_10k_not_run")
    elif not current_source["acceptance_eligible"]:
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
    hard_deadline_evidence = current_source or next(
        (entry for entry in history if entry["role"].startswith("six_lane_optimized")),
        None,
    )
    if hard_deadline_evidence and int(
        hard_deadline_evidence["solver"].get("compute_deadline_miss_count", 0) or 0
    ):
        blockers.append("system_level_host_driver_timing_outliers_unresolved")
    if marker.get("local_only") is not True or marker.get("not_delivered") is not True:
        blockers.append("v30_local_package_boundary_invalid")
    if any(review.get("status") != "pass" for review in reviews.values()):
        blockers.append("milestone_reviews_incomplete_or_blocking")
    blockers = sorted(set(blockers))
    status = STATUS_READY if not blockers else STATUS_BLOCKED

    return {
        "schema_version": "step5d_v30_offline_readiness_v1",
        "generated_at": generated_at,
        "status": status,
        "profile": {
            "backend": "cupy",
            "inner_iterations": 1024,
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
            "summary_path": str(timing_summary_path.relative_to(ROOT)),
            "summary_sha256": sha256(timing_summary_path),
            "overall_pass": bool(acceptance_entries),
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
                "The current-source 10,000-solve run keeps low steady p99, but uncensored "
                ">=2 ms host/driver completion outliers remain; no 60 s paced "
                "runtime-shaped pass exists."
            ),
        },
        "control_pipeline": {
            "path": (
                "Step5dObservation -> StrictRnnControlPolicy -> ControlCandidate -> "
                "step5d_v30_contract_pipeline -> DLS shadow -> SafetyEnvelope -> "
                "RegisterCommand -> DeferredV30Diagnostics"
            ),
            "dls_shadow_runtime_fallback_allowed": False,
            "dls_shadow_command_inert_test": "tests/test_step5d_v30_control_contract.py",
            "normal_contract": "n_reaction = -n_approach in one canonical command frame",
        },
        "package": {
            "marker_path": str(marker_path.relative_to(ROOT)),
            "marker_sha256": sha256(marker_path),
            "local_only": marker.get("local_only"),
            "not_delivered": marker.get("not_delivered"),
            "semantic_fingerprint": marker.get("semantic_fingerprint"),
            "triplet_sha256": marker.get("sha256"),
        },
        "source_contract": source_hashes,
        "reviews": reviews,
        "review_manifest": {
            "path": str(review_manifest_path.relative_to(ROOT)),
            "sha256": sha256(review_manifest_path),
            "milestone_status": review_manifest.get("milestone_gate", {}).get(
                "status", "missing"
            ),
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
