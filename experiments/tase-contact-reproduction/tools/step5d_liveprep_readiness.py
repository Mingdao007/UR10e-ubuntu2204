#!/usr/bin/env python3
"""Build offline-only Step5d v29 timing, DLS-shadow, and readiness evidence.

This tool never opens RTDE, never writes controller files, and never invokes
``zero_ftsensor``.  Its DLS calculation is diagnostic-only and cannot be used
as a runtime command fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_CONTRACT = Path("config/step5d_v29_liveprep_benchmark.json")
READINESS_SCHEMA = "step5d_liveprep_readiness_v1"
REVIEW_SOURCE_FILES = (
    "config/current_stage.json",
    "config/step5_stage_table.json",
    "config/step5d_v29_liveprep_benchmark.json",
    "config/step5d_liveprep_solver_gate.json",
    "tools/step5c_strict_rnn.py",
    "tools/step5d_liveprep_readiness.py",
    "tools/step5d_runtime_interface.py",
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _finite_vector(values: Any, length: int, name: str) -> np.ndarray:
    vector = np.asarray(values, dtype=float)
    if vector.shape != (length,) or not np.all(np.isfinite(vector)):
        raise ValueError(f"{name} must be a finite length-{length} vector")
    return vector


def _finite_matrix(values: Any, shape: tuple[int, int], name: str) -> np.ndarray:
    matrix = np.asarray(values, dtype=float)
    if matrix.shape != shape or not np.all(np.isfinite(matrix)):
        raise ValueError(f"{name} must be a finite {shape[0]}x{shape[1]} matrix")
    return matrix


def _percentile_ms(values: list[float], percentile: float) -> float:
    if not values:
        return math.inf
    return float(np.percentile(np.asarray(values, dtype=float), percentile))


def load_benchmark_contract(root: Path = EXPERIMENT_ROOT) -> dict[str, Any]:
    payload = _load_json(root / BENCHMARK_CONTRACT)
    if payload.get("schema_version") != "step5d_v29_liveprep_benchmark_v1":
        raise ValueError("unsupported Step5d live-prep benchmark schema")
    _finite_matrix(payload.get("jacobian_base_tcp"), (6, 6), "jacobian_base_tcp")
    _finite_vector(payload.get("approach_normal_base"), 3, "approach_normal_base")
    _finite_vector(payload.get("representative_xdot_c"), 6, "representative_xdot_c")
    return payload


def derive_workflow_state(
    *,
    package_ready: bool,
    offline_ready: bool,
    live_motion_authorized: bool,
    bridge_has_started: bool,
    live_run_state: str,
    reproduction_state: str,
) -> str:
    """Derive the claim-safe state without collapsing package/live/reproduction."""
    if reproduction_state == "complete":
        return "reproduction_complete"
    if live_run_state == "accepted":
        return "live_accepted"
    if bridge_has_started or live_run_state == "running":
        return "live_running"
    if live_motion_authorized:
        return "live_authorized" if package_ready and offline_ready else "liveprep_blocked"
    if package_ready and offline_ready:
        return "awaiting_live_authorization"
    return "liveprep_blocked"


def evaluate_offline_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    blockers: list[str] = []
    if evidence.get("runtime_profile_match") is not True:
        blockers.append("runtime_profile_mismatch")
    timing = evidence.get("timing") if isinstance(evidence.get("timing"), Mapping) else {}
    for key, blocker in (
        ("microbenchmark", "solver_microbenchmark_failed"),
        ("synthetic_tick", "synthetic_tick_timing_failed"),
        ("safe_hold", "safe_hold_timing_failed"),
    ):
        item = timing.get(key) if isinstance(timing, Mapping) else None
        if not isinstance(item, Mapping) or item.get("pass") is not True:
            blockers.append(blocker)
    shadow = evidence.get("dls_shadow") if isinstance(evidence.get("dls_shadow"), Mapping) else {}
    if shadow.get("pass") is not True and shadow.get("ok") is not True:
        blockers.append("dls_shadow_failed")
    if shadow.get("diagnostic_only") is not True or shadow.get("runtime_fallback_allowed") is not False:
        blockers.append("dls_shadow_boundary_invalid")
    if evidence.get("controller_readback_verified") is not True:
        blockers.append("controller_readback_not_verified")
    if evidence.get("package_hashes_match") is not True:
        blockers.append("package_hash_mismatch")
    review = evidence.get("review") if isinstance(evidence.get("review"), Mapping) else {}
    if review.get("ok") is not True:
        blockers.append("milestone_review_not_accepted")
    return {"pass": not blockers, "ok": not blockers, "blockers": blockers}


def build_dls_shadow(
    *,
    jacobian: Any,
    xdot_c: Any,
    qdot_rnn: Any,
    omega_minus: Any,
    omega_plus: Any,
    approach_normal: Any,
    damping: float = 1e-4,
) -> dict[str, Any]:
    """Return an independent DLS diagnostic; never a command source."""
    J = _finite_matrix(jacobian, (6, 6), "jacobian")
    xdot = _finite_vector(xdot_c, 6, "xdot_c")
    rnn = _finite_vector(qdot_rnn, 6, "qdot_rnn")
    lower = _finite_vector(omega_minus, 6, "omega_minus")
    upper = _finite_vector(omega_plus, 6, "omega_plus")
    normal = _finite_vector(approach_normal, 3, "approach_normal")
    if np.any(lower > upper):
        raise ValueError("omega_minus must be <= omega_plus")
    if not math.isfinite(float(damping)) or damping <= 0.0:
        raise ValueError("damping must be finite and positive")
    normal_norm = float(np.linalg.norm(normal))
    if normal_norm <= 0.0:
        raise ValueError("approach_normal must be nonzero")
    normal = normal / normal_norm
    lhs = J @ J.T + float(damping) ** 2 * np.eye(6)
    qdot_unclipped = J.T @ np.linalg.solve(lhs, xdot)
    qdot_dls = np.clip(qdot_unclipped, lower, upper)
    twist_dls = J @ qdot_dls
    twist_rnn = J @ rnn
    desired_normal = float(np.dot(xdot[:3], normal))
    dls_normal = float(np.dot(twist_dls[:3], normal))
    rnn_normal = float(np.dot(twist_rnn[:3], normal))
    sign_tolerance = 1e-9

    def same_nonzero_sign(reference: float, candidate: float) -> bool:
        if abs(reference) <= sign_tolerance:
            return abs(candidate) <= sign_tolerance
        return reference * candidate > 0.0

    normal_sign_consistent = same_nonzero_sign(desired_normal, rnn_normal)
    finite = bool(np.all(np.isfinite(qdot_dls)) and np.all(np.isfinite(twist_dls)))
    dls_bounds_ok = bool(np.all(qdot_dls >= lower - 1e-9) and np.all(qdot_dls <= upper + 1e-9))
    bounds_ok = bool(np.all(rnn >= lower - 1e-9) and np.all(rnn <= upper + 1e-9))
    dls_sign_consistent = same_nonzero_sign(desired_normal, dls_normal)
    passed = finite and dls_bounds_ok and bounds_ok and dls_sign_consistent and normal_sign_consistent
    return {
        "ok": passed,
        "pass": passed,
        "diagnostic_only": True,
        "runtime_fallback_allowed": False,
        "damping": float(damping),
        "qdot_dls": qdot_dls.tolist(),
        "qdot_rnn": rnn.tolist(),
        "twist_dls": twist_dls.tolist(),
        "twist_rnn": twist_rnn.tolist(),
        "dls_residual_norm": float(np.linalg.norm(twist_dls - xdot)),
        "rnn_residual_norm": float(np.linalg.norm(twist_rnn - xdot)),
        "qdot_delta_norm": float(np.linalg.norm(rnn - qdot_dls)),
        "dls_saturated_mask": np.logical_or(qdot_unclipped < lower, qdot_unclipped > upper).tolist(),
        "dls_bounds_ok": dls_bounds_ok,
        "rnn_bounds_ok": bounds_ok,
        "desired_approach_normal_m_s": desired_normal,
        "dls_approach_normal_m_s": dls_normal,
        "rnn_approach_normal_m_s": rnn_normal,
        "dls_normal_sign_consistent": dls_sign_consistent,
        "normal_sign_consistent": normal_sign_consistent,
    }


def run_synthetic_tick(
    solver: Any,
    *,
    jacobian: Any,
    xdot_c: Any,
    omega_minus: Any,
    omega_plus: Any,
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Run one offline compute tick and fail closed to zero qdot."""
    started = time.perf_counter()
    zero = np.zeros(6, dtype=float)
    try:
        J = _finite_matrix(jacobian, (6, 6), "jacobian")
        xdot = _finite_vector(xdot_c, 6, "xdot_c")
        lower = _finite_vector(omega_minus, 6, "omega_minus")
        upper = _finite_vector(omega_plus, 6, "omega_plus")
        if np.any(lower > upper):
            raise ValueError("invalid_bounds")
    except (TypeError, ValueError):
        reason = "nonfinite_input" if not np.all(np.isfinite(np.asarray(xdot_c, dtype=float))) else "invalid_input"
        return {"accepted": False, "reason": reason, "qdot": zero.tolist(), "wall_ms": (time.perf_counter() - started) * 1000.0}
    try:
        result = solver.solve(
            actual_q=np.zeros(6),
            actual_qd=np.zeros(6),
            target_state={
                "J": J,
                "xdot_c": xdot,
                "omega_minus": lower,
                "omega_plus": upper,
                "dt": 0.002,
                "epsilon": float(profile["epsilon"]),
                "r": float(profile["sigr_exponent_r"]),
                "cmd_valid": True,
            },
        )
        qdot = _finite_vector(result.qdot, 6, "solver qdot")
        cap = float(profile["qdot_cap_rad_s"])
        if np.any(qdot < lower - 1e-9) or np.any(qdot > upper + 1e-9) or np.max(np.abs(qdot)) > cap + 1e-9:
            raise ValueError("qdot_out_of_bounds")
    except Exception as exc:  # solver errors must never leak a prior command
        return {
            "accepted": False,
            "reason": "qdot_out_of_bounds" if str(exc) == "qdot_out_of_bounds" else "solver_rejected",
            "qdot": zero.tolist(),
            "wall_ms": (time.perf_counter() - started) * 1000.0,
        }
    return {
        "accepted": True,
        "reason": "accepted",
        "qdot": qdot.tolist(),
        "residual_norm": float(result.residual_norm),
        "solver_wall_ms": float(result.diagnostics.get("solve_wall_ms", math.nan)),
        "wall_ms": (time.perf_counter() - started) * 1000.0,
    }


