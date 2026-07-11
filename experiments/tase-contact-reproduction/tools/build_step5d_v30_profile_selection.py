#!/usr/bin/env python3
"""Build the immutable 128/256/512 strict-RNN selection record.

The three inputs are diagnostic SCHED_FIFO/20 sweep artifacts.  They select a
canonical profile but do not satisfy the formal 10k-solver or 60-second timing
gates.  Selection is fail-closed: zero normal-sign mismatch is required first,
then the bounded timing checks, and timing speed only breaks ties.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "config" / "step5d_v30_profile_selection.json"
RAW_ARTIFACTS = {
    128: (
        "config/step5d_v30_rnn128_f3c36a7_profile_sweep_raw.json",
        "5127e6516c9f038d90bbf234f43d4efd9a692ab72c0e87aaf967cb3128c878af",
    ),
    256: (
        "config/step5d_v30_rnn256_f3c36a7_profile_sweep_raw.json",
        "a9f9a9d979db48ecff3e4d4d56c83236a1397d44c946455bf8a869c8f5cd4d96",
    ),
    512: (
        "config/step5d_v30_rnn512_f3c36a7_profile_sweep_raw.json",
        "1baad564d35d31a46b61ffb565f081233c57b92d09a49271275491980ca38998",
    ),
}
FIXED_PROFILE = {
    "backend": "cupy",
    "control_hz": 500.0,
    "epsilon": 0.010,
    "qdot_cap_rad_s": 0.05,
    "sigr_exponent_r": 0.8,
}
HISTORICAL_PRE_512_EVIDENCE = (
    "config/step5d_v30_rnn128_00d64f5_formal_timing_raw.json",
    "config/step5d_v30_current_source_solver_10k_raw.json",
    "config/step5d_v30_timing_summary.json",
    "config/step5d_p0_v8_offline_simulation_diagnostic.json",
    "config/step5d_p0_v8_offline_simulation_state.json",
)


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def finite_metric(mapping: Mapping[str, Any], name: str) -> float:
    value = mapping.get(name)
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"profile sweep metric is missing/nonfinite: {name}")
    return float(value)


def candidate_from_raw(
    *,
    inner_iterations: int,
    relative_path: str,
    expected_sha256: str,
) -> dict[str, Any]:
    path = ROOT / relative_path
    actual_sha256 = sha256_path(path)
    if actual_sha256 != expected_sha256:
        raise ValueError(f"profile sweep raw hash mismatch: {relative_path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    profile = payload.get("profile")
    expected_profile = {**FIXED_PROFILE, "inner_iterations": inner_iterations}
    selection = payload.get("profile_selection")
    if (
        payload.get("schema_version") != "step5d_v30_remote_timing_raw_v1"
        or profile != expected_profile
        or not isinstance(selection, dict)
        or selection.get("diagnostic_override_requested") is not True
        or selection.get("effective_profile") != expected_profile
        or selection.get("acceptance_profile_eligible") is not False
    ):
        raise ValueError(f"profile sweep binding invalid: {relative_path}")
    runtime = payload.get("runtime_environment")
    if not isinstance(runtime, dict) or (
        runtime.get("scheduler_policy_name") != "SCHED_FIFO"
        or runtime.get("scheduler_priority") != 20
    ):
        raise ValueError(f"profile sweep scheduler is not SCHED_FIFO/20: {relative_path}")
    thread_environment = runtime.get("thread_environment")
    cuda_environment = runtime.get("cuda")
    numeric_versions = runtime.get("versions")
    if (
        thread_environment
        != {
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        }
        or not isinstance(cuda_environment, dict)
        or any(
            cuda_environment.get(name) in (None, "")
            for name in ("driver_version", "runtime_version", "nvrtc_version")
        )
        or not isinstance(numeric_versions, dict)
        or any(
            numeric_versions.get(name) in (None, "", "unavailable")
            for name in ("numpy", "cupy", "pinocchio")
        )
    ):
        raise ValueError(f"profile sweep runtime environment is unbound: {relative_path}")
    source_binding = payload.get("source_binding")
    artifact_binding = payload.get("artifact_binding")
    if (
        not isinstance(source_binding, dict)
        or source_binding.get("delivery") != "stdin_bundle"
        or not isinstance(artifact_binding, dict)
    ):
        raise ValueError(f"profile sweep source/artifact binding invalid: {relative_path}")

    solver = payload.get("solver") or {}
    full_tick = payload.get("full_tick") or {}
    safe_hold = payload.get("safe_hold") or {}
    control = payload.get("full_tick_control_diagnostics") or {}
    mismatch = control.get("normal_sign_mismatch") or {}
    residual = control.get("residual_norm") or {}
    samples = int(full_tick.get("samples", 0) or 0)
    mismatch_count = int(mismatch.get("total_count", -1) or 0)
    ok_count = int((payload.get("full_tick_reason_counts") or {}).get("ok", 0) or 0)
    solver_p99 = finite_metric(solver, "p99_ms")
    full_tick_p99 = finite_metric(full_tick, "p99_ms")
    full_tick_max = finite_metric(full_tick, "max_ms")
    safe_hold_p99 = finite_metric(safe_hold, "p99_ms")
    safe_hold_max = finite_metric(safe_hold, "max_ms")
    residual_max = finite_metric(residual, "max")
    parallel_equivalence = payload.get("cupy_parallel_equivalence") or {}
    bitwise_equivalence = bool(
        parallel_equivalence.get("bitwise_equal") is True
        and parallel_equivalence.get("inner_iterations") == inner_iterations
        and int(parallel_equivalence.get("samples", 0) or 0) >= 100
        and float(parallel_equivalence.get("max_abs_difference", math.inf)) == 0.0
    )
    finite_control = bool(
        int(residual.get("nonfinite_count", 0) or 0) == 0
        and residual_max <= 1e-3
    )
    timing_pass = bool(
        int(solver.get("nonfinite_count", 0) or 0) == 0
        and int(solver.get("compute_deadline_miss_count", 0) or 0) == 0
        and solver_p99 <= 1.50
        and int(full_tick.get("nonfinite_count", 0) or 0) == 0
        and int(full_tick.get("compute_deadline_miss_count", 0) or 0) == 0
        and full_tick_p99 <= 1.80
        and full_tick_max < 2.00
        and int(safe_hold.get("nonfinite_count", 0) or 0) == 0
        and int(safe_hold.get("compute_deadline_miss_count", 0) or 0) == 0
        and safe_hold_p99 <= 1.80
        and safe_hold_max < 2.00
    )
    zero_normal_mismatch = mismatch_count == 0
    full_execute_path = bool(
        samples == 500
        and ok_count == samples
        and control.get("accepted_count") == samples
        and control.get("execute_count") == samples
        and control.get("safe_hold_count") == 0
        and control.get("execute_path_proven") is True
    )
    eligible = bool(
        zero_normal_mismatch
        and timing_pass
        and full_execute_path
        and finite_control
        and bitwise_equivalence
    )
    return {
        "inner_iterations": inner_iterations,
        "profile": expected_profile,
        "profile_sha256": canonical_sha256(expected_profile),
        "raw_artifact": relative_path,
        "raw_sha256": actual_sha256,
        "source_binding": source_binding,
        "source_binding_sha256": canonical_sha256(source_binding),
        "artifact_binding": artifact_binding,
        "artifact_binding_sha256": canonical_sha256(artifact_binding),
        "runtime_environment_sha256": canonical_sha256(runtime),
        "scheduler": {"policy": "SCHED_FIFO", "priority": 20},
        "samples": samples,
        "execute_count": ok_count,
        "normal_sign_mismatch_count": mismatch_count,
        "normal_sign_mismatch": mismatch,
        "solver_p99_ms": solver_p99,
        "full_tick_p99_ms": full_tick_p99,
        "full_tick_max_ms": full_tick_max,
        "full_tick_deadline_miss_count": int(
            full_tick.get("compute_deadline_miss_count", 0) or 0
        ),
        "residual_max": residual_max,
        "safe_hold_p99_ms": safe_hold_p99,
        "safe_hold_max_ms": safe_hold_max,
        "safe_hold_deadline_miss_count": int(
            safe_hold.get("compute_deadline_miss_count", 0) or 0
        ),
        "bitwise_parallel_equivalence": bitwise_equivalence,
        "finite_residual_within_gate": finite_control,
        "zero_normal_sign_mismatch": zero_normal_mismatch,
        "timing_pass": timing_pass,
        "full_execute_path": full_execute_path,
        "selection_eligible": eligible,
    }


def select_candidate(candidates: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    eligible = [candidate for candidate in candidates if candidate.get("selection_eligible")]
    if not eligible:
        raise ValueError("no profile has zero normal mismatch plus passing timing")
    return min(
        eligible,
        key=lambda candidate: (
            int(candidate["inner_iterations"]),
            float(candidate["full_tick_p99_ms"]),
            float(candidate["full_tick_max_ms"]),
        ),
    )


def build() -> dict[str, Any]:
    candidates = [
        candidate_from_raw(
            inner_iterations=inner_iterations,
            relative_path=relative_path,
            expected_sha256=expected_sha256,
        )
        for inner_iterations, (relative_path, expected_sha256) in sorted(
            RAW_ARTIFACTS.items()
        )
    ]
    common_source_sha = {candidate["source_binding_sha256"] for candidate in candidates}
    common_artifact_sha = {
        candidate["artifact_binding_sha256"] for candidate in candidates
    }
    common_runtime_sha = {
        candidate["runtime_environment_sha256"] for candidate in candidates
    }
    if (
        len(common_source_sha) != 1
        or len(common_artifact_sha) != 1
        or len(common_runtime_sha) != 1
    ):
        raise ValueError(
            "profile sweep candidates do not share one source/artifact/runtime binding"
        )
    selected = select_candidate(candidates)
    historical = []
    for relative_path in HISTORICAL_PRE_512_EVIDENCE:
        path = ROOT / relative_path
        if not path.is_file():
            raise ValueError(f"historical pre-512 evidence is missing: {relative_path}")
        historical.append(
            {
                "path": relative_path,
                "sha256": sha256_path(path),
                "status": "historical_superseded_by_v30_rnn512_profile_selection",
                "immutable": True,
            }
        )
    return {
        "schema_version": "step5d_v30_profile_selection_v1",
        "classification": "diagnostic_selection_not_formal_timing",
        "mode": "diagnostic_profile_selection_not_formal_timing_acceptance",
        "selection_rule": {
            "primary": "zero_normal_sign_mismatch_required",
            "secondary": (
                "solver_p99<=1.50ms and full_tick_p99<=1.80ms and "
                "safe_hold_p99<=1.80ms and full/safe max<2.00ms, zero compute "
                "deadline miss, and bitwise parallel equivalence"
            ),
            "tie_break": "minimum inner_iterations, then full_tick_p99, then max",
            "forbidden": "do_not_select_fastest_profile_when_normal_sign_mismatch_is_nonzero",
        },
        "fixed_contract": {
            **FIXED_PROFILE,
            "dls_runtime_fallback_allowed": False,
            "safety_envelope_unchanged": True,
            "normal_sign_guard_fail_closed": True,
            "runtime_scheduler": {"policy": "SCHED_FIFO", "priority": 20},
        },
        "candidates": candidates,
        "selected_inner_iterations": int(selected["inner_iterations"]),
        "selected_profile": selected["profile"],
        "selected_profile_sha256": selected["profile_sha256"],
        "selected_raw_artifact": selected["raw_artifact"],
        "selected_raw_sha256": selected["raw_sha256"],
        "decision": (
            "512 is the only candidate with 0/500 normal-sign mismatch and a "
            "500/500 execute path; its solver/full-tick timing also passes the "
            "diagnostic thresholds"
        ),
        "common_source_binding_sha256": next(iter(common_source_sha)),
        "common_artifact_binding_sha256": next(iter(common_artifact_sha)),
        "common_runtime_environment_sha256": next(iter(common_runtime_sha)),
        "historical_pre_512_evidence": historical,
        "formal_evidence_required": {
            "solver_samples": 10_000,
            "full_tick_samples": 30_000,
            "safe_hold_samples": 30_000,
            "paced_500hz": True,
            "scheduler": {"policy": "SCHED_FIFO", "priority": 20},
            "status": "not_satisfied_by_500_tick_profile_sweep",
        },
        "claim_boundary": {
            "canonical_profile_selected": True,
            "v30_offline_ready": False,
            "p0_passed": False,
            "live_motion_authorized": False,
            "reproduction_complete": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = build()
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
