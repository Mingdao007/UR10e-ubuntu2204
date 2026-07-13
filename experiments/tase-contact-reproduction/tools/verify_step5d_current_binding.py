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
from verify_current_stage_readback import EXPERIMENT_ROOT, fail, load_json, verify


STEP5D_PACKAGE_PREFIXES = ("step5d_strict_rnn_liveprep_", "step5d_strict_rnn_ablation_")
STEP5D_ABLATION_V29 = "step5d_strict_rnn_ablation_v29"
STEP5D_ABLATION_V30 = "step5d_strict_rnn_ablation_v30"
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
    if raw.get("paced_500hz") is not True:
        fail("v30 timing raw evidence is not paced at 500 Hz")
    solver = raw.get("solver")
    full_tick = raw.get("full_tick")
    safe_hold = raw.get("safe_hold")
    if not all(isinstance(item, dict) for item in (solver, full_tick, safe_hold)):
        fail("v30 timing raw evidence is missing solver/full_tick/safe_hold sections")
    assert isinstance(solver, dict) and isinstance(full_tick, dict) and isinstance(safe_hold, dict)

    if _finite_number(solver.get("samples"), "solver.samples") < 10_000:
        fail("v30 timing solver evidence has fewer than 10,000 samples")
    first_post_warm = raw.get("first_post_warm_ms", solver.get("first_post_warm_ms"))
    if _finite_number(first_post_warm, "solver.first_post_warm_ms") > 1.75:
        fail("v30 timing solver first post-warm exceeds 1.75 ms")
    if _finite_number(solver.get("p99_ms"), "solver.p99_ms") > 1.50:
        fail("v30 timing solver p99 exceeds 1.50 ms")
    if _finite_number(solver.get("max_ms"), "solver.max_ms") >= 2.0:
        fail("v30 timing solver max reaches the 2.00 ms deadline")
    _zero_counter(solver, "solver", "compute_deadline_miss_count", "deadline_miss_count")
    _zero_counter(solver, "solver", "nonfinite_count")

    if _finite_number(full_tick.get("samples"), "full_tick.samples") < 30_000:
        fail("v30 timing full tick evidence has fewer than 30,000 samples")
    if _finite_number(raw.get("elapsed_full_tick_wall_s"), "elapsed_full_tick_wall_s") < 60.0:
        fail("v30 timing full tick evidence is shorter than 60 seconds")
    if _finite_number(full_tick.get("p99_ms"), "full_tick.p99_ms") > 1.80:
        fail("v30 timing full tick p99 exceeds 1.80 ms")
    if _finite_number(full_tick.get("max_ms"), "full_tick.max_ms") >= 2.0:
        fail("v30 timing full tick max reaches the 2.00 ms deadline")
    _zero_counter(full_tick, "full_tick", "compute_deadline_miss_count", "deadline_miss_count")
    _zero_counter(full_tick, "full_tick", "nonfinite_count")
    _zero_counter(raw, "full_tick_schedule", "full_tick_schedule_deadline_miss_count")

    if _finite_number(safe_hold.get("samples"), "safe_hold.samples") < 30_000:
        fail("v30 timing safe-hold evidence has fewer than 30,000 samples")
    if _finite_number(raw.get("elapsed_safe_hold_wall_s"), "elapsed_safe_hold_wall_s") < 60.0:
        fail("v30 timing safe-hold evidence is shorter than 60 seconds")
    if _finite_number(safe_hold.get("p99_ms"), "safe_hold.p99_ms") >= 2.0:
        fail("v30 timing safe-hold p99 reaches the 2.00 ms deadline")
    if _finite_number(safe_hold.get("max_ms"), "safe_hold.max_ms") >= 2.0:
        fail("v30 timing safe-hold max reaches the 2.00 ms deadline")
    _zero_counter(safe_hold, "safe_hold", "compute_deadline_miss_count", "deadline_miss_count")
    _zero_counter(safe_hold, "safe_hold", "nonfinite_count")
    _zero_counter(raw, "safe_hold_schedule", "safe_hold_schedule_deadline_miss_count")
    return {
        "path": _relative(root, raw_path),
        "sha256": raw_sha256,
        "solver_samples": int(_finite_number(solver.get("samples"), "solver.samples")),
        "full_tick_samples": int(_finite_number(full_tick.get("samples"), "full_tick.samples")),
        "safe_hold_samples": int(_finite_number(safe_hold.get("samples"), "safe_hold.samples")),
    }


def _verify_v30_review_v3(
    root: Path,
    stage_entry: dict[str, Any],
    readiness_review: dict[str, Any],
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
    resolved = resolve_review_v3(
        workflow="v30",
        milestone="contact_pre_live",
        gate={"evidence_frozen": True, "composite_fingerprint": composite},
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
        fail("v30 promotion requires P0 v8 final continuous 60 second pass")
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
        or not math.isclose(_finite_number(p0_artifact.get("phase_s"), "P0 v8 phase_s"), 60.0)
        or p0_artifact.get("blockers") != []
    ):
        fail("v30 promotion requires a clean final continuous 60 second P0 v8 artifact")
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
    review = _verify_v30_review_v3(root, stage_entry, readiness.get("review_v3") or {})
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
            "joint_layout_code": 524.0,
        }
    except (TypeError, ValueError):
        fail(f"{profile_label} live bridge requires the exact runtime profile")
    expected = (
        V30_EXACT_RUNTIME_PROFILE
        if profile_label == "v30"
        else V29_EXACT_RUNTIME_PROFILE
    )
    if observed != expected:
        fail(
            f"{profile_label} live bridge requires the exact runtime profile "
            + (
                "speedj_rnn_live/cupy/512/epsilon=0.01/r=0.8/qdot=0.05"
                if profile_label == "v30"
                else "speedj_rnn_live/cupy/1024/epsilon=0.01/r=0.8/qdot=0.05"
            )
        )
    return observed


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
    else:
        print(f"[operator] Step5d current binding passed for {result['program']}: {result['manifest']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(24)
