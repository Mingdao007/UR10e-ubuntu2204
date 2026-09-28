#!/usr/bin/env python3
"""I-1 fixed-input NumPy/native 512 replay; no robot endpoint is opened.

Run with the runtime owner's tools first on PYTHONPATH and this topic's tools
second. Both candidates use the same replay entry and runtime.command timing
boundary. This is command equivalence, not contact-force prediction.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time

import numpy as np

def _distribution(values: list[float] | np.ndarray, *, timing_ms: bool = False) -> dict[str, float | int]:
    array = np.asarray(values, dtype=float)
    if array.size == 0 or not np.isfinite(array).all():
        raise ValueError("replay distribution must be finite and nonempty")
    summary = {
        "count": int(array.size), "mean": float(np.mean(array)),
        "p50": float(np.median(array)), "p95": float(np.quantile(array, .95)),
        "p99": float(np.quantile(array, .99)), "max": float(np.max(array)),
    }
    if timing_ms:
        summary["over_2ms"] = int(np.count_nonzero(array > 2.0))
    return summary


def _replay(runtime_root: Path, source: dict, rows: list[dict],
            twists: np.ndarray, *, backend: str, library: Path,
            make_provider, run_commands, native_class, numpy_class) -> tuple[dict, dict]:
    provider = make_provider(runtime_root, source["parameter_file"])
    native = None
    try:
        base_config = provider.runtime.solver.config
        if base_config.inner_iterations != 1 or base_config.sigr_exponent_r != 1.0:
            raise ValueError("source is not mature LEGACY_R1")
        if backend == "native":
            native = native_class(library)
            provider.runtime.solver = native
        else:
            provider.runtime.solver = numpy_class(
                replace(base_config, inner_iterations=512, backend="numpy")
            )
        elapsed_ms: list[float] = []
        command = provider.runtime.command

        def timed_command(*args, **kwargs):
            started = time.perf_counter_ns()
            result = command(*args, **kwargs)
            elapsed_ms.append((time.perf_counter_ns() - started) / 1e6)
            return result

        provider.runtime.command = timed_command
        replay = run_commands(provider, rows, twists, source_transitions=False)
        summary = {
            "backend": "cpp-double" if backend == "native" else "numpy-float64",
            "iterations": 512, "r": 1.0, "epsilon": .022,
            "dt_policy": "repeat_actual_full_dt_per_inner_iteration",
            "runtime_command_ms": _distribution(elapsed_ms, timing_ms=True),
            "warm_start_rows": replay["warm_start_rows"],
            "direction_retry_rows": replay["direction_retry_rows"],
            "final_gate_scaled_rows": int(np.count_nonzero(
                np.asarray(replay["final_gate_scales"]) < 1 - 1e-12
            )),
            "joint_box_violation_rows": int(np.count_nonzero(replay["bound_violations"])),
            "post_gate_slew_violation_rows": int(np.count_nonzero(replay["post_gate_slew_violations"])),
            "post_gate_task_cap_violation_rows": int(np.count_nonzero(replay["post_gate_task_cap_violations"])),
            "normal_final_abs_velocity_residual_mm_s": _distribution(
                np.abs(np.asarray(replay["tasks"])[:, 2] - twists[:, 2]) * 1000
            ),
        }
        return summary, replay
    finally:
        if native is not None:
            native.close()
        provider.close()


def run(*, runtime_root: Path, attempt_dir: Path, library: Path,
        max_rows: int, native_only: bool = False) -> dict:
    runtime_root = runtime_root.resolve(strict=True)
    attempt_dir = attempt_dir.resolve(strict=True)
    library = library.resolve(strict=True)
    # The topic worktree intentionally contains no copy of the runtime's
    # uncommitted live chain. Resolve the owner modules only from the explicit
    # source checkout, while keeping the new native adapter in this script's
    # directory. No runtime source file is modified or imported implicitly.
    sys.path.insert(0, str(runtime_root / "tools"))
    from replay_tase_force_feedback_selection import (  # type: ignore[import-not-found]
        _new_provider, _read_source, _run_rnn_commands,
    )
    from step5c_strict_rnn import StrictTaseRnnSolver  # type: ignore[import-not-found]
    from step5c_mature_rnn512_native import NativeMatureRnn512
    source, all_rows = _read_source(attempt_dir)
    if max_rows < 1:
        raise ValueError("max_rows must be positive")
    rows = all_rows[:max_rows]
    twists = np.asarray([row["requested_outer_twist_m_s_rad_s"] for row in rows], dtype=float)
    numpy_summary = numpy_replay = None
    if not native_only:
        numpy_summary, numpy_replay = _replay(
            runtime_root, source, rows, twists, backend="numpy", library=library,
            make_provider=_new_provider, run_commands=_run_rnn_commands,
            native_class=NativeMatureRnn512, numpy_class=StrictTaseRnnSolver,
        )
    native_summary, native_replay = _replay(
        runtime_root, source, rows, twists, backend="native", library=library,
        make_provider=_new_provider, run_commands=_run_rnn_commands,
        native_class=NativeMatureRnn512, numpy_class=StrictTaseRnnSolver,
    )
    qdot_difference = None
    if numpy_replay is not None:
        qdot_difference = np.max(np.abs(
            np.asarray(numpy_replay["final_qdots"]) -
            np.asarray(native_replay["final_qdots"])
        ), axis=1)
    return {
        "schema": "tase.i1.mature-r1-512-native-fixed-input-v1",
        "scope": "offline_same_sealed_observations_and_runtime_command_boundary",
        "source_attempt": str(attempt_dir),
        "source_protocol_id": source["reduction"]["protocol_id"],
        "source_evidence_eligible": source["reduction"]["evidence_eligible"],
        "source_total_rows": len(all_rows), "compared_rows": len(rows),
        "full_path": len(rows) == len(all_rows),
        "native_library": str(library),
        "native_library_sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
        "timing_boundary": "runtime.command incl. solver completion; excludes outer loop, host slew, final gate, KWR75, RTDE, TP",
        "physical_candidate_executed": False,
        "candidate_force_mae_predicted": False,
        "numpy": numpy_summary, "native": native_summary,
        "final_qdot_max_abs_delta_rad_s": (
            None if qdot_difference is None else _distribution(qdot_difference)
        ),
        "warm_start_count_delta": (
            None if numpy_summary is None else
            native_summary["warm_start_rows"] - numpy_summary["warm_start_rows"]
        ),
        "direction_retry_count_delta": (
            None if numpy_summary is None else
            native_summary["direction_retry_rows"] - numpy_summary["direction_retry_rows"]
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--attempt-dir", type=Path, required=True)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--max-rows", type=int, default=1000)
    parser.add_argument("--native-only", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(runtime_root=args.runtime_root, attempt_dir=args.attempt_dir,
                 library=args.library, max_rows=args.max_rows,
                 native_only=args.native_only)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "rows": result["compared_rows"],
                      "qdot_max_abs_delta": (
                          None if result["final_qdot_max_abs_delta_rad_s"] is None
                          else result["final_qdot_max_abs_delta_rad_s"]["max"]
                      )}))


if __name__ == "__main__":
    main()
