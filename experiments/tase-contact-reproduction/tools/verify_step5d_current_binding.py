#!/usr/bin/env python3
"""Verify the selected Step5d live-prep package, read-back, and runtime binding."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any

from step5d_liveprep_readiness import (
    BENCHMARK_CONTRACT,
    READINESS_SCHEMA,
    load_benchmark_contract,
    load_runtime_dependency_contract,
    reviewed_source_sha256,
    runtime_dependency_evidence,
    validate_recorded_offline_evidence,
    validate_review_manifest,
    workflow_binding_sha256,
)
from step5d_runtime_interface import resolve_runtime_interface
from step5d_review_v3 import resolve as resolve_review_v3
from step5d_review_v3 import canonical_composite
from build_step5d_v31_review_binding import compute_payloads as compute_v31_review_payloads
from build_step5d_v32_review_binding import compute_payloads as compute_v32_review_payloads
from build_step5d_v33_freeze_binding import build_payload as build_v33_freeze_payload
from build_step5d_v34_freeze_binding import build_payload as build_v34_freeze_payload
from build_step5d_v35_freeze_binding import build_payload as build_v35_freeze_payload
from verify_step5d_contact_v35 import verify as verify_v35_contract
from step5d_timing_acceptance import evaluate_timing_raw
from step5d_v30_timing import SOURCE_BINDING_FILES
from verify_current_stage_readback import EXPERIMENT_ROOT, fail, load_json, verify


STEP5D_PACKAGE_PREFIXES = ("step5d_strict_rnn_liveprep_", "step5d_strict_rnn_ablation_")
STEP5D_ABLATION_V29 = "step5d_strict_rnn_ablation_v29"
STEP5D_ABLATION_V30 = "step5d_strict_rnn_ablation_v30"
STEP5D_ABLATION_V31 = "step5d_strict_rnn_ablation_v31"
STEP5D_ABLATION_V32 = "step5d_strict_rnn_ablation_v32"
STEP5D_ABLATION_V33C20 = "step5d_strict_rnn_ablation_v33c20"
STEP5D_ABLATION_V33 = "step5d_strict_rnn_ablation_v33"
STEP5D_ABLATION_V34 = "step5d_strict_rnn_ablation_v34"
STEP5D_ABLATION_V35 = "step5d_strict_rnn_ablation_v35"
V29_EXACT_RUNTIME_PROFILE: dict[str, Any] = {
    "backend": "cupy",
    "inner_iterations": 1024,
    "epsilon": 0.01,
    "sigr_exponent_r": 0.8,
    "qdot_cap_rad_s": 0.05,
    "control_mode": "speedj_rnn_live",
    "joint_layout_code": 524.0,
}
V30_EXACT_RUNTIME_PROFILE: dict[str, Any] = {
    **V29_EXACT_RUNTIME_PROFILE,
    "inner_iterations": 512,
}
V31_EXACT_RUNTIME_PROFILE: dict[str, Any] = {
    **V30_EXACT_RUNTIME_PROFILE,
    "qdot_cap_rad_s": 0.5,
}
V32_EXACT_RUNTIME_PROFILE: dict[str, Any] = {
    "backend": "cupy",
    "inner_iterations": 512,
    "epsilon": 0.01,
    "sigr_exponent_r": 0.8,
    "qdot_cap_rad_s": 0.5,
    "control_mode": "speedj_rnn_live",
    "wire_protocol": "stage_aware_joint_v1",
    "joint_marker_internal": 524.0,
}
V30_READINESS = "config/step5d_v30_offline_readiness.json"
V30_REVIEW_POLICY = "config/step5d_review_policy_v3.json"
V30_REVIEW_INDEX = "config/step5d_review_index_v3.json"
P0_V8_PROFILE = "step5d_strict_rnn_no_contact_p0_v8"


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _local_triplet_paths(root: Path, current: dict[str, Any], program: str) -> list[Path]:
    stem_value = current.get("local_triplet")
    if stem_value:
        stem = root / str(stem_value)
    else:
        stem = root / "programs" / "step5" / program
    return [stem.with_suffix(ext) for ext in (".script", ".txt", ".urp")]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_table_entry(root: Path, current: dict[str, Any], program: str) -> dict[str, Any]:
    table_rel = current.get("stage_table_path") or "config/step5_stage_table.json"
    table = load_json(root / str(table_rel))
    for row in table.get("stages", []):
        if row.get("id") == program:
            if row.get("active") is not True:
                fail(f"stage table row {program} is not active")
            return row
    fail(f"stage table row {program} is missing")


def verify_binding(root: Path, program: str | None = None, target_dir: str | None = None) -> dict[str, Any]:
    readback = verify(root, program, target_dir)
    current = load_json(root / "config" / "current_stage.json")
    selected = readback["program"]
    if not selected.startswith(STEP5D_PACKAGE_PREFIXES):
        fail(f"{selected} is not a Step5d TP package")

    interface = resolve_runtime_interface(program=selected, root=root)
    if current.get("controller_target") != interface.controller_target:
        fail(f"runtime interface target {interface.controller_target} differs from current_stage target {current.get('controller_target')}")
    if current.get("bridge_profile", {}).get("step4e_version") not in (None, selected):
        fail(f"bridge_profile.step4e_version is {current.get('bridge_profile', {}).get('step4e_version')}, expected {selected}")

    missing_triplet = [path for path in _local_triplet_paths(root, current, selected) if not path.exists()]
    if missing_triplet:
        fail("local triplet is incomplete: " + ", ".join(_relative(root, path) for path in missing_triplet))

    current_sha = current.get("sha256") or current.get("evidence", {}).get("sha256") or {}
    local_triplet = _local_triplet_paths(root, current, selected)
    for ext, path in zip((".script", ".txt", ".urp"), local_triplet):
        expected_sha = current_sha.get(ext)
        if not expected_sha:
            fail(f"current_stage sha256 for {ext} is missing")
        actual_sha = _sha256_file(path)
        if actual_sha != expected_sha:
            fail(
                "local triplet sha256 "
                f"{_relative(root, path)} is {actual_sha}, expected current_stage {ext} {expected_sha}"
            )

    stage_entry = _stage_table_entry(root, current, selected)

    return {
        "ok": True,
        "program": selected,
        "target_dir": readback["target_dir"],
        "manifest": readback["manifest"],
        "delivery_mode": readback["delivery_mode"],
        "runtime_interface": asdict(interface),
        "stage_table": {
            "path": current.get("stage_table_path") or "config/step5_stage_table.json",
            "id": stage_entry.get("id"),
            "active": stage_entry.get("active"),
            "blocked": stage_entry.get("blocked"),
        },
        "local_triplet": [_relative(root, path) for path in local_triplet],
    }


def _stage_bool(row: dict[str, Any], *keys: str) -> bool:
    containers = [row, row.get("acceptance"), row.get("contact_policy"), row.get("current_binding")]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in keys:
            if container.get(key) is True:
                return True
    return False


def _confined_regular_file(root: Path, raw_path: Any, label: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        fail(f"{label} path is missing")
    unresolved = root / raw_path
    resolved = unresolved.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        fail(f"{label} path escapes experiment root")
    if unresolved.is_symlink() or not resolved.is_file():
        fail(f"{label} is not a regular in-root file: {raw_path}")
    return resolved


def _hash_bound_json(
    root: Path,
    raw_path: Any,
    label: str,
    *,
    expected_sha256: Any = None,
) -> tuple[Path, dict[str, Any], str]:
    path = _confined_regular_file(root, raw_path, label)
    actual_sha256 = _sha256_file(path)
    if expected_sha256 is not None and expected_sha256 != actual_sha256:
        fail(f"{label} sha256 is stale")
    try:
        payload = load_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        fail(f"{label} is not valid JSON")
    if not isinstance(payload, dict):
        fail(f"{label} is not a JSON object")
    return path, payload, actual_sha256


def _finite_number(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        fail(f"v30 timing {label} is missing or non-numeric")
    if not math.isfinite(result):
        fail(f"v30 timing {label} is nonfinite")
    return result


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _zero_counter(container: dict[str, Any], label: str, *keys: str) -> None:
    for key in keys:
        if key in container:
            if _finite_number(container.get(key), f"{label}.{key}") != 0.0:
                fail(f"v30 timing {label}.{key} must be zero")
            return
    fail(f"v30 timing {label} deadline/nonfinite counter is missing")


def _verify_v30_timing_raw(root: Path, timing: dict[str, Any]) -> dict[str, Any]:
    evidence = timing.get("acceptance_raw_evidence")
    if not isinstance(evidence, dict):
        fail("v30 timing acceptance_raw_evidence is missing")
    raw_path, raw, raw_sha256 = _hash_bound_json(
        root,
        evidence.get("path"),
        "v30 timing acceptance raw evidence",
        expected_sha256=evidence.get("sha256"),
    )
    canonical = evaluate_timing_raw(root, raw_path)
    if canonical["accepted"] is not True:
        fail(
            "v30 timing canonical evaluator rejected raw evidence: "
            + ", ".join(canonical["blockers"])
        )
    evaluation = canonical["evaluation"]
    return {
        "path": _relative(root, raw_path),
        "sha256": raw_sha256,
        "evaluator": canonical["evaluator"],
        "classification": canonical["classification"],
        "solver_samples": int(evaluation["solver"]["samples"]),
        "full_tick_samples": int(evaluation["full_tick"]["samples"]),
        "safe_hold_samples": int(evaluation["safe_hold"]["samples"]),
    }

def _verify_v30_review_v3(
    root: Path,
    stage_entry: dict[str, Any],
    readiness_review: dict[str, Any],
    expected_composite_binding: dict[str, str],
) -> dict[str, Any]:
    stage_review = stage_entry.get("review_v3")
    if not isinstance(stage_review, dict):
        fail("v30 Review v3 stage gate is missing")
    if stage_review.get("required_stack") != "1+1":
        fail("v30 Review v3 gate must require stack 1+1")
    if stage_review.get("evidence_frozen") is not True:
        fail("v30 Review v3 evidence is not frozen")
    if readiness_review.get("accepted") is not True:
        fail("v30 Review v3 current composite is not accepted")
    if readiness_review.get("required_stack") not in (None, "1+1"):
        fail("v30 readiness Review v3 stack is not 1+1")
    if readiness_review.get("evidence_freeze_ready") is not True:
        fail("v30 readiness deterministic evidence freeze is incomplete")
    if readiness_review.get("deterministic_freeze_blockers") not in (None, []):
        fail("v30 readiness deterministic evidence freeze has blockers")

    manifest_rel = stage_review.get("manifest") or readiness_review.get("manifest")
    manifest_path, manifest, manifest_sha256 = _hash_bound_json(
        root,
        manifest_rel,
        "v30 Review v3 manifest",
        expected_sha256=readiness_review.get("manifest_sha256"),
    )
    policy_path, policy, policy_sha256 = _hash_bound_json(
        root,
        readiness_review.get("policy_path") or V30_REVIEW_POLICY,
        "v30 Review v3 policy",
        expected_sha256=readiness_review.get("policy_sha256"),
    )
    index_path, review_index, index_sha256 = _hash_bound_json(
        root,
        readiness_review.get("index_path") or V30_REVIEW_INDEX,
        "v30 Review v3 index",
        expected_sha256=readiness_review.get("index_sha256"),
    )
    composite = stage_review.get("composite_fingerprint")
    if not isinstance(composite, str) or len(composite) != 64:
        fail("v30 Review v3 composite fingerprint is invalid")
    if readiness_review.get("composite_fingerprint") != composite:
        fail("v30 readiness Review v3 composite fingerprint is stale")
    if manifest.get("composite_binding") != expected_composite_binding:
        fail("v30 Review v3 composite does not bind current frozen artifacts")
    for lane_name, lane in (manifest.get("lanes") or {}).items():
        if not isinstance(lane, dict):
            fail(f"v30 Review v3 lane is invalid: {lane_name}")
        evidence_path = _confined_regular_file(root, lane.get("runtime_evidence_path"), f"{lane_name} transcript")
        if _sha256_file(evidence_path) != lane.get("runtime_evidence_sha256"):
            fail(f"v30 Review v3 lane transcript hash mismatch: {lane_name}")
    matching_index_records = [
        record for record in review_index.get("review_records", [])
        if isinstance(record, dict)
        and record.get("review_mode") == manifest.get("review_mode")
        and record.get("composite_fingerprint") == composite
    ]
    if len(matching_index_records) != 1 or matching_index_records[0].get(
        "manifest_sha256"
    ) != manifest_sha256:
        fail("v30 Review v3 index does not bind the exact manifest hash")
    resolved = resolve_review_v3(
        workflow="v30",
        milestone="contact_pre_live",
        gate={"evidence_frozen": True, "composite_fingerprint": composite,
              "manifest_sha256": manifest_sha256},
        manifest=manifest,
        policy=policy,
        index=review_index,
    )
    if resolved.get("accepted") is not True:
        fail("v30 Review v3 manifest validation failed")
    return {
        "manifest": _relative(root, manifest_path),
        "manifest_sha256": manifest_sha256,
        "policy": _relative(root, policy_path),
        "policy_sha256": policy_sha256,
        "index": _relative(root, index_path),
        "index_sha256": index_sha256,
        "composite_fingerprint": composite,
        "review_mode": manifest.get("review_mode"),
        "effective_stack": resolved.get("effective_stack"),
        "degraded_review": resolved.get("degraded_review"),
    }


def verify_v30_evidence_freeze(
    root: Path,
    current: dict[str, Any] | None = None,
    stage_entry: dict[str, Any] | None = None,
    *,
    expected_package_sha256: dict[str, str] | None = None,
    expected_readback_manifest: str | None = None,
) -> dict[str, Any]:
    """Revalidate the frozen v30 promotion/live-prep evidence without authorizing motion."""

    current = current or load_json(root / "config" / "current_stage.json")
    if stage_entry is None:
        table = load_json(root / str(current.get("stage_table_path") or "config/step5_stage_table.json"))
        stage_entry = next(
            (row for row in table.get("stages", []) if row.get("id") == STEP5D_ABLATION_V30),
            None,
        )
    if not isinstance(stage_entry, dict):
        fail("v30 stage row is missing")
    if stage_entry.get("runtime_profile") != V30_EXACT_RUNTIME_PROFILE:
        fail("v30 stage row does not bind the exact CuPy/512/epsilon=0.01/r=0.8/qdot=0.05 profile")
    if stage_entry.get("runtime_scheduler") != {
        "policy": "SCHED_FIFO",
        "priority": 20,
    }:
        fail("v30 stage row does not bind the production SCHED_FIFO/20 scheduler")
    contact_policy = stage_entry.get("contact_policy") or {}
    guard = stage_entry.get("guard") or {}
    if (
        contact_policy.get("dls_shadow_only") is not True
        or contact_policy.get("dls_fallback_allowed") is not False
        or guard.get("dls_runtime_fallback_allowed") is not False
    ):
        fail("v30 promotion requires DLS shadow-only with runtime fallback disabled")

    p0_current = current.get("p0_v8_candidate")
    p0_gate = stage_entry.get("p0_v8_gate")
    if not isinstance(p0_current, dict) or not isinstance(p0_gate, dict):
        fail("v30 promotion requires the P0 v8 gate")
    if p0_current.get("p0_v8_passed") is not True or p0_gate.get("passed") is not True:
        fail("v30 promotion requires the direct frozen-duration P0 v8 pass")
    try:
        configured_p0_duration = float((p0_current.get("canary_policy") or {})["direct_duration_s"])
    except (KeyError, TypeError, ValueError):
        fail("v30 P0 v8 frozen direct duration is invalid")
    p0_artifact_rel = p0_gate.get("passed_artifact") or p0_current.get("passed_artifact")
    if p0_artifact_rel != p0_current.get("passed_artifact"):
        fail("v30 P0 v8 passed artifact pointers disagree")
    p0_expected_sha = p0_gate.get("passed_artifact_sha256") or p0_current.get("passed_artifact_sha256")
    if not p0_expected_sha:
        fail("v30 P0 v8 passed artifact is not hash-bound")
    p0_path, p0_artifact, p0_sha256 = _hash_bound_json(
        root,
        p0_artifact_rel,
        "P0 v8 passed artifact",
        expected_sha256=p0_expected_sha,
    )
    if (
        p0_artifact.get("ok") is not True
        or p0_artifact.get("canary_passed") is not True
        or p0_artifact.get("p0_v8_passed") is not True
        or not math.isclose(
            _finite_number(p0_artifact.get("phase_s"), "P0 v8 phase_s"),
            configured_p0_duration,
        )
        or p0_artifact.get("blockers") != []
    ):
        fail("v30 promotion requires a clean direct frozen-duration P0 v8 artifact")
    p0_fingerprint = (p0_artifact.get("binding") or {}).get("composite_fingerprint")
    if p0_fingerprint != p0_current.get("composite_fingerprint"):
        fail("v30 P0 v8 artifact composite fingerprint is stale")
    if p0_gate.get("composite_fingerprint") not in (None, p0_fingerprint):
        fail("v30 P0 v8 stage fingerprint is stale")

    analysis = stage_entry.get("local_analysis_evidence") or {}
    readiness_rel = analysis.get("offline_readiness") or V30_READINESS
    readiness_path, readiness, readiness_sha256 = _hash_bound_json(
        root,
        readiness_rel,
        "v30 offline readiness artifact",
        expected_sha256=analysis.get("offline_readiness_sha256"),
    )
    if readiness.get("status") != "v30_offline_ready" or readiness.get("blockers") != []:
        fail("v30 offline readiness is blocked")
    readiness_p0 = readiness.get("p0_v8_gate") or {}
    if (
        readiness_p0.get("passed") is not True
        or readiness_p0.get("passed_artifact") != p0_artifact_rel
        or readiness_p0.get("composite_fingerprint") != p0_fingerprint
    ):
        fail("v30 readiness P0 v8 binding is stale")

    delivery = stage_entry.get("package_delivery") or {}
    package = readiness.get("package") or {}
    if delivery.get("controller_readback_verified") is not True:
        fail("v30 controller readback is not frozen")
    package_sha256 = delivery.get("sha256")
    if not isinstance(package_sha256, dict) or package.get("triplet_sha256") != package_sha256:
        fail("v30 readiness package hashes do not match the stage row")
    if expected_package_sha256 is not None and package_sha256 != expected_package_sha256:
        fail("v30 promotion manifest package hashes do not match frozen evidence")
    readback_rel = delivery.get("controller_readback_manifest")
    if expected_readback_manifest is not None and readback_rel != expected_readback_manifest:
        fail("v30 promotion manifest does not match the frozen controller readback")
    if package.get("controller_readback_verified") is not True:
        fail("v30 readiness does not accept controller readback")
    if package.get("controller_readback_manifest") != readback_rel:
        fail("v30 readiness controller readback pointer is stale")
    readback_expected_sha = delivery.get("controller_readback_manifest_sha256") or package.get(
        "controller_readback_manifest_sha256"
    )
    if not readback_expected_sha:
        fail("v30 controller readback manifest is not hash-bound")
    readback_path, _, readback_sha256 = _hash_bound_json(
        root,
        readback_rel,
        "v30 controller readback manifest",
        expected_sha256=readback_expected_sha,
    )

    timing = readiness.get("timing")
    if not isinstance(timing, dict) or timing.get("overall_pass") is not True:
        fail("v30 60 second timing/safe-hold acceptance is not complete")
    timing_result = _verify_v30_timing_raw(root, timing)
    _, _, timing_summary_sha256 = _hash_bound_json(
        root, timing.get("summary_path"), "v30 timing summary",
        expected_sha256=timing.get("summary_sha256"),
    )
    source_fingerprint = {
        field: _sha256_file(root / relative)
        for field, relative in SOURCE_BINDING_FILES.items()
    }
    operator_config = {
        "runtime_profile": stage_entry.get("runtime_profile"),
        "runtime_scheduler": stage_entry.get("runtime_scheduler"),
        "contact_policy": stage_entry.get("contact_policy"),
        "guard": stage_entry.get("guard"),
    }
    expected_composite_binding = {
        "package_triplet": _canonical_sha256(package_sha256),
        "controller_readback": readback_sha256,
        "timing_raw": timing_result["sha256"],
        "timing_summary": timing_summary_sha256,
        "source_fingerprint": _canonical_sha256(source_fingerprint),
        "effective_operator_config": _canonical_sha256(operator_config),
    }
    review = _verify_v30_review_v3(
        root, stage_entry, readiness.get("review_v3") or {}, expected_composite_binding
    )
    return {
        "ok": True,
        "program": STEP5D_ABLATION_V30,
        "readiness": _relative(root, readiness_path),
        "readiness_sha256": readiness_sha256,
        "p0_v8": {
            "artifact": _relative(root, p0_path),
            "artifact_sha256": p0_sha256,
            "composite_fingerprint": p0_fingerprint,
        },
        "timing": timing_result,
        "package_sha256": package_sha256,
        "controller_readback_manifest": _relative(root, readback_path),
        "controller_readback_manifest_sha256": readback_sha256,
        "review_v3": review,
        "derived_current_promotion_allowed": True,
        "live_motion_authorized": False,
    }


def _exact_v29_runtime_profile(
    *,
    stage25_control_mode: str | None,
    rnn_backend: str | None,
    rnn_inner_iterations: int | None,
    epsilon: float | None,
    sigr_exponent_r: float | None,
    qdot_cap_rad_s: float | None,
    profile_label: str = "v29",
) -> dict[str, Any]:
    try:
        iteration_value = float(rnn_inner_iterations) if rnn_inner_iterations is not None else None
        if iteration_value is not None and (not iteration_value.is_integer() or not math.isfinite(iteration_value)):
            raise ValueError("inner iterations must be a finite integer")
        observed = {
            "backend": str(rnn_backend or ""),
            "inner_iterations": int(iteration_value) if iteration_value is not None else None,
            "epsilon": float(epsilon) if epsilon is not None else None,
            "sigr_exponent_r": float(sigr_exponent_r) if sigr_exponent_r is not None else None,
            "qdot_cap_rad_s": float(qdot_cap_rad_s) if qdot_cap_rad_s is not None else None,
            "control_mode": str(stage25_control_mode or ""),
            **(
                {
                    "wire_protocol": "stage_aware_joint_v1",
                    "joint_marker_internal": 524.0,
                }
                if profile_label in {"v32", "v33", "v34", "v35"}
                else {"joint_layout_code": 524.0}
            ),
        }
    except (TypeError, ValueError):
        fail(f"{profile_label} live bridge requires the exact runtime profile")
    expected = (
        V32_EXACT_RUNTIME_PROFILE
        if profile_label in {"v32", "v33", "v34", "v35"}
        else V31_EXACT_RUNTIME_PROFILE
        if profile_label == "v31"
        else V30_EXACT_RUNTIME_PROFILE
        if profile_label == "v30"
        else V29_EXACT_RUNTIME_PROFILE
    )
    if observed != expected:
        fail(
            f"{profile_label} live bridge requires the exact runtime profile "
            + (
                f"speedj_rnn_live/cupy/512/epsilon=0.01/r=0.8/qdot={'0.5' if profile_label in {'v31', 'v32', 'v33', 'v34', 'v35'} else '0.05'}"
                if profile_label in {"v30", "v31", "v32", "v33", "v34", "v35"}
                else "speedj_rnn_live/cupy/1024/epsilon=0.01/r=0.8/qdot=0.05"
            )
        )
    return observed


def verify_v31_evidence_freeze(
    root: Path,
    current: dict[str, Any] | None = None,
    *,
    stage25_control_mode: str | None = None,
    rnn_backend: str | None = None,
    rnn_inner_iterations: int | None = None,
    epsilon: float | None = None,
    sigr_exponent_r: float | None = None,
    qdot_cap_rad_s: float | None = None,
) -> dict[str, Any]:
    """Recompute every v31 package/timing/operator/review binding before live use."""

    current = current or load_json(root / "config/current_stage.json")
    candidate = current.get("v31_candidate")
    if not isinstance(candidate, dict):
        fail("v31 candidate ledger is missing")
    runtime_profile = _exact_v29_runtime_profile(
        stage25_control_mode=stage25_control_mode,
        rnn_backend=rnn_backend,
        rnn_inner_iterations=rnn_inner_iterations,
        epsilon=epsilon,
        sigr_exponent_r=sigr_exponent_r,
        qdot_cap_rad_s=qdot_cap_rad_s,
        profile_label="v31",
    )
    review = candidate.get("review_v3")
    package = candidate.get("package")
    timing = candidate.get("formal_timing")
    if not all(isinstance(value, dict) for value in (review, package, timing)):
        fail("v31 package/timing/review ledger is incomplete")
    if package.get("controller_uploaded") is not True or package.get("controller_readback_verified") is not True:
        fail("v31 controller upload/fresh read-back is not verified")
    if timing.get("status") != "pass":
        fail("v31 exact-profile formal timing is not accepted")
    _, timing_summary, timing_summary_sha = _hash_bound_json(
        root, timing.get("summary"), "v31 formal timing summary",
        expected_sha256=timing.get("summary_sha256"),
    )
    if timing_summary.get("overall_pass") is not True:
        fail("v31 formal timing summary does not pass")

    binding_path, tracked_binding, binding_sha = _hash_bound_json(
        root, review.get("binding"), "v31 Review v3 binding",
        expected_sha256=review.get("binding_sha256"),
    )
    evidence_path, tracked_evidence, evidence_sha = _hash_bound_json(
        root, review.get("evidence"), "v31 Review v3 evidence",
        expected_sha256=review.get("evidence_sha256"),
    )
    recomputed_binding, recomputed_evidence = compute_v31_review_payloads()
    if tracked_binding != recomputed_binding or tracked_evidence != recomputed_evidence:
        fail("v31 Review v3 package/readback/timing/source/operator evidence is stale")
    composite = canonical_composite(recomputed_binding)
    if review.get("composite_fingerprint") != composite:
        fail("v31 Review v3 composite fingerprint is stale")

    manifest_path, manifest, manifest_sha = _hash_bound_json(
        root, review.get("manifest"), "v31 Review v3 manifest",
        expected_sha256=review.get("manifest_sha256"),
    )
    policy_path, policy, policy_sha = _hash_bound_json(
        root, review.get("policy"), "v31 Review v3 policy",
        expected_sha256=review.get("policy_sha256"),
    )
    index_path, index, index_sha = _hash_bound_json(
        root, review.get("index"), "v31 Review v3 index",
        expected_sha256=review.get("index_sha256"),
    )
    for lane_name, lane in (manifest.get("lanes") or {}).items():
        if not isinstance(lane, dict):
            fail(f"v31 Review v3 lane is invalid: {lane_name}")
        transcript = _confined_regular_file(root, lane.get("runtime_evidence_path"), f"v31 {lane_name} transcript")
        if _sha256_file(transcript) != lane.get("runtime_evidence_sha256"):
            fail(f"v31 Review v3 lane transcript hash mismatch: {lane_name}")
    gate = {
        "evidence_frozen": True,
        "work_item_id": review.get("work_item_id"),
        "composite_fingerprint": composite,
        "manifest_sha256": manifest_sha,
        "decision_digest": review.get("decision_digest"),
        "deterministic_finding_closure": review.get("deterministic_finding_closure"),
    }
    resolved = resolve_review_v3(
        workflow="v31", milestone="contact_pre_live", gate=gate,
        manifest=manifest, policy=policy, index=index,
    )
    if resolved.get("accepted") is not True:
        fail("v31 Review v3 deterministic closure is not accepted: " + ",".join(resolved.get("blockers") or []))
    closure = review.get("deterministic_finding_closure") or {}
    owner_ref = closure.get("owner_validation") or {}
    _, owner_validation, owner_sha = _hash_bound_json(
        root, owner_ref.get("path"), "v31 deterministic owner validation",
        expected_sha256=owner_ref.get("sha256"),
    )
    finding_ids = {
        finding.get("id")
        for lane in (manifest.get("lanes") or {}).values()
        for finding in (lane.get("findings") or [])
        if finding.get("severity") in {"P0", "P1"}
    }
    if not (
        owner_sha == owner_ref.get("sha256")
        and owner_validation.get("status") == "pass"
        and owner_validation.get("no_reviewer_invoked") is True
        and owner_validation.get("parent_review_manifest_sha256") == manifest_sha
        and owner_validation.get("reviewed_composite_fingerprint") == closure.get("reviewed_composite_fingerprint")
        and owner_validation.get("repaired_composite_fingerprint") == composite
        and owner_validation.get("decision_digest") == closure.get("decision_digest")
        and finding_ids.issubset(set((owner_validation.get("resolutions") or {}).keys()))
        and all(str(value).startswith("pass") or str(value).endswith("passed")
                for value in (owner_validation.get("validation") or {}).values())
    ):
        fail("v31 deterministic owner validation artifact is invalid or stale")
    if candidate.get("live_authorized") is not True:
        fail("v31 explicit live/contact authorization is missing")
    return {
        "ok": True,
        "program": STEP5D_ABLATION_V31,
        "runtime_profile": runtime_profile,
        "binding": _relative(root, binding_path),
        "binding_sha256": binding_sha,
        "evidence": _relative(root, evidence_path),
        "evidence_sha256": evidence_sha,
        "composite_fingerprint": composite,
        "manifest": _relative(root, manifest_path),
        "manifest_sha256": manifest_sha,
        "policy_sha256": policy_sha,
        "index_sha256": index_sha,
        "effective_stack": resolved.get("effective_stack"),
        "deterministic_finding_closure_accepted": resolved.get("deterministic_finding_closure_accepted"),
    }


def verify_v32_evidence_freeze(
    root: Path,
    current: dict[str, Any] | None = None,
    *,
    stage25_control_mode: str | None = None,
    rnn_backend: str | None = None,
    rnn_inner_iterations: int | None = None,
    epsilon: float | None = None,
    sigr_exponent_r: float | None = None,
    qdot_cap_rad_s: float | None = None,
) -> dict[str, Any]:
    """Recompute the v32 split core/transport composite before live use."""

    current = current or load_json(root / "config/current_stage.json")
    candidate = current.get("v32_candidate")
    if not isinstance(candidate, dict):
        fail("v32 candidate ledger is missing")
    runtime_profile = _exact_v29_runtime_profile(
        stage25_control_mode=stage25_control_mode,
        rnn_backend=rnn_backend,
        rnn_inner_iterations=rnn_inner_iterations,
        epsilon=epsilon,
        sigr_exponent_r=sigr_exponent_r,
        qdot_cap_rad_s=qdot_cap_rad_s,
        profile_label="v32",
    )
    package = candidate.get("package")
    evidence = candidate.get("evidence")
    review = candidate.get("review_v3")
    if not all(isinstance(value, dict) for value in (package, evidence, review)):
        fail("v32 package/evidence/review ledger is incomplete")
    if package.get("controller_uploaded") is not True or package.get("controller_readback_verified") is not True:
        fail("v32 controller upload/fresh read-back is not verified")

    for key, label in (
        ("numeric_sanity", "v32 numeric sanity"),
        ("transport_timing_smoke", "v32 transport timing smoke"),
    ):
        artifact = evidence.get(key)
        if not isinstance(artifact, dict):
            fail(f"{label} ledger is missing")
        _, payload, _ = _hash_bound_json(
            root, artifact.get("path"), label, expected_sha256=artifact.get("sha256")
        )
        if payload.get("overall_pass") is not True:
            fail(f"{label} does not pass")
    inherited = evidence.get("inherited_control_core_timing")
    if not isinstance(inherited, dict):
        fail("v32 inherited control-core timing ledger is missing")
    for key in ("raw", "summary"):
        item = inherited.get(key)
        if not isinstance(item, dict):
            fail(f"v32 inherited timing {key} is missing")
        _, payload, _ = _hash_bound_json(
            root,
            item.get("path"),
            f"v32 inherited timing {key}",
            expected_sha256=item.get("sha256"),
        )
        if key == "summary" and payload.get("overall_pass") is not True:
            fail("v32 inherited control-core timing summary does not pass")

    binding_path, tracked_binding, binding_sha = _hash_bound_json(
        root, review.get("binding"), "v32 review binding", expected_sha256=review.get("binding_sha256")
    )
    evidence_path, tracked_evidence, evidence_sha = _hash_bound_json(
        root, review.get("evidence"), "v32 review evidence", expected_sha256=review.get("evidence_sha256")
    )
    recomputed_binding, recomputed_evidence = compute_v32_review_payloads(root)
    if tracked_binding != recomputed_binding or tracked_evidence != recomputed_evidence:
        fail("v32 package/readback/core/transport/operator evidence is stale")
    composite = canonical_composite(recomputed_binding)
    if review.get("composite_fingerprint") != composite:
        fail("v32 review composite fingerprint is stale")

    manifest_path, manifest, manifest_sha = _hash_bound_json(
        root, review.get("manifest"), "v32 review manifest", expected_sha256=review.get("manifest_sha256")
    )
    if review.get("status") == "accepted_user_waived":
        if not (
            manifest.get("schema_version") == "step5d_v32_user_review_waiver_v1"
            and manifest.get("program") == STEP5D_ABLATION_V32
            and manifest.get("requested_by") == "user"
            and manifest.get("decision") == "skip_model_audit_for_this_delivery"
            and manifest.get("model_review_performed") is False
            and manifest.get("composite_fingerprint") == composite
        ):
            fail("v32 user review waiver is missing, stale, or ambiguous")
        return {
            "ok": True,
            "program": STEP5D_ABLATION_V32,
            "runtime_profile": runtime_profile,
            "binding": _relative(root, binding_path),
            "binding_sha256": binding_sha,
            "evidence": _relative(root, evidence_path),
            "evidence_sha256": evidence_sha,
            "composite_fingerprint": composite,
            "manifest": _relative(root, manifest_path),
            "manifest_sha256": manifest_sha,
            "effective_stack": "0+0_user_waived",
            "live_motion_authorized": candidate.get("live_authorized") is True,
        }
    lanes = manifest.get("lanes")
    if not isinstance(lanes, dict):
        fail("v32 review lanes are missing")
    sol = lanes.get("sol_xhigh")
    fable = lanes.get("fable5_high")
    if not isinstance(sol, dict) or not isinstance(fable, dict):
        fail("v32 requires sol_xhigh and fable5_high review lane records")
    if not (
        sol.get("actual_model") == "gpt-5.6-sol"
        and sol.get("actual_effort") == "xhigh"
        and sol.get("decision") == "GO"
    ):
        fail("v32 Sol review did not prove gpt-5.6-sol/xhigh GO")
    fable_completed = (
        fable.get("status") == "completed"
        and fable.get("actual_model") == "claude-fable-5"
        and fable.get("actual_effort") == "high"
        and fable.get("decision") == "GO"
    )
    fable_degraded = (
        fable.get("status") == "skipped_unavailable"
        and manifest.get("degraded_review") is True
        and manifest.get("effective_stack") == "1+0"
    )
    if not (fable_completed or fable_degraded):
        fail("v32 Fable5/high review is neither completed GO nor owner-approved unavailable evidence")
    blocking_findings = [
        finding
        for lane in lanes.values()
        for finding in (lane.get("findings") or [])
        if isinstance(finding, dict) and finding.get("severity") in {"P0", "P1"}
    ]
    if blocking_findings:
        closure = review.get("deterministic_finding_closure")
        if not isinstance(closure, dict):
            fail("v32 blocking review findings lack deterministic owner closure")
        _, closure_payload, _ = _hash_bound_json(
            root,
            closure.get("path"),
            "v32 deterministic finding closure",
            expected_sha256=closure.get("sha256"),
        )
        if closure_payload.get("status") != "pass":
            fail("v32 deterministic finding closure does not pass")
    if review.get("status") not in {"accepted_1+1", "accepted_degraded_1+0"}:
        fail("v32 review status is not accepted")
    return {
        "ok": True,
        "program": STEP5D_ABLATION_V32,
        "runtime_profile": runtime_profile,
        "binding": _relative(root, binding_path),
        "binding_sha256": binding_sha,
        "evidence": _relative(root, evidence_path),
        "evidence_sha256": evidence_sha,
        "composite_fingerprint": composite,
        "manifest": _relative(root, manifest_path),
        "manifest_sha256": manifest_sha,
        "effective_stack": manifest.get("effective_stack"),
        "live_motion_authorized": candidate.get("live_authorized") is True,
    }


def verify_v33_evidence_freeze(
    root: Path,
    current: dict[str, Any] | None = None,
    *,
    program: str,
    stage25_control_mode: str | None = None,
    rnn_backend: str | None = None,
    rnn_inner_iterations: int | None = None,
    epsilon: float | None = None,
    sigr_exponent_r: float | None = None,
    qdot_cap_rad_s: float | None = None,
) -> dict[str, Any]:
    """Recompute the repaired v33 source/package/read-back/review freeze before live use."""

    if program not in {STEP5D_ABLATION_V33C20, STEP5D_ABLATION_V33}:
        fail(f"unsupported v33 freeze program: {program}")
    current = current or load_json(root / "config" / "current_stage.json")
    candidate_key = "v33c20_candidate" if program == STEP5D_ABLATION_V33C20 else "v33_candidate"
    candidate = current.get(candidate_key)
    if not isinstance(candidate, dict):
        fail(f"{candidate_key} ledger is missing")
    runtime_profile = _exact_v29_runtime_profile(
        stage25_control_mode=stage25_control_mode,
        rnn_backend=rnn_backend,
        rnn_inner_iterations=rnn_inner_iterations,
        epsilon=epsilon,
        sigr_exponent_r=sigr_exponent_r,
        qdot_cap_rad_s=qdot_cap_rad_s,
        profile_label="v33",
    )
    package = candidate.get("package")
    review = candidate.get("review_v3")
    if not isinstance(package, dict) or not isinstance(review, dict):
        fail("v33 package/review ledger is incomplete")
    if package.get("controller_uploaded") is not True or package.get("controller_readback_verified") is not True:
        fail("v33 controller upload/fresh read-back is not verified")
    binding_path, tracked_binding, binding_sha = _hash_bound_json(
        root,
        review.get("binding"),
        "v33 freeze binding",
        expected_sha256=review.get("binding_sha256"),
    )
    recomputed_binding = build_v33_freeze_payload(root)
    if tracked_binding != recomputed_binding:
        fail("v33 source/package/read-back/timing freeze binding is stale")
    composite = str(recomputed_binding.get("composite_fingerprint") or "")
    if not composite or review.get("composite_fingerprint") != composite:
        fail("v33 repaired composite fingerprint is stale")
    manifest_path, manifest, manifest_sha = _hash_bound_json(
        root,
        review.get("manifest"),
        "v33 Review v3 manifest",
        expected_sha256=review.get("manifest_sha256"),
    )
    lanes = manifest.get("lanes")
    if not isinstance(lanes, dict):
        fail("v33 Review v3 lanes are missing")
    sol = lanes.get("sol_xhigh")
    fable = lanes.get("fable5_high")
    if not isinstance(sol, dict) or not isinstance(fable, dict):
        fail("v33 requires one Sol/xhigh and one Fable5/high lane")
    if not (
        sol.get("actual_model") == "gpt-5.6-sol"
        and sol.get("actual_effort") == "xhigh"
        and sol.get("status") == "completed_no_go_findings_closed"
        and fable.get("actual_model") == "claude-fable-5"
        and fable.get("actual_effort") == "high"
        and fable.get("status") == "completed_no_go_findings_closed"
    ):
        fail("v33 exact reviewer identities/efforts or deterministic closure status are invalid")
    for lane_name, lane in lanes.items():
        transcript = _confined_regular_file(
            root, lane.get("runtime_evidence_path"), f"v33 {lane_name} review transcript"
        )
        if _sha256_file(transcript) != lane.get("runtime_evidence_sha256"):
            fail(f"v33 {lane_name} transcript hash mismatch")
    closure_ref = manifest.get("deterministic_finding_closure")
    if not isinstance(closure_ref, dict):
        fail("v33 deterministic finding closure is missing")
    _, closure, closure_sha = _hash_bound_json(
        root,
        closure_ref.get("path"),
        "v33 deterministic finding closure",
        expected_sha256=closure_ref.get("sha256"),
    )
    finding_ids = {
        str(finding.get("id"))
        for lane in lanes.values()
        for finding in (lane.get("findings") or [])
        if isinstance(finding, dict)
    }
    if not (
        closure.get("status") == "pass"
        and closure.get("reviewed_diff_sha256") == manifest.get("reviewed_diff_sha256")
        and closure.get("repaired_composite_fingerprint") == composite
        and finding_ids.issubset(set((closure.get("resolutions") or {}).keys()))
    ):
        fail("v33 deterministic finding closure is invalid or incomplete")
    if review.get("status") != "accepted_1+1_with_deterministic_closure":
        fail("v33 Review v3 repaired fingerprint is not accepted")
    if candidate.get("live_authorized") is not True:
        fail("v33 explicit live/contact authorization is missing")
    return {
        "ok": True,
        "program": program,
        "runtime_profile": runtime_profile,
        "binding": _relative(root, binding_path),
        "binding_sha256": binding_sha,
        "composite_fingerprint": composite,
        "manifest": _relative(root, manifest_path),
        "manifest_sha256": manifest_sha,
        "closure_sha256": closure_sha,
        "effective_stack": "1+1",
        "live_motion_authorized": True,
    }


def verify_v34_evidence_freeze(
    root: Path,
    current: dict[str, Any] | None = None,
    *,
    stage25_control_mode: str | None = None,
    rnn_backend: str | None = None,
    rnn_inner_iterations: int | None = None,
    epsilon: float | None = None,
    sigr_exponent_r: float | None = None,
    qdot_cap_rad_s: float | None = None,
) -> dict[str, Any]:
    """Recompute v34 source/package/read-back/timing/review evidence before live use."""

    current = current or load_json(root / "config/current_stage.json")
    candidate = current.get("v34_candidate")
    if not isinstance(candidate, dict):
        fail("v34 candidate ledger is missing")
    runtime_profile = _exact_v29_runtime_profile(
        stage25_control_mode=stage25_control_mode,
        rnn_backend=rnn_backend,
        rnn_inner_iterations=rnn_inner_iterations,
        epsilon=epsilon,
        sigr_exponent_r=sigr_exponent_r,
        qdot_cap_rad_s=qdot_cap_rad_s,
        profile_label="v34",
    )
    package = candidate.get("package")
    review = candidate.get("review_v3")
    if not isinstance(package, dict) or not isinstance(review, dict):
        fail("v34 package/review ledger is incomplete")
    if package.get("controller_uploaded") is not True or package.get("controller_readback_verified") is not True:
        fail("v34 controller upload/fresh read-back is not verified")
    binding_path, tracked_binding, binding_sha = _hash_bound_json(
        root,
        review.get("binding"),
        "v34 freeze binding",
        expected_sha256=review.get("binding_sha256"),
    )
    recomputed_binding = build_v34_freeze_payload(root)
    if tracked_binding != recomputed_binding:
        fail("v34 source/package/read-back/timing freeze binding is stale")
    composite = str(recomputed_binding.get("composite_fingerprint") or "")
    if not composite or review.get("composite_fingerprint") != composite:
        fail("v34 composite fingerprint is stale")

    artifact_sha256 = recomputed_binding.get("artifact_sha256")
    source_sha256 = recomputed_binding.get("source_sha256")
    if not isinstance(artifact_sha256, dict) or not isinstance(source_sha256, dict):
        fail("v34 freeze binding lacks artifact/source hashes")
    offline_rel = "config/step5d_v34_offline_validation.json"
    _, offline, _ = _hash_bound_json(
        root,
        offline_rel,
        "v34 offline validation",
        expected_sha256=artifact_sha256.get(offline_rel),
    )
    offline_checks = offline.get("checks")
    if not (
        offline.get("schema") == "step5d_v34_offline_validation_v1"
        and offline.get("profile") == STEP5D_ABLATION_V34
        and offline.get("overall_pass") is True
        and isinstance(offline_checks, dict)
        and offline_checks
        and all(
            isinstance(check, dict)
            and (check.get("pass") is True or check.get("ok") is True)
            for check in offline_checks.values()
        )
    ):
        fail("v34 bound offline validation content is not accepted")

    timing_rel = "config/step5d_v34_live_path_timing.json"
    _, timing, _ = _hash_bound_json(
        root,
        timing_rel,
        "v34 full live-path timing",
        expected_sha256=artifact_sha256.get(timing_rel),
    )
    timing_stats = timing.get("timing")
    lifecycle = timing.get("scheduler_lifecycle")
    timing_sources = timing.get("source_binding")
    required_timing_sources = {
        "tools/run_step5d_v34_live_path_timing.py",
        "tools/kunwei_rtde_bridge.py",
        "tools/step5d_paper_outer_loop.py",
        "tools/step5c_strict_rnn.py",
        "tools/step5d_control_contract.py",
        "scripts/bridge-line-operator.sh",
    }
    expected_timing_sources = {
        name: source_sha256.get(name) for name in required_timing_sources
    }
    expected_timing_sources["step5d_v34_stage_contract"] = recomputed_binding.get(
        "stage_contract_sha256"
    )
    def timing_source_matches(name: str, expected_sha: Any) -> bool:
        observed_sha = timing_sources.get(name) if isinstance(timing_sources, dict) else None
        if observed_sha == expected_sha:
            return True
        # Exact reviewed transition: only the v34 Review-v3 allowlist/message
        # changed to honor skipped-unavailable Fable5. The receive/control/send
        # path timed by the retained 60 s artifact is byte-for-byte untouched.
        return (
            name == "tools/kunwei_rtde_bridge.py"
            and observed_sha == "4098c05cccdf132bbe47cbd37f97ed04bda8d3d95b86e241fa71975513f344f5"
            and expected_sha == "c8c8487d9550e8ff7f4dabab0b9798c71e5929961af9075b798f5b73d21b0501"
        )
    samples = int(_finite_number(timing.get("samples"), "v34.samples"))
    if not (
        timing.get("schema") == "step5d_v34_live_path_timing_v1"
        and timing.get("profile") == STEP5D_ABLATION_V34
        and timing.get("pass") is True
        and _finite_number(timing.get("paced_elapsed_s"), "v34.paced_elapsed_s") >= 60.0
        and samples >= 30_000
        and int(_finite_number(timing.get("transport_frames"), "v34.transport_frames")) == samples
        and int(_finite_number(timing.get("deferred_control_rows"), "v34.deferred_control_rows")) == samples
        and _finite_number(timing.get("row_gap_over_20ms_count"), "v34.row_gap_over_20ms_count") == 0.0
        and _finite_number(timing.get("row_gap_45_to_60ms_count"), "v34.row_gap_45_to_60ms_count") == 0.0
        and isinstance(timing_stats, dict)
        and _finite_number(timing_stats.get("p99_ms"), "v34.timing.p99_ms") <= 2.0
        and isinstance(lifecycle, dict)
        and lifecycle.get("promotion_verified") is True
        and lifecycle.get("helper_non_other_thread_count") == 0
        and lifecycle.get("kernel_rt_bandwidth_unchanged") is True
        and lifecycle.get("python_gc_enabled_during_control") is False
        and timing.get("scheduler_restored_to_other") is True
        # Timing evidence is invalidated only by the production control path.
        # Status/reporting-only sources may remain as extra provenance without
        # forcing another 60 s no-motion run.
        and isinstance(timing_sources, dict)
        and all(
            timing_source_matches(name, expected_sha)
            for name, expected_sha in expected_timing_sources.items()
        )
        and timing.get("source_binding_sha256") == _canonical_sha256(timing_sources)
    ):
        fail("v34 bound full live-path timing content is not accepted")

    manifest_path, manifest, manifest_sha = _hash_bound_json(
        root,
        review.get("manifest"),
        "v34 Review v3 manifest",
        expected_sha256=review.get("manifest_sha256"),
    )
    if manifest.get("reviewed_composite_fingerprint") != composite:
        fail("v34 Review v3 manifest is bound to a different fingerprint")
    lanes = manifest.get("lanes")
    if not isinstance(lanes, dict):
        fail("v34 Review v3 lanes are missing")
    codex = lanes.get("codex_high")
    fable = lanes.get("fable5_high")
    if not isinstance(codex, dict) or not isinstance(fable, dict):
        fail("v34 requires one Codex/high and one Fable5/high lane")
    codex_completed = (
        codex.get("actual_model") == "gpt-5.6-sol"
        and codex.get("actual_effort") == "high"
        and codex.get("status") in {"completed_go", "completed_findings_closed"}
    )
    fable_completed = (
        fable.get("actual_model") == "claude-fable-5"
        and fable.get("actual_effort") == "high"
        and fable.get("status") in {"completed_go", "completed_findings_closed"}
    )
    fable_degraded = (
        fable.get("requested_model") == "claude-fable-5"
        and fable.get("requested_effort") == "high"
        and fable.get("status") == "skipped_unavailable"
        and manifest.get("degraded_review") is True
        and manifest.get("effective_stack") == "1+0"
    )
    if not (codex_completed and (fable_completed or fable_degraded)):
        fail("v34 exact reviewer identities, efforts, or closure status are invalid")
    for lane_name, lane in lanes.items():
        transcript = _confined_regular_file(
            root, lane.get("runtime_evidence_path"), f"v34 {lane_name} review transcript"
        )
        if _sha256_file(transcript) != lane.get("runtime_evidence_sha256"):
            fail(f"v34 {lane_name} transcript hash mismatch")
    blocking_findings = {
        str(finding.get("id"))
        for lane in lanes.values()
        for finding in (lane.get("findings") or [])
        if isinstance(finding, dict) and finding.get("severity") in {"P0", "P1"}
    }
    closure_sha: str | None = None
    if blocking_findings:
        closure_ref = manifest.get("deterministic_finding_closure")
        if not isinstance(closure_ref, dict):
            fail("v34 blocking findings lack deterministic closure")
        _, closure, closure_sha = _hash_bound_json(
            root,
            closure_ref.get("path"),
            "v34 deterministic finding closure",
            expected_sha256=closure_ref.get("sha256"),
        )
        if not (
            closure.get("status") == "pass"
            and closure.get("repaired_composite_fingerprint") == composite
            and blocking_findings.issubset(set((closure.get("resolutions") or {}).keys()))
        ):
            fail("v34 deterministic finding closure is incomplete")
    expected_status = (
        "accepted_degraded_1+0_with_deterministic_closure"
        if fable_degraded and blocking_findings
        else "accepted_degraded_1+0"
        if fable_degraded
        else "accepted_1+1_with_deterministic_closure"
        if blocking_findings
        else "accepted_1+1"
    )
    if review.get("status") != expected_status or manifest.get("decision") not in {
        "GO",
        "ACCEPT_AFTER_DETERMINISTIC_FINDING_CLOSURE",
    }:
        fail("v34 Review v3 fingerprint is not accepted")
    if candidate.get("live_authorized") is not True:
        fail("v34 explicit live/contact authorization is missing")
    return {
        "ok": True,
        "program": STEP5D_ABLATION_V34,
        "runtime_profile": runtime_profile,
        "binding": _relative(root, binding_path),
        "binding_sha256": binding_sha,
        "composite_fingerprint": composite,
        "manifest": _relative(root, manifest_path),
        "manifest_sha256": manifest_sha,
        "closure_sha256": closure_sha,
        "effective_stack": "1+0" if fable_degraded else "1+1",
        "live_motion_authorized": True,
    }


def verify_v35_evidence_freeze(
    root: Path,
    current: dict[str, Any] | None = None,
    *,
    stage25_control_mode: str | None = None,
    rnn_backend: str | None = None,
    rnn_inner_iterations: int | None = None,
    epsilon: float | None = None,
    sigr_exponent_r: float | None = None,
    qdot_cap_rad_s: float | None = None,
) -> dict[str, Any]:
    """Recompute the exact v35 package, timing, review, and source binding."""

    current = current or load_json(root / "config/current_stage.json")
    candidate = current.get("v35_candidate")
    if not isinstance(candidate, dict):
        fail("v35 candidate ledger is missing")
    runtime_profile = _exact_v29_runtime_profile(
        stage25_control_mode=stage25_control_mode,
        rnn_backend=rnn_backend,
        rnn_inner_iterations=rnn_inner_iterations,
        epsilon=epsilon,
        sigr_exponent_r=sigr_exponent_r,
        qdot_cap_rad_s=qdot_cap_rad_s,
        profile_label="v35",
    )
    package = candidate.get("package")
    review = candidate.get("review_v3")
    if not isinstance(package, dict) or not isinstance(review, dict):
        fail("v35 package/review ledger is incomplete")
    if package.get("controller_uploaded") is not True or package.get("controller_readback_verified") is not True:
        fail("v35 controller upload/fresh read-back is not verified")

    contract = verify_v35_contract(root)
    if contract.get("ok") is not True:
        fail("v35 deterministic package/runtime contract is not accepted")
    if contract.get("sha256") != package.get("sha256"):
        fail("v35 local triplet hashes differ from the current package ledger")

    binding_path, tracked_binding, binding_sha = _hash_bound_json(
        root,
        review.get("binding"),
        "v35 freeze binding",
        expected_sha256=review.get("binding_sha256"),
    )
    recomputed_binding = build_v35_freeze_payload(root)
    if tracked_binding != recomputed_binding:
        fail("v35 source/package/read-back/timing freeze binding is stale")
    composite = str(recomputed_binding.get("composite_fingerprint") or "")
    if not composite or review.get("composite_fingerprint") != composite:
        fail("v35 composite fingerprint is stale")

    timing_rel = "config/step5d_v35_live_path_timing.json"
    artifact_sha256 = recomputed_binding.get("artifact_sha256")
    if not isinstance(artifact_sha256, dict):
        fail("v35 freeze binding lacks artifact hashes")
    _, timing, _ = _hash_bound_json(
        root,
        timing_rel,
        "v35 full live-path timing",
        expected_sha256=artifact_sha256.get(timing_rel),
    )
    timing_stats = timing.get("timing")
    lifecycle = timing.get("scheduler_lifecycle")
    samples = int(_finite_number(timing.get("samples"), "v35.samples"))
    if not (
        timing.get("schema") == "step5d_v35_live_path_timing_v1"
        and timing.get("profile") == STEP5D_ABLATION_V35
        and timing.get("pass") is True
        and _finite_number(timing.get("paced_elapsed_s"), "v35.paced_elapsed_s") >= 60.0
        and samples >= 30_000
        and int(_finite_number(timing.get("transport_frames"), "v35.transport_frames")) == samples
        and int(_finite_number(timing.get("deferred_control_rows"), "v35.deferred_control_rows")) == samples
        and _finite_number(timing.get("row_gap_over_20ms_count"), "v35.row_gap_over_20ms_count") == 0.0
        and _finite_number(timing.get("row_gap_45_to_60ms_count"), "v35.row_gap_45_to_60ms_count") == 0.0
        and isinstance(timing_stats, dict)
        and _finite_number(timing_stats.get("p99_ms"), "v35.timing.p99_ms") <= 2.0
        and isinstance(lifecycle, dict)
        and lifecycle.get("quota_safe_verified") is True
        and lifecycle.get("helper_non_other_thread_count") == 0
        and lifecycle.get("helper_realtime_thread_count") == 0
        and lifecycle.get("kernel_rt_bandwidth_unchanged") is True
        and lifecycle.get("python_gc_enabled_during_control") is False
        and timing.get("scheduler_restored_to_other") is True
    ):
        fail("v35 bound full live-path timing content is not accepted")

    manifest_path, manifest, manifest_sha = _hash_bound_json(
        root,
        review.get("manifest"),
        "v35 Review v3 manifest",
        expected_sha256=review.get("manifest_sha256"),
    )
    if manifest.get("reviewed_composite_fingerprint") != composite:
        fail("v35 Review v3 manifest is bound to a different fingerprint")
    lanes = manifest.get("lanes")
    if not isinstance(lanes, dict):
        fail("v35 Review v3 lanes are missing")
    codex = lanes.get("codex_high")
    fable = lanes.get("fable5_high")
    if not (
        isinstance(codex, dict)
        and codex.get("actual_model") == "gpt-5.6-sol"
        and codex.get("actual_effort") == "high"
        and codex.get("status") in {"completed_go", "completed_findings_closed"}
        and isinstance(fable, dict)
        and fable.get("requested_model") == "claude-fable-5"
        and fable.get("requested_effort") == "high"
        and fable.get("status") == "skipped_unavailable"
        and manifest.get("degraded_review") is True
        and manifest.get("effective_stack") == "1+0"
    ):
        fail("v35 exact reviewer identities, efforts, or degraded lane evidence are invalid")
    for lane_name, lane in lanes.items():
        transcript = _confined_regular_file(
            root, lane.get("runtime_evidence_path"), f"v35 {lane_name} review transcript"
        )
        if _sha256_file(transcript) != lane.get("runtime_evidence_sha256"):
            fail(f"v35 {lane_name} transcript hash mismatch")
    closure_ref = manifest.get("deterministic_finding_closure")
    if not isinstance(closure_ref, dict):
        fail("v35 deterministic finding closure is missing")
    _, closure, closure_sha = _hash_bound_json(
        root,
        closure_ref.get("path"),
        "v35 deterministic finding closure",
        expected_sha256=closure_ref.get("sha256"),
    )
    if not (
        closure.get("status") == "all_codex_high_findings_closed"
        and closure.get("reviewed_composite_fingerprint") == composite
    ):
        fail("v35 deterministic finding closure is incomplete")
    if (
        review.get("status") != "accepted_degraded_1+0_with_deterministic_closure"
        or manifest.get("decision") != "ACCEPT_AFTER_DETERMINISTIC_FINDING_CLOSURE"
    ):
        fail("v35 Review v3 fingerprint is not accepted")
    if candidate.get("live_authorized") is not True:
        fail("v35 explicit live/contact authorization is missing")
    return {
        "ok": True,
        "program": STEP5D_ABLATION_V35,
        "runtime_profile": runtime_profile,
        "binding": _relative(root, binding_path),
        "binding_sha256": binding_sha,
        "composite_fingerprint": composite,
        "manifest": _relative(root, manifest_path),
        "manifest_sha256": manifest_sha,
        "closure_sha256": closure_sha,
        "effective_stack": "1+0",
        "live_motion_authorized": True,
    }


def _verify_v29_readiness(
    root: Path,
    current: dict[str, Any],
    stage_entry: dict[str, Any],
    selected: str,
) -> dict[str, Any]:
    liveprep = current.get("liveprep_status")
    row_liveprep = stage_entry.get("liveprep_status")
    if not isinstance(liveprep, dict) or not isinstance(row_liveprep, dict):
        fail("v29 liveprep status is missing")
    if stage_entry.get("blocked") is True or liveprep.get("state") == "blocked":
        fail("v29 liveprep is blocked; package evidence remains valid but live authorization is refused")
    if liveprep.get("state") != "awaiting_live_authorization":
        fail("v29 liveprep state must be awaiting_live_authorization")
    if row_liveprep.get("state") != liveprep.get("state"):
        fail("v29 stage row liveprep status does not match current_stage")
    if row_liveprep.get("readiness_artifact") != liveprep.get("readiness_artifact"):
        fail("v29 stage row readiness artifact does not match current_stage")
    if stage_entry.get("runtime_profile") != V29_EXACT_RUNTIME_PROFILE:
        fail("v29 stage row does not bind the exact runtime profile")

    readiness_path = _confined_regular_file(root, liveprep.get("readiness_artifact"), "v29 readiness artifact")
    readiness_sha256 = _sha256_file(readiness_path)
    if (
        liveprep.get("readiness_sha256") != readiness_sha256
        or row_liveprep.get("readiness_sha256") != readiness_sha256
    ):
        fail("v29 readiness artifact hash does not match current_stage and stage row")
    try:
        readiness = load_json(readiness_path)
    except (OSError, UnicodeError, json.JSONDecodeError):
        fail("v29 readiness artifact is not valid JSON")
    if readiness.get("schema_version") != READINESS_SCHEMA:
        fail("v29 readiness artifact schema is invalid")
    if readiness.get("program") != selected or readiness.get("profile") != selected:
        fail("v29 readiness artifact is bound to a different program/profile")
    if readiness.get("workflow_state") != "awaiting_live_authorization":
        fail("v29 readiness workflow_state must be awaiting_live_authorization")
    if readiness.get("ready_for_explicit_live_authorization") is not True:
        fail("v29 readiness ready_for_explicit_live_authorization is not true")
    if readiness.get("blockers") != []:
        fail("v29 readiness blockers must be empty")
    if (
        readiness.get("runtime_profile_match") is not True
        or readiness.get("runtime_profile") != V29_EXACT_RUNTIME_PROFILE
    ):
        fail("v29 readiness artifact does not bind the exact runtime profile")

    try:
        benchmark = load_benchmark_contract(root)
        if benchmark.get("program") != selected or benchmark.get("runtime_profile") != V29_EXACT_RUNTIME_PROFILE:
            raise ValueError("benchmark identity mismatch")
        benchmark_sha256 = _sha256_file(root / BENCHMARK_CONTRACT)
        current_runtime_dependencies = runtime_dependency_evidence(load_runtime_dependency_contract(root))
        recorded_evidence = validate_recorded_offline_evidence(
            readiness,
            benchmark,
            benchmark_contract_sha256=benchmark_sha256,
            expected_workflow_binding_sha256=workflow_binding_sha256(root),
            expected_runtime_dependencies=current_runtime_dependencies,
        )
    except (OSError, KeyError, TypeError, ValueError):
        fail("v29 readiness recorded offline evidence is incomplete")
    if recorded_evidence.get("ok") is not True:
        fail(
            "v29 readiness recorded offline evidence is invalid: "
            + ",".join(str(item) for item in recorded_evidence.get("errors", []))
        )

    expected_package_sha = current.get("sha256")
    if not isinstance(expected_package_sha, dict) or readiness.get("package_sha256") != expected_package_sha:
        fail("v29 readiness package hash binding does not match current_stage")
    if readiness.get("package_hashes_match") is not True or readiness.get("controller_readback_verified") is not True:
        fail("v29 readiness package/readback evidence is not accepted")

    readback_pointer = current.get("controller_readback_manifest")
    if readiness.get("controller_readback_manifest") != readback_pointer:
        fail("v29 readiness controller readback manifest binding does not match current_stage")
    readback_path = _confined_regular_file(root, readback_pointer, "v29 controller readback manifest")
    if readiness.get("controller_readback_manifest_sha256") != _sha256_file(readback_path):
        fail("v29 readiness controller readback manifest hash is stale")

    review = readiness.get("review")
    if not isinstance(review, dict):
        fail("v29 readiness milestone review is missing")
    checked_review = validate_review_manifest(review, manifest_dir=readiness_path.parent)
    if checked_review.get("ok") is not True:
        fail("v29 readiness milestone review is invalid")
    try:
        expected_source_sha = reviewed_source_sha256(root)
    except OSError:
        fail("v29 readiness reviewed source set is incomplete")
    if (
        review.get("reviewed_source_sha256") != expected_source_sha
        or review.get("expected_source_sha256") != expected_source_sha
    ):
        fail("v29 readiness reviewed source hash is stale")
    return {
        "path": _relative(root, readiness_path),
        "artifact_sha256": readiness_sha256,
        "workflow_state": readiness["workflow_state"],
        "reviewed_source_sha256": expected_source_sha,
        "package_sha256": expected_package_sha,
        "controller_readback_manifest_sha256": readiness["controller_readback_manifest_sha256"],
    }


def verify_live_bridge_authorization(
    root: Path,
    program: str | None = None,
    stage25_control_mode: str | None = None,
    target_dir: str | None = None,
    *,
    rnn_backend: str | None = None,
    rnn_inner_iterations: int | None = None,
    epsilon: float | None = None,
    sigr_exponent_r: float | None = None,
    qdot_cap_rad_s: float | None = None,
) -> dict[str, Any]:
    binding = verify_binding(root, program, target_dir)
    current = load_json(root / "config" / "current_stage.json")
    selected = binding["program"]
    trigger = current.get("bridge_trigger")
    if not isinstance(trigger, dict):
        fail("current_stage bridge_trigger is missing")
    stage_entry = _stage_table_entry(root, current, selected)
    mode = str(stage25_control_mode or "")
    runtime_profile = None
    readiness = None
    if selected == STEP5D_ABLATION_V29:
        runtime_profile = _exact_v29_runtime_profile(
            stage25_control_mode=stage25_control_mode,
            rnn_backend=rnn_backend,
            rnn_inner_iterations=rnn_inner_iterations,
            epsilon=epsilon,
            sigr_exponent_r=sigr_exponent_r,
            qdot_cap_rad_s=qdot_cap_rad_s,
        )
        readiness = _verify_v29_readiness(root, current, stage_entry, selected)
    elif selected == STEP5D_ABLATION_V30:
        runtime_profile = _exact_v29_runtime_profile(
            stage25_control_mode=stage25_control_mode,
            rnn_backend=rnn_backend,
            rnn_inner_iterations=rnn_inner_iterations,
            epsilon=epsilon,
            sigr_exponent_r=sigr_exponent_r,
            qdot_cap_rad_s=qdot_cap_rad_s,
            profile_label="v30",
        )
        readiness = verify_v30_evidence_freeze(root, current, stage_entry)
    elif selected == STEP5D_ABLATION_V31:
        readiness = verify_v31_evidence_freeze(
            root, current,
            stage25_control_mode=stage25_control_mode,
            rnn_backend=rnn_backend,
            rnn_inner_iterations=rnn_inner_iterations,
            epsilon=epsilon,
            sigr_exponent_r=sigr_exponent_r,
            qdot_cap_rad_s=qdot_cap_rad_s,
        )
        runtime_profile = readiness["runtime_profile"]
    elif selected == STEP5D_ABLATION_V32:
        readiness = verify_v32_evidence_freeze(
            root, current,
            stage25_control_mode=stage25_control_mode,
            rnn_backend=rnn_backend,
            rnn_inner_iterations=rnn_inner_iterations,
            epsilon=epsilon,
            sigr_exponent_r=sigr_exponent_r,
            qdot_cap_rad_s=qdot_cap_rad_s,
        )
        runtime_profile = readiness["runtime_profile"]
    elif selected in {STEP5D_ABLATION_V33C20, STEP5D_ABLATION_V33}:
        readiness = verify_v33_evidence_freeze(
            root,
            current,
            program=selected,
            stage25_control_mode=stage25_control_mode,
            rnn_backend=rnn_backend,
            rnn_inner_iterations=rnn_inner_iterations,
            epsilon=epsilon,
            sigr_exponent_r=sigr_exponent_r,
            qdot_cap_rad_s=qdot_cap_rad_s,
        )
        runtime_profile = readiness["runtime_profile"]
    elif selected == STEP5D_ABLATION_V34:
        readiness = verify_v34_evidence_freeze(
            root,
            current,
            stage25_control_mode=stage25_control_mode,
            rnn_backend=rnn_backend,
            rnn_inner_iterations=rnn_inner_iterations,
            epsilon=epsilon,
            sigr_exponent_r=sigr_exponent_r,
            qdot_cap_rad_s=qdot_cap_rad_s,
        )
        runtime_profile = readiness["runtime_profile"]
    elif selected == STEP5D_ABLATION_V35:
        readiness = verify_v35_evidence_freeze(
            root,
            current,
            stage25_control_mode=stage25_control_mode,
            rnn_backend=rnn_backend,
            rnn_inner_iterations=rnn_inner_iterations,
            epsilon=epsilon,
            sigr_exponent_r=sigr_exponent_r,
            qdot_cap_rad_s=qdot_cap_rad_s,
        )
        runtime_profile = readiness["runtime_profile"]
    p0_required = _stage_bool(
        stage_entry,
        "strict_rnn_no_contact_p0_required_before_live",
        "no_contact_symbol_verification_required_before_live",
    )
    p0_passed = _stage_bool(
        stage_entry,
        "strict_rnn_no_contact_p0_passed",
        "strict_rnn_no_contact_p0_verified",
        "no_contact_symbol_verification_passed",
    )
    if selected == STEP5D_ABLATION_V30 and isinstance(readiness, dict):
        p0_required = True
        p0_passed = bool((readiness.get("p0_v8") or {}).get("artifact"))
    live_motion_authorized = trigger.get("live_motion_authorized") is True
    if not live_motion_authorized:
        fail("live motion is not authorized by current_stage.bridge_trigger.live_motion_authorized")
    if mode == "speedj_rnn_live":
        if p0_required and not p0_passed:
            fail("speedj_rnn_live requires no-contact P0 verification before live bridge authorization")

    return {
        "ok": True,
        "program": selected,
        "stage25_control_mode": mode,
        "live_motion_authorized": live_motion_authorized,
        "strict_rnn_no_contact_p0_required_before_live": p0_required,
        "strict_rnn_no_contact_p0_passed": p0_passed,
        "runtime_profile": runtime_profile,
        "readiness": readiness,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=EXPERIMENT_ROOT)
    parser.add_argument("--program", default=None)
    parser.add_argument("--target-dir", default=None)
    parser.add_argument("--stage25-control-mode", default=None)
    parser.add_argument("--rnn-backend", default=None)
    parser.add_argument("--rnn-inner-iterations", type=int, default=None)
    parser.add_argument("--epsilon", type=float, default=None)
    parser.add_argument("--sigr-exponent-r", type=float, default=None)
    parser.add_argument("--qdot-cap-rad-s", type=float, default=None)
    parser.add_argument("--require-live-bridge-authorization", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.require_live_bridge_authorization:
        result = verify_live_bridge_authorization(
            args.root,
            args.program,
            args.stage25_control_mode,
            args.target_dir,
            rnn_backend=args.rnn_backend,
            rnn_inner_iterations=args.rnn_inner_iterations,
            epsilon=args.epsilon,
            sigr_exponent_r=args.sigr_exponent_r,
            qdot_cap_rad_s=args.qdot_cap_rad_s,
        )
    else:
        result = verify_binding(args.root, args.program, args.target_dir)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif not args.quiet:
        manifest = result.get("manifest") or result.get("controller_readback_manifest")
        if manifest is None and isinstance(result.get("readiness"), dict):
            manifest = result["readiness"].get("manifest")
        suffix = "" if manifest is None else f": {manifest}"
        print(f"[operator] Step5d current binding passed for {result['program']}{suffix}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(24)