def _new_solver(root: Path, profile: Mapping[str, Any]) -> tuple[StrictTaseRnnSolver, float]:
    started = time.perf_counter()
    solver = StrictTaseRnnSolver(
        StrictRnnConfig(
            paper_truth_path=root / "config" / "step5d_liveprep_solver_gate.json",
            qdot_limit_rad_s=float(profile["qdot_cap_rad_s"]),
            epsilon=float(profile["epsilon"]),
            sigr_exponent_r=float(profile["sigr_exponent_r"]),
            inner_iterations=int(profile["inner_iterations"]),
            backend=str(profile["backend"]),
        )
    )
    return solver, (time.perf_counter() - started) * 1000.0


def run_offline_benchmark(root: Path, contract: Mapping[str, Any], *, quick: bool = False) -> dict[str, Any]:
    profile = contract["runtime_profile"]
    thresholds = contract["thresholds"]
    J = _finite_matrix(contract["jacobian_base_tcp"], (6, 6), "jacobian_base_tcp")
    xdot = _finite_vector(contract["representative_xdot_c"], 6, "representative_xdot_c")
    cap = float(profile["qdot_cap_rad_s"])
    lower = np.full(6, -cap)
    upper = np.full(6, cap)
    solver, precompile_ms = _new_solver(root, profile)
    solver.warm_start(J=J, xdot_c=xdot, omega_minus=lower, omega_plus=upper)

    first = run_synthetic_tick(
        solver, jacobian=J, xdot_c=xdot, omega_minus=lower, omega_plus=upper, profile=profile
    )
    solver_samples = min(100, int(thresholds["solver_samples"])) if quick else int(thresholds["solver_samples"])
    deadline_ms = float(thresholds["deadline_ms"])
    solver_ms: list[float] = []
    solver_rejections: list[str] = []
    last_tick = first
    last_accepted_qdot = list(first["qdot"]) if first.get("accepted") is True else [0.0] * 6
    for _ in range(solver_samples):
        last_tick = run_synthetic_tick(
            solver, jacobian=J, xdot_c=xdot, omega_minus=lower, omega_plus=upper, profile=profile
        )
        if last_tick.get("accepted") is True:
            solver_ms.append(float(last_tick.get("solver_wall_ms", deadline_ms)))
            last_accepted_qdot = list(last_tick["qdot"])
        else:
            solver_ms.append(deadline_ms)
            solver_rejections.append(str(last_tick.get("reason", "unknown")))
    micro = {
        "samples": solver_samples,
        "precompile_ms": precompile_ms,
        "precompile_outside_loop": True,
        "first_post_warm_ms": float(first.get("solver_wall_ms", deadline_ms)),
        "p50_ms": statistics.median(solver_ms),
        "p99_ms": _percentile_ms(solver_ms, 99.0),
        "max_ms": max(solver_ms),
        "accepted_count": solver_samples - len(solver_rejections),
        "rejected_count": len(solver_rejections),
        "rejection_reasons": sorted(set(solver_rejections)),
        "deadline_miss_count": sum(value >= deadline_ms for value in solver_ms),
    }
    micro["pass"] = bool(
        first.get("accepted") is True
        and not solver_rejections
        and micro["first_post_warm_ms"] <= float(thresholds["first_post_warm_max_ms"])
        and micro["p99_ms"] <= float(thresholds["solver_p99_max_ms"])
        and micro["deadline_miss_count"] == 0
    )

    synthetic_samples = (
        min(200, int(float(thresholds["synthetic_duration_s"]) * float(thresholds["synthetic_frequency_hz"])))
        if quick
        else int(float(thresholds["synthetic_duration_s"]) * float(thresholds["synthetic_frequency_hz"]))
    )
    synthetic_ms: list[float] = []
    accepted_count = 0
    schedule_overrun_count = 0
    max_schedule_lateness_ms = 0.0
    frequency_hz = float(thresholds["synthetic_frequency_hz"])
    period_s = 1.0 / frequency_hz
    synthetic_started = time.perf_counter()
    for index in range(synthetic_samples):
        release = time.perf_counter() if quick else synthetic_started + index * period_s
        if not quick:
            while True:
                remaining = release - time.perf_counter()
                if remaining <= 0.0:
                    break
                if remaining > 0.0002:
                    time.sleep(remaining - 0.0001)
        tick = run_synthetic_tick(
            solver, jacobian=J, xdot_c=xdot, omega_minus=lower, omega_plus=upper, profile=profile
        )
        synthetic_ms.append(float(tick["wall_ms"]))
        accepted_count += int(tick["accepted"] is True)
        if tick.get("accepted") is True:
            last_accepted_qdot = list(tick["qdot"])
        finished = time.perf_counter()
        lateness_ms = 0.0 if quick else max(0.0, (finished - (release + period_s)) * 1000.0)
        max_schedule_lateness_ms = max(max_schedule_lateness_ms, lateness_ms)
        schedule_overrun_count += int(lateness_ms > 0.0)
        last_tick = tick
    if not quick:
        end_deadline = synthetic_started + float(thresholds["synthetic_duration_s"])
        remaining = end_deadline - time.perf_counter()
        if remaining > 0.0:
            time.sleep(remaining)
    synthetic_elapsed_s = time.perf_counter() - synthetic_started
    compute_deadline_miss_count = sum(value >= deadline_ms for value in synthetic_ms)
    synthetic = {
        "samples": synthetic_samples,
        "duration_s": synthetic_elapsed_s,
        "requested_duration_s": float(thresholds["synthetic_duration_s"]),
        "frequency_hz": frequency_hz,
        "deadline_paced": not quick,
        "accepted_count": accepted_count,
        "p50_ms": statistics.median(synthetic_ms),
        "p99_ms": _percentile_ms(synthetic_ms, 99.0),
        "max_ms": max(synthetic_ms),
        "compute_deadline_miss_count": compute_deadline_miss_count,
        "schedule_overrun_count": schedule_overrun_count,
        "max_schedule_lateness_ms": max_schedule_lateness_ms,
        "deadline_miss_count": compute_deadline_miss_count + (0 if quick else schedule_overrun_count),
    }
    synthetic["pass"] = bool(
        accepted_count == synthetic_samples
        and (quick or synthetic_elapsed_s >= float(thresholds["synthetic_duration_s"]))
        and synthetic["p99_ms"] <= float(thresholds["synthetic_p99_max_ms"])
        and synthetic["deadline_miss_count"] == 0
    )

    safe_samples = min(100, int(thresholds["safe_hold_samples"])) if quick else int(thresholds["safe_hold_samples"])
    safe_ms: list[float] = []
    safe_zero_count = 0
    for _ in range(safe_samples):
        tick = run_synthetic_tick(
            solver,
            jacobian=J,
            xdot_c=np.array([math.nan, 0.0, 0.0, 0.0, 0.0, 0.0]),
            omega_minus=lower,
            omega_plus=upper,
            profile=profile,
        )
        safe_ms.append(float(tick["wall_ms"]))
        safe_zero_count += int(not tick["accepted"] and np.allclose(tick["qdot"], np.zeros(6)))
    safe_hold = {
        "samples": safe_samples,
        "zero_qdot_count": safe_zero_count,
        "p99_ms": _percentile_ms(safe_ms, 99.0),
        "max_ms": max(safe_ms),
        "deadline_miss_count": sum(value >= float(thresholds["deadline_ms"]) for value in safe_ms),
    }
    safe_hold["pass"] = bool(
        safe_zero_count == safe_samples
        and safe_hold["p99_ms"] <= float(thresholds["safe_hold_p99_max_ms"])
        and safe_hold["deadline_miss_count"] == 0
    )
    timing_ok = bool(micro["pass"] and synthetic["pass"] and safe_hold["pass"])
    timing = {
        "ok": timing_ok,
        "quick_mode": bool(quick),
        "microbenchmark": micro,
        "synthetic_tick": synthetic,
        "safe_hold": safe_hold,
    }
    shadow = build_dls_shadow(
        jacobian=J,
        xdot_c=xdot,
        qdot_rnn=last_accepted_qdot,
        omega_minus=lower,
        omega_plus=upper,
        approach_normal=contract["approach_normal_base"],
    )
    return {"timing": timing, "dls_shadow": shadow}


def _review_file_matches(
    raw_path: Any,
    expected_sha256: Any,
    *,
    manifest_dir: Path | None,
) -> bool:
    if manifest_dir is None or not isinstance(raw_path, str) or not raw_path:
        return False
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        return False
    base = manifest_dir.resolve()
    candidate = Path(raw_path)
    candidate = (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return False
    return candidate.is_file() and not candidate.is_symlink() and _sha256(candidate) == expected_sha256


def validate_review_manifest(
    payload: Mapping[str, Any],
    *,
    artifact_sha256: str | None = None,
    manifest_dir: Path | None = None,
) -> dict[str, Any]:
    """Validate the saved three-lane milestone review instead of trusting ``ok`` alone."""
    required_lanes = {"control_claim", "timing_runtime", "physical_operator_safety"}
    lanes = payload.get("lanes") if isinstance(payload.get("lanes"), list) else []
    lane_ids = {str(lane.get("id")) for lane in lanes if isinstance(lane, Mapping)}
    lane_results_ok = bool(
        lane_ids == required_lanes
        and all(
            isinstance(lane, Mapping)
            and lane.get("result") == "accepted"
            and isinstance(lane.get("artifact"), str)
            and bool(lane.get("artifact"))
            and isinstance(lane.get("artifact_sha256"), str)
            and len(str(lane.get("artifact_sha256"))) == 64
            and lane.get("model") == "gpt-5.6-sol"
            and lane.get("reasoning_effort") == "max"
            and _review_file_matches(
                lane.get("artifact"),
                lane.get("artifact_sha256"),
                manifest_dir=manifest_dir,
            )
            and _review_file_matches(
                lane.get("runtime_evidence"),
                lane.get("runtime_evidence_sha256"),
                manifest_dir=manifest_dir,
            )
            for lane in lanes
        )
    )
    source_hash = payload.get("reviewed_source_sha256")
    source_bound = isinstance(source_hash, str) and len(source_hash) == 64
    ok = bool(
        payload.get("schema_version") == "step5d_liveprep_milestone_review_v1"
        and payload.get("ok") is True
        and lane_results_ok
        and source_bound
    )
    return {
        "ok": ok,
        "schema_version": payload.get("schema_version"),
        "reviewed_source_sha256": source_hash,
        "lanes": lanes,
        "artifact_sha256": artifact_sha256,
        "reason": "accepted" if ok else "invalid_or_incomplete_three_lane_milestone_review",
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reviewed_source_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for relative in REVIEW_SOURCE_FILES:
        path = root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def package_evidence(
    root: Path,
    current: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    readback_manifest_path: Path | None = None,
) -> dict[str, Any]:
    expected = dict(current.get("sha256") or {})
    delivery = row.get("package_delivery") if isinstance(row.get("package_delivery"), Mapping) else {}
    local = row.get("local_delivery_evidence") if isinstance(row.get("local_delivery_evidence"), Mapping) else {}
    local_base = root / "programs" / "step5" / "step5d" / str(current.get("program"))
    observed: dict[str, str | None] = {}
    for extension in (".script", ".txt", ".urp"):
        path = local_base.with_suffix(extension)
        observed[extension] = _sha256(path) if path.is_file() else None
    hashes_match = bool(expected and observed == expected and delivery.get("sha256") == expected and local.get("sha256") == expected)
    pointer = str(current.get("controller_readback_manifest") or "")
    manifest_path = readback_manifest_path or (root / pointer)
    manifest: dict[str, Any] = {}
    try:
        manifest = _load_json(manifest_path)
    except (OSError, json.JSONDecodeError):
        manifest = {}
    manifest_hashes = manifest.get("sha256") if isinstance(manifest.get("sha256"), Mapping) else {}
    manifest_verified = bool(
        manifest.get("status") == "controller read-back verified"
        and manifest.get("readback_source") == "fresh_controller_get"
        and manifest.get("validation", {}).get("program") == current.get("program")
        and manifest.get("target_resolution", {}).get("controller_target") == current.get("controller_target")
        and all(manifest_hashes.get(channel) == expected for channel in ("local", "controller", "readback"))
    )
    readback_verified = bool(
        row.get("acceptance", {}).get("controller_readback_verified") is True
        and delivery.get("controller_readback_status") == "verified_current"
        and local.get("controller_readback_verified") is True
        and current.get("v29_contact_candidate", {}).get("controller_readback_verified") is True
        and manifest_verified
    )
    return {
        "controller_readback_verified": readback_verified,
        "package_hashes_match": hashes_match,
        "package_sha256": expected,
        "observed_local_sha256": observed,
        "controller_readback_manifest": current.get("controller_readback_manifest"),
        "controller_readback_manifest_path": str(manifest_path),
        "controller_readback_manifest_sha256": _sha256(manifest_path) if manifest else None,
        "controller_readback_manifest_verified": manifest_verified,
    }


def build_readiness(
    root: Path,
    *,
    benchmark: Mapping[str, Any],
    benchmark_result: Mapping[str, Any],
    review: Mapping[str, Any],
    readback_manifest_path: Path | None = None,
) -> dict[str, Any]:
    current = _load_json(root / "config" / "current_stage.json")
    table = _load_json(root / "config" / "step5_stage_table.json")
    row = next(row for row in table.get("stages", []) if row.get("id") == current.get("current_stage_id"))
    profile = dict(benchmark["runtime_profile"])
    runtime_profile_match = row.get("runtime_profile") == profile
    package = package_evidence(root, current, row, readback_manifest_path=readback_manifest_path)
    timing = dict(benchmark_result["timing"])
    dls_shadow = dict(benchmark_result["dls_shadow"])
    expected_review_source = reviewed_source_sha256(root)
    bound_review = dict(review)
    bound_review["expected_source_sha256"] = expected_review_source
    if bound_review.get("reviewed_source_sha256") != expected_review_source:
        bound_review["ok"] = False
        bound_review["reason"] = "reviewed_source_sha256_mismatch"
    evidence = {
        "runtime_profile_match": runtime_profile_match,
        "timing": timing,
        "dls_shadow": dls_shadow,
        "review": bound_review,
        **package,
    }
    evaluation = evaluate_offline_evidence(evidence)
    live_authorized = current.get("bridge_trigger", {}).get("live_motion_authorized") is True
    bridge_started = current.get("bridge_trigger", {}).get("bridge_has_started") is True
    workflow_state = derive_workflow_state(
        package_ready=bool(package["controller_readback_verified"] and package["package_hashes_match"]),
        offline_ready=evaluation["pass"],
        live_motion_authorized=live_authorized,
        bridge_has_started=bridge_started,
        live_run_state=str(current.get("live_run_status", {}).get("state", "not_started")),
        reproduction_state=str(current.get("reproduction_status", {}).get("state", "incomplete")),
    )
    ready = workflow_state == "awaiting_live_authorization"
    return {
        "schema_version": READINESS_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile": str(current.get("program")),
        "program": str(current.get("program")),
        "workflow_state": workflow_state,
        "ready_for_explicit_live_authorization": ready,
        "live_motion_authorized": live_authorized,
        "bridge_has_started": bridge_started,
        "package_sha256": package["package_sha256"],
        "runtime_profile": profile,
        "runtime_profile_match": runtime_profile_match,
        "timing": timing,
        "dls_shadow": dls_shadow,
        "review": bound_review,
        "controller_readback_verified": package["controller_readback_verified"],
        "controller_readback_manifest": package["controller_readback_manifest"],
        "controller_readback_manifest_sha256": package["controller_readback_manifest_sha256"],
        "package_hashes_match": package["package_hashes_match"],
        "blockers": evaluation["blockers"],
        "claims": {
            "package_accepted": bool(package["controller_readback_verified"] and package["package_hashes_match"]),
            "live_run_accepted": False,
            "reproduction_complete": False,
        },
        "host": {"node": platform.node(), "platform": platform.platform(), "python": platform.python_version()},
        "safety_boundary": list(benchmark.get("safety_boundary", [])),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=EXPERIMENT_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review-manifest", type=Path)
    parser.add_argument("--readback-manifest", type=Path, help="Read-only v29 controller readback manifest override")
    parser.add_argument("--quick", action="store_true", help="Smoke-test sample counts; never produces ready=true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    contract = load_benchmark_contract(root)
    review: dict[str, Any] = {"ok": False, "reason": "milestone_review_manifest_missing"}
    if args.review_manifest:
        review = validate_review_manifest(
            _load_json(args.review_manifest),
            artifact_sha256=_sha256(args.review_manifest),
            manifest_dir=args.review_manifest.parent,
        )
    benchmark_result = run_offline_benchmark(root, contract, quick=args.quick)
    readiness = build_readiness(
        root,
        benchmark=contract,
        benchmark_result=benchmark_result,
        review=review,
        readback_manifest_path=args.readback_manifest,
    )
    if args.quick:
        readiness["ready_for_explicit_live_authorization"] = False
        readiness["workflow_state"] = "liveprep_blocked"
        if "quick_mode_not_acceptance" not in readiness["blockers"]:
            readiness["blockers"].append("quick_mode_not_acceptance")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(readiness, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(readiness, indent=2, sort_keys=True))
    return 0 if readiness["ready_for_explicit_live_authorization"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
