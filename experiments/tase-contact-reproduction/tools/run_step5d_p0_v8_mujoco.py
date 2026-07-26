#!/usr/bin/env python3
"""Run the offline Step5d P0 v8 no-contact canary in MuJoCo.

The plant is engine-specific, but every control tick crosses the production
``Step5dSimulatorAdapter`` seam.  This runner does not contain a second policy,
SafetyEnvelope, DLS fallback, bridge, RTDE, or controller entry point.
"""

from __future__ import annotations

import argparse
import dataclasses
import gc
import hashlib
import json
import math
import os
import platform
import struct
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import numpy as np

from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver
from step5d_control_contract import (
    JOINT_LAYOUT_CODE,
    STRICT_RNN_SOLVER_OK_STATUS,
    ZERO6,
    ControlCandidate,
    DeferredV30Diagnostics,
    SafetyEnvelope,
    StrictRnnControlPolicy,
)
from step5d_simulator_adapter import (
    P0_V8_HISTORICAL_DIAGNOSTIC_PHASES_S,
    P0_V8_CONTROL_HZ,
    P0_V8_DBIL_HZ,
    P0_V8_EFFECTIVE_KO,
    P0_V8_EPSILON,
    P0_V8_INNER_ITERATIONS,
    P0_V8_PHYSICS_HZ,
    P0_V8_QDOT_CAP_RAD_S,
    P0_V8_SIGR_EXPONENT_R,
    IntegerRateSchedule,
    SimulationCommand,
    SimulatorState,
    Step5dSimulatorAdapter,
    simulation_claim_boundary,
)
from ur10e_mujoco_adapter import MuJoCoVelocityPlant
from verify_step5d_sim_evidence import (
    P0_REQUIRED_FAULTS,
    source_composite_sha256,
)
from verify_step5d_p0_v8_mujoco import validate_phase_evidence
from verify_step5d_p0_v8_mujoco import validate_prewarm_evidence


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
PROFILE = {
    "id": "step5d_strict_rnn_no_contact_p0_v8",
    "backend": "cupy",
    "inner_iterations": P0_V8_INNER_ITERATIONS,
    "epsilon": P0_V8_EPSILON,
    "sigr_exponent_r": P0_V8_SIGR_EXPONENT_R,
    "qdot_cap_rad_s": P0_V8_QDOT_CAP_RAD_S,
    "effective_ko": P0_V8_EFFECTIVE_KO,
    "dls_runtime_fallback_allowed": False,
}
CONTROL_PATH = (
    "SimulatorState->Step5dObservation->StrictRnnControlPolicy->"
    "step5d_v30_contract_pipeline->SafetyEnvelope->RegisterCommand->"
    "SimulationCommand"
)
SOURCE_FILES = (
    "tools/step5c_strict_rnn.py",
    "tools/step5d_p0_v8_control_core.py",
    "tools/step5d_control_contract.py",
    "tools/step5d_simulator_adapter.py",
    "tools/ur10e_mujoco_adapter.py",
    "tools/run_step5d_p0_v8_mujoco.py",
    "tools/verify_step5d_sim_evidence.py",
    "tools/verify_step5d_p0_v8_mujoco.py",
    "config/step5d_liveprep_solver_gate.json",
    "config/schemas/ur10e_simulation_evidence_v1.schema.json",
    "config/schemas/ur10e_simulation_evidence_v2.schema.json",
    "config/schemas/ur10e_simulation_evidence_v3.schema.json",
    "config/schemas/ur10e_simulation_evidence_v4.schema.json",
    "config/schemas/step5d_p0_v8_production_path_prewarm_v1.schema.json",
)

EVIDENCE_SCHEMA_V4 = "ur10e_simulation_evidence_v4"
RUN_SCHEMA_V4 = "step5d_p0_v8_mujoco_run_v4"
TIMING_SCOPE_VERSION = "p0_v8_timing_lane_split_v2"
CONTROL_HARD_SCOPE = "simulator_state_ready_to_adapter_step_complete"
SIMULATOR_CYCLE_SCOPE = (
    "release_to_oracle_snapshot_to_adapter_step_to_command_apply_and_four_physics_substeps"
)
PREWARM_SCHEMA = "step5d_p0_v8_production_path_prewarm_v1"
PREWARM_EXECUTE_TICKS = 1_000
PREWARM_CONTROL_HZ = P0_V8_CONTROL_HZ
PREWARM_MODE = "source_bound_unmeasured_no_output_500hz"
PREWARM_PACING_STRATEGY = "previous_tick_start_plus_2ms_no_catch_up"
PREWARM_BURST_TOLERANCE_S = 0.00005
CONTROL_DEADLINE_MS = 2.0
CONTROL_DEADLINE_REASON = "control_deadline_miss_ge_2ms"
DEADLINE_COMMAND_CONTRACT_VERSION = "pre_write_exact_zero_stop_v1"
TRACE_PREFAULT_STRATEGY = (
    "numpy_fill_zero_before_gc_collect_and_measured_loop"
)
NUMERIC_THREAD_ENV_CONTRACT = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
}


class VelocityPlant(Protocol):
    manifest: Mapping[str, Any]

    def reset(self) -> None:
        ...

    def read_state(self, *, sequence: int, wall_time_s: float) -> SimulatorState:
        ...

    def write_command(self, command: Any) -> None:
        ...


@dataclass(frozen=True)
class PhaseSpec:
    duration_s: float
    sequence_index: int

    @property
    def tick_count(self) -> int:
        value = self.duration_s * P0_V8_CONTROL_HZ
        rounded = int(round(value))
        if rounded < 1 or not math.isclose(value, rounded, abs_tol=1e-9):
            raise ValueError("phase duration must contain an integer number of 500 Hz ticks")
        return rounded


@dataclass(frozen=True)
class ProductionPathPrewarmResult:
    execute_tick_count: int
    accepted_tick_count: int
    safe_hold_count: int
    stop_count: int
    nonfinite_output_count: int
    qdot_bound_violation_count: int
    dls_shadow_count: int
    dls_runtime_fallback_count: int
    register_command_generation_count: int
    command_sink_write_count: int
    first_sequence: int
    last_sequence: int
    release_wait_count: int
    deferred_diagnostic_count: int
    first_release_elapsed_s: float
    last_release_elapsed_s: float
    elapsed_release_span_s: float
    min_inter_release_s: float
    max_inter_release_s: float
    burst_interval_count: int

    @property
    def passed(self) -> bool:
        return (
            self.execute_tick_count == PREWARM_EXECUTE_TICKS
            and self.accepted_tick_count == PREWARM_EXECUTE_TICKS
            and self.safe_hold_count == 0
            and self.stop_count == 0
            and self.nonfinite_output_count == 0
            and self.qdot_bound_violation_count == 0
            and self.dls_shadow_count == PREWARM_EXECUTE_TICKS
            and self.dls_runtime_fallback_count == 0
            and self.register_command_generation_count == PREWARM_EXECUTE_TICKS
            and self.command_sink_write_count == 0
            and self.first_sequence == 0
            and self.last_sequence == PREWARM_EXECUTE_TICKS - 1
            and self.release_wait_count == PREWARM_EXECUTE_TICKS - 1
            and self.deferred_diagnostic_count == PREWARM_EXECUTE_TICKS
            and self.burst_interval_count == 0
            and self.min_inter_release_s
            >= 1.0 / PREWARM_CONTROL_HZ - PREWARM_BURST_TOLERANCE_S
        )


@dataclass(frozen=True)
class NominalPhaseResult:
    tick_count: int
    accepted_tick_count: int
    safe_hold_count: int
    stop_count: int
    nonfinite_output_count: int
    qdot_bound_violation_count: int
    unexpected_contact_count: int
    cage_collision_count: int
    control_deadline_miss_count: int
    cycle_compute_deadline_miss_count: int
    absolute_deadline_miss_count: int
    max_qdot_abs_rad_s: float
    exact_zero_rejection_count: int
    deadline_rejection_count: int
    deadline_zero_rejection_count: int
    nonzero_rejection_count: int
    physics_tick_count: int
    dbil_tick_count: int
    sim_time_start_s: float
    sim_time_end_s: float
    sim_time_drift_s: float
    first_sequence: int
    last_sequence: int
    control_compute_ms: np.ndarray
    oracle_snapshot_ms: np.ndarray
    command_apply_and_physics_ms: np.ndarray
    cycle_wall_ms: np.ndarray
    release_lateness_ms: np.ndarray
    absolute_finish_lateness_ms: np.ndarray
    sim_time_s: np.ndarray
    qdot: np.ndarray
    command_jacobian: np.ndarray
    desired_twist: np.ndarray
    reaction_normal: np.ndarray
    approach_normal: np.ndarray
    wrench: np.ndarray
    native_contact_count: np.ndarray
    cage_collision_count_per_tick: np.ndarray
    tcp_inside_cage: np.ndarray
    accepted: np.ndarray
    deadline_rejected: np.ndarray
    command_stop_request: np.ndarray
    command_bytes_sha256: np.ndarray
    actions: np.ndarray
    reasons: np.ndarray
    deferred: DeferredV30Diagnostics
    trace_buffers_prefaulted: bool

    @property
    def compute_ms(self) -> np.ndarray:
        """Compatibility alias for diagnostic sweep callers.

        In v2 this is the hard control lane only, never the simulator cycle.
        """

        return self.control_compute_ms

    @property
    def compute_deadline_miss_count(self) -> int:
        """Compatibility alias for the v2 hard control deadline count."""

        return self.control_deadline_miss_count

    @property
    def control_path_pass(self) -> bool:
        """Control/safety verdict; deadline acceptance remains a separate gate.

        A measured deadline miss is allowed here only when that exact sample was
        replaced by the explicit zero/stop command before the plant sink.  It
        still fails :func:`control_hard_timing` and therefore cannot make a
        failed 2 s or 10 s timing phase pass.
        """

        late = self.control_compute_ms >= CONTROL_DEADLINE_MS
        expected_accepted = np.logical_not(late).astype(np.uint8)
        expected_actions = np.where(late, "stop", "execute")
        expected_reasons = np.where(late, CONTROL_DEADLINE_REASON, "ok")
        deferred_actions = np.asarray(
            self.deferred.actions[: self.deferred.count], dtype="<U96"
        )
        deferred_reasons = np.asarray(
            self.deferred.reasons[: self.deferred.count], dtype="<U160"
        )

        return (
            self.accepted_tick_count
            == self.tick_count - self.control_deadline_miss_count
            and self.first_sequence == 0
            and self.last_sequence == self.tick_count - 1
            and self.physics_tick_count
            == self.tick_count * (P0_V8_PHYSICS_HZ // P0_V8_CONTROL_HZ)
            and self.dbil_tick_count
            == (
                (self.physics_tick_count - 1)
                // (P0_V8_PHYSICS_HZ // P0_V8_DBIL_HZ)
                + 1
            )
            and self.accepted.shape == (self.tick_count,)
            and bool(np.array_equal(self.accepted, expected_accepted))
            and self.actions.shape == (self.tick_count,)
            and bool(np.array_equal(self.actions, expected_actions))
            and self.reasons.shape == (self.tick_count,)
            and bool(np.array_equal(self.reasons, expected_reasons))
            and self.safe_hold_count == 0
            and self.stop_count == self.control_deadline_miss_count
            and self.nonfinite_output_count == 0
            and self.qdot_bound_violation_count == 0
            and self.qdot.shape == (self.tick_count, 6)
            and bool(np.all(np.isfinite(self.qdot)))
            and self.max_qdot_abs_rad_s <= P0_V8_QDOT_CAP_RAD_S + 1e-12
            and self.unexpected_contact_count == 0
            and self.cage_collision_count == 0
            and self.exact_zero_rejection_count
            == self.control_deadline_miss_count
            and self.deadline_rejection_count
            == self.control_deadline_miss_count
            and self.deadline_zero_rejection_count
            == self.control_deadline_miss_count
            and self.nonzero_rejection_count == 0
            and self.deadline_rejected.shape == (self.tick_count,)
            and bool(
                np.array_equal(
                    self.deadline_rejected,
                    late.astype(np.uint8),
                )
            )
            and self.command_stop_request.shape == (self.tick_count,)
            and bool(
                np.array_equal(
                    self.command_stop_request,
                    late.astype(np.uint8),
                )
            )
            and self.command_bytes_sha256.shape == (self.tick_count,)
            and bool(np.all(np.char.str_len(self.command_bytes_sha256) == 64))
            and bool(np.all(self.qdot[late] == 0.0))
            and self.deferred.count == self.tick_count
            and deferred_actions.shape == (self.tick_count,)
            and bool(np.all(deferred_actions == "execute"))
            and deferred_reasons.shape == (self.tick_count,)
            and bool(np.all(deferred_reasons == "ok"))
            and abs(self.sim_time_drift_s) <= 1e-9
            and self.sim_time_s.shape == (self.tick_count,)
            and bool(np.all(np.isfinite(self.sim_time_s)))
            and (
                self.tick_count == 1
                or bool(
                    np.allclose(
                        np.diff(self.sim_time_s),
                        1.0 / P0_V8_CONTROL_HZ,
                        atol=1e-12,
                        rtol=0.0,
                    )
                )
            )
        )


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def git_value(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def build_source_binding(
    *,
    root: Path,
    plant: VelocityPlant,
    solver: Any,
    no_contact_lane: Mapping[str, object],
    pace_wall_clock: bool,
    release_spin_window_s: float,
) -> dict[str, object]:
    source_hashes = {
        relpath: sha256_path(root / relpath)
        for relpath in SOURCE_FILES
    }
    try:
        base_commit = git_value(root, "merge-base", "HEAD", "origin/main")
    except (subprocess.CalledProcessError, FileNotFoundError):
        base_commit = git_value(root, "rev-parse", "HEAD")
    source: dict[str, object] = {
        "base_commit": base_commit,
        "head_commit": git_value(root, "rev-parse", "HEAD"),
        "calibration_hash": str(plant.manifest["calibration_hash"]),
        "model_sha256": str(
            plant.manifest["outputs"]["no_contact_velocity"]["sha256"]
        ),
        "frame_lineage_sha256": plant.read_state(
            sequence=0,
            wall_time_s=0.0,
        ).frame_lineage.sha256,
        "source_file_sha256": source_hashes,
        "no_contact_lane": dict(no_contact_lane),
        "runtime_timing_environment": runtime_timing_environment(
            plant=plant,
            solver=solver,
            pace_wall_clock=pace_wall_clock,
            release_spin_window_s=release_spin_window_s,
        ),
    }
    source["composite_sha256"] = source_composite_sha256(source)
    return source


def runtime_timing_environment(
    *,
    plant: VelocityPlant,
    solver: Any,
    pace_wall_clock: bool,
    release_spin_window_s: float,
) -> dict[str, object]:
    """Fingerprint process-local scheduling and numeric runtime capabilities."""

    try:
        affinity: dict[str, object] = {
            "available": True,
            "cpu_ids": sorted(int(value) for value in os.sched_getaffinity(0)),
        }
    except (AttributeError, OSError) as exc:
        affinity = {"available": False, "error_type": type(exc).__name__}
    try:
        scheduler_value = int(os.sched_getscheduler(0))
        scheduler_names = {
            int(value): name
            for name in ("SCHED_OTHER", "SCHED_FIFO", "SCHED_RR", "SCHED_BATCH", "SCHED_IDLE")
            if (value := getattr(os, name, None)) is not None
        }
        scheduler: dict[str, object] = {
            "available": True,
            "policy": scheduler_value,
            "policy_name": scheduler_names.get(scheduler_value, "unknown"),
            "priority": int(os.sched_getparam(0).sched_priority),
        }
    except (AttributeError, OSError) as exc:
        scheduler = {"available": False, "error_type": type(exc).__name__}
    cupy_module = getattr(solver, "_cp", None)
    mujoco_module = getattr(plant, "mujoco", None)
    return {
        "paced_wall_clock": bool(pace_wall_clock),
        "release_spin_window_s": float(release_spin_window_s),
        "timing_scope_contract": {
            "version": TIMING_SCOPE_VERSION,
            "control_hard_500hz": CONTROL_HARD_SCOPE,
            "simulator_cycle_diagnostic": SIMULATOR_CYCLE_SCOPE,
        },
        "deadline_command_contract": {
            "version": DEADLINE_COMMAND_CONTRACT_VERSION,
            "classification_point": "after_adapter_step_before_plant_write",
            "deadline_ms": CONTROL_DEADLINE_MS,
            "comparison": "control_elapsed_ms_gte_deadline",
            "late_action": "stop",
            "late_reason": CONTROL_DEADLINE_REASON,
            "late_qdot": list(ZERO6),
            "late_cmd_valid": False,
            "late_stop_request": True,
            "measured_samples_may_be_discarded": False,
            "deadline_rejection_may_satisfy_timing_gate": False,
        },
        "production_path_prewarm_contract": {
            "schema": PREWARM_SCHEMA,
            "mode": PREWARM_MODE,
            "execute_ticks": PREWARM_EXECUTE_TICKS,
            "control_hz": PREWARM_CONTROL_HZ,
            "paced": True,
            "pacing_strategy": PREWARM_PACING_STRATEGY,
            "burst_tolerance_s": PREWARM_BURST_TOLERANCE_S,
            "control_path": CONTROL_PATH,
            "complete_production_path": True,
            "safety_envelope_exercised": True,
            "dls_shadow_only": True,
            "register_command_generated": True,
            "command_sink_write_allowed": False,
            "timing_acceptance_eligible": False,
            "measured_samples_may_be_discarded": False,
        },
        "trace_prefault": {
            "required": True,
            "completed": True,
            "strategy": TRACE_PREFAULT_STRATEGY,
        },
        "process_affinity": affinity,
        "process_scheduler": scheduler,
        "thread_environment": {
            name: os.environ.get(name) for name in NUMERIC_THREAD_ENV_CONTRACT
        },
        "versions": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "numpy": np.__version__,
            "cupy": str(getattr(cupy_module, "__version__", "unavailable")),
            "mujoco": str(getattr(mujoco_module, "__version__", "unavailable")),
        },
        "capabilities": {
            "busy_poll_completion": bool(
                getattr(solver, "cupy_busy_poll_completion", False)
            ),
            "pinned_host_staging": bool(
                getattr(solver, "cupy_host_staging_pinned", False)
            ),
            "dedicated_nonblocking_stream": bool(
                getattr(solver, "cupy_dedicated_stream", False)
            ),
        },
    }


def require_numeric_thread_environment() -> dict[str, str]:
    """Refuse timing runs that can create RT-throttling BLAS worker pools."""

    observed = {
        name: os.environ.get(name) for name in NUMERIC_THREAD_ENV_CONTRACT
    }
    if observed != NUMERIC_THREAD_ENV_CONTRACT:
        raise RuntimeError(
            "P0 v8 timing requires OPENBLAS/OMP/MKL/NUMEXPR thread counts all set to 1"
        )
    return dict(NUMERIC_THREAD_ENV_CONTRACT)


def require_hash_bound_no_contact_lane(
    plant: MuJoCoVelocityPlant,
) -> dict[str, object]:
    """Require a generator-owned no-contact scene; never mutate plant geometry.

    The normal P0 press target advances roughly 9 mm in 60 seconds.  A contact
    scene derived from the v29 pose has only about 4.7 mm pad clearance, so it
    is not a valid no-contact lane.  The generator must produce and hash-bind a
    distinct scene whose clearance budget covers the complete 60 second phase.
    """

    scene = plant.manifest.get("no_contact_scene")
    if not isinstance(scene, Mapping):
        raise ValueError("model manifest lacks a hash-bound no_contact_scene")
    required = {
        "mode": "surface_translation_with_native_collision",
        "output_key": "no_contact_velocity",
        "native_contact_enabled": True,
    }
    for field, expected in required.items():
        if scene.get(field) != expected:
            raise ValueError(f"no_contact_scene.{field} must equal {expected!r}")
    output = (plant.manifest.get("outputs") or {}).get("no_contact_velocity")
    if not isinstance(output, Mapping) or not str(output.get("sha256") or ""):
        raise ValueError("model manifest lacks outputs.no_contact_velocity binding")
    if not str(scene.get("id") or "") or not str(scene.get("claim") or ""):
        raise ValueError("no_contact_scene id and claim are required")
    translation = np.asarray(scene.get("translation_offset_m"), dtype=float)
    if (
        translation.shape != (3,)
        or not np.all(np.isfinite(translation))
        or float(np.linalg.norm(translation)) <= 0.0
    ):
        raise ValueError("no_contact_scene.translation_offset_m must be a finite nonzero 3-vector")
    numeric: dict[str, float] = {}
    for field in (
        "initial_clearance_m",
        "required_initial_clearance_m",
        "commanded_travel_budget_m",
        "minimum_remaining_clearance_m",
    ):
        try:
            value = float(scene[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"no_contact_scene.{field} must be finite") from exc
        if not math.isfinite(value) or value < 0.0:
            raise ValueError(f"no_contact_scene.{field} must be finite and non-negative")
        numeric[field] = value
    computed_remaining = (
        numeric["initial_clearance_m"] - numeric["commanded_travel_budget_m"]
    )
    if not math.isclose(
        computed_remaining,
        numeric["minimum_remaining_clearance_m"],
        abs_tol=1e-9,
    ):
        raise ValueError("no_contact_scene clearance budget is internally inconsistent")
    if numeric["commanded_travel_budget_m"] < 0.009 - 1e-9:
        raise ValueError("no_contact_scene does not cover the 60 s press travel budget")
    if numeric["initial_clearance_m"] < numeric["required_initial_clearance_m"]:
        raise ValueError("no_contact_scene does not meet required initial clearance")
    if numeric["minimum_remaining_clearance_m"] <= 0.0:
        raise ValueError("no_contact_scene remaining clearance must be positive")
    return dict(scene)


def make_solver(root: Path) -> StrictTaseRnnSolver:
    solver = StrictTaseRnnSolver(
        StrictRnnConfig(
            paper_truth_path=root / "config" / "step5d_liveprep_solver_gate.json",
            qdot_limit_rad_s=P0_V8_QDOT_CAP_RAD_S,
            epsilon=P0_V8_EPSILON,
            sigr_exponent_r=P0_V8_SIGR_EXPONENT_R,
            inner_iterations=P0_V8_INNER_ITERATIONS,
            backend="cupy",
        )
    )
    if not solver.cupy_host_staging_pinned:
        raise RuntimeError("P0 v8 requires page-locked CuPy host staging")
    if not solver.cupy_dedicated_stream:
        raise RuntimeError("P0 v8 requires the dedicated nonblocking CuPy stream")
    if not solver.cupy_busy_poll_completion:
        raise RuntimeError("P0 v8 requires preallocated CuPy event completion")
    equivalence = solver.cupy_parallel_equivalence or {}
    if (
        equivalence.get("bitwise_equal") is not True
        or int(equivalence.get("samples", 0)) < 100
        or equivalence.get("parallel_block_threads") != 6
    ):
        raise RuntimeError("P0 v8 requires startup block-6/serial equivalence")
    return solver


def warm_solver(solver: Any, state: SimulatorState) -> None:
    solver.reset_state()
    solver.warm_start(
        J=state.command_jacobian,
        xdot_c=state.desired_twist,
        omega_minus=state.omega_minus,
        omega_plus=state.omega_plus,
    )


def make_adapter(policy: Any, *, capacity: int) -> Step5dSimulatorAdapter:
    return Step5dSimulatorAdapter(
        policy=policy,
        safety_envelope=SafetyEnvelope(
            qdot_cap_rad_s=P0_V8_QDOT_CAP_RAD_S,
            max_normal_tracking_error_m_s=5e-4,
            max_residual_norm=1e-3,
        ),
        deferred_diagnostics=DeferredV30Diagnostics(capacity=capacity),
    )


def wait_until(deadline_s: float, *, spin_window_s: float = 0.00025) -> None:
    while True:
        remaining = deadline_s - time.perf_counter()
        if remaining <= 0.0:
            return
        if remaining > spin_window_s:
            time.sleep(remaining - spin_window_s)
        else:
            while time.perf_counter() < deadline_s:
                pass
            return


def prefault_numeric_buffers(*buffers: np.ndarray) -> bool:
    """Materialize preallocated trace pages before the measured 500 Hz loop."""

    for buffer in buffers:
        buffer.fill(0)
    return True


def simulation_command_sha256(
    qdot: Sequence[float],
    *,
    cmd_valid: bool,
    stop_request: bool,
) -> str:
    """Hash the exact command payload used by ``SimulationCommand``."""

    values = tuple(float(value) for value in qdot)
    if len(values) != 6 or any(not math.isfinite(value) for value in values):
        raise ValueError("simulation command hash requires six finite qdot values")
    encoded = struct.pack("<6d??", *values, bool(cmd_valid), bool(stop_request))
    return hashlib.sha256(encoded).hexdigest()


def classify_control_deadline(
    command: SimulationCommand,
    *,
    control_elapsed_ms: float,
) -> tuple[SimulationCommand, bool]:
    """Replace a late candidate with an exact-zero stop before sink write.

    The measured duration is never altered or discarded.  A value exactly on
    the 2 ms boundary is late by contract.  Nonfinite timing is also rejected
    fail-closed; the trace verifier will independently reject that timing as
    malformed rather than allowing it to masquerade as a valid sample.
    """

    elapsed = float(control_elapsed_ms)
    late = not math.isfinite(elapsed) or elapsed >= CONTROL_DEADLINE_MS
    if not late:
        return command, False
    return (
        dataclasses.replace(
            command,
            qdot=ZERO6,
            accepted=False,
            action="stop",
            reason=CONTROL_DEADLINE_REASON,
            stop_request=True,
            command_bytes_sha256=simulation_command_sha256(
                ZERO6,
                cmd_valid=False,
                stop_request=True,
            ),
        ),
        True,
    )


def run_production_path_prewarm(
    *,
    plant: VelocityPlant,
    solver: Any,
    release_spin_window_s: float = 0.00025,
) -> ProductionPathPrewarmResult:
    """Exercise 1,000 complete control ticks without writing a plant command.

    This is deliberately not a discarded prefix of the measured trace.  It has
    its own adapter, sequence, diagnostics buffer, and fixed 500 Hz pacing.  A
    generated RegisterCommand/SimulationCommand proves the production seam was
    crossed, while omitting ``plant.write_command`` keeps the prewarm no-output.
    """

    if (
        not math.isfinite(float(release_spin_window_s))
        or release_spin_window_s < 0.0
        or release_spin_window_s > 1.0 / PREWARM_CONTROL_HZ
    ):
        raise ValueError("prewarm spin window must be within one 500 Hz period")
    plant.reset()
    base_state = plant.read_state(sequence=0, wall_time_s=0.0)
    warm_solver(solver, base_state)
    adapter = make_adapter(
        StrictRnnControlPolicy(solver),
        capacity=PREWARM_EXECUTE_TICKS,
    )
    accepted = 0
    safe_holds = 0
    stops = 0
    nonfinite = 0
    over_cap = 0
    dls_shadows = 0
    dls_fallbacks = 0
    register_commands = 0
    release_waits = 0
    release_elapsed_s = np.empty(PREWARM_EXECUTE_TICKS, dtype=np.float64)
    previous_tick_started: float | None = None
    wall_start = time.perf_counter()
    gc_was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        for index in range(PREWARM_EXECUTE_TICKS):
            if previous_tick_started is not None:
                wait_until(
                    previous_tick_started + 1.0 / PREWARM_CONTROL_HZ,
                    spin_window_s=release_spin_window_s,
                )
                release_waits += 1
            tick_started = time.perf_counter()
            elapsed_s = tick_started - wall_start
            release_elapsed_s[index] = elapsed_s
            state = dataclasses.replace(
                base_state,
                sequence=index,
                sim_time_s=(
                    float(base_state.sim_time_s) + index / PREWARM_CONTROL_HZ
                ),
                wall_time_s=elapsed_s,
                path_time_s=(
                    float(base_state.path_time_s) + index / PREWARM_CONTROL_HZ
                ),
            )
            result = adapter.step(state)
            values = np.asarray(result.simulation_command.qdot, dtype=float)
            accepted += int(result.control.decision.accepted)
            safe_holds += int(result.control.decision.action == "safe_hold")
            stops += int(result.control.decision.action == "stop")
            nonfinite += int(not np.all(np.isfinite(values)))
            over_cap += int(
                np.all(np.isfinite(values))
                and float(np.max(np.abs(values)))
                > P0_V8_QDOT_CAP_RAD_S + 1e-12
            )
            shadow = result.control.dls_shadow
            dls_shadows += int(shadow is not None)
            dls_fallbacks += int(
                shadow is not None and shadow.runtime_fallback_allowed
            )
            register = result.control.register_command
            register_values = register.as_register_values()
            expected_indices = {
                26,
                28,
                *range(37, 43),
                *range(43, 48),
            }
            register_commands += int(
                set(register_values) == expected_indices
                and all(
                    math.isfinite(float(value))
                    for value in register_values.values()
                )
                and register.layout_code == JOINT_LAYOUT_CODE
                and register.heartbeat == float(index)
                and register.cmd_valid is True
                and register.stop_request is False
                and register.qdot == result.control.decision.qdot
                and register.qdot == result.simulation_command.qdot
            )
            # No plant.write_command call is allowed in this prewarm lane.
            previous_tick_started = tick_started
    finally:
        if gc_was_enabled:
            gc.enable()
    intervals = np.diff(release_elapsed_s)
    minimum_interval = float(np.min(intervals))
    maximum_interval = float(np.max(intervals))
    return ProductionPathPrewarmResult(
        execute_tick_count=PREWARM_EXECUTE_TICKS,
        accepted_tick_count=accepted,
        safe_hold_count=safe_holds,
        stop_count=stops,
        nonfinite_output_count=nonfinite,
        qdot_bound_violation_count=over_cap,
        dls_shadow_count=dls_shadows,
        dls_runtime_fallback_count=dls_fallbacks,
        register_command_generation_count=register_commands,
        command_sink_write_count=0,
        first_sequence=0,
        last_sequence=PREWARM_EXECUTE_TICKS - 1,
        release_wait_count=release_waits,
        deferred_diagnostic_count=adapter.deferred_diagnostics.count,
        first_release_elapsed_s=float(release_elapsed_s[0]),
        last_release_elapsed_s=float(release_elapsed_s[-1]),
        elapsed_release_span_s=float(
            release_elapsed_s[-1] - release_elapsed_s[0]
        ),
        min_inter_release_s=minimum_interval,
        max_inter_release_s=maximum_interval,
        burst_interval_count=int(
            np.count_nonzero(
                intervals
                < 1.0 / PREWARM_CONTROL_HZ - PREWARM_BURST_TOLERANCE_S
            )
        ),
    )


def reset_after_production_path_prewarm(
    *,
    plant: VelocityPlant,
    solver: Any,
) -> dict[str, object]:
    """Reset every stateful prewarm owner before measured sequence zero."""

    plant.reset()
    solver.reset_state()
    return {
        "simulator_state_reset_after_prewarm": True,
        "solver_state_reset_after_prewarm": True,
        "control_adapter_discarded_after_prewarm": True,
        "measured_phase_first_sequence": 0,
        "post_reset_unmeasured_execute_tick_count": 0,
        "next_action": "measured_direct_frozen_current_stage_duration",
    }


def prewarm_artifact_payload(
    *,
    result: ProductionPathPrewarmResult,
    source_binding: Mapping[str, object],
    reset_state: Mapping[str, object],
) -> dict[str, object]:
    runtime = source_binding.get("runtime_timing_environment")
    contract = (
        runtime.get("production_path_prewarm_contract")
        if isinstance(runtime, Mapping)
        else None
    )
    return {
        "schema": PREWARM_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_composite_sha256": source_binding.get("composite_sha256"),
        "profile": dict(PROFILE),
        "contract": dict(contract) if isinstance(contract, Mapping) else {},
        "result": {
            **dataclasses.asdict(result),
            "pacing_hz": PREWARM_CONTROL_HZ,
            "paced": True,
            "unmeasured": True,
            "no_output": True,
            "timing_acceptance_eligible": False,
            "measured_sample_count": 0,
            "pass": result.passed,
        },
        "reset": dict(reset_state),
        "claim_boundary": {
            "prewarm_is_not_measured_timing_evidence": True,
            "prewarm_is_not_p0_pass": True,
            "prewarm_is_not_live_acceptance": True,
        },
    }


def write_production_path_prewarm_artifact(
    *,
    output_dir: Path,
    result: ProductionPathPrewarmResult,
    source_binding: Mapping[str, object],
    reset_state: Mapping[str, object],
) -> tuple[Path, dict[str, object], dict[str, object]]:
    payload = prewarm_artifact_payload(
        result=result,
        source_binding=source_binding,
        reset_state=reset_state,
    )
    path = output_dir / "production_path_prewarm.json"
    write_json(path, payload)
    binding = {
        "schema": PREWARM_SCHEMA,
        "path": path.name,
        "sha256": sha256_path(path),
        "size_bytes": path.stat().st_size,
        "source_composite_sha256": source_binding.get("composite_sha256"),
        "execute_tick_count": result.execute_tick_count,
        "pacing_hz": PREWARM_CONTROL_HZ,
        "paced": True,
        "pass": result.passed,
    }
    return path, payload, binding


def run_nominal_phase(
    *,
    plant: VelocityPlant,
    solver: Any,
    spec: PhaseSpec,
    pace_wall_clock: bool = False,
    release_spin_window_s: float = 0.00025,
    plant_already_reset: bool = False,
) -> NominalPhaseResult:
    """Run one phase with no I/O or dynamically growing tick log in the loop."""

    if (
        not math.isfinite(float(release_spin_window_s))
        or release_spin_window_s < 0.0
        or release_spin_window_s > 1.0 / P0_V8_CONTROL_HZ
    ):
        raise ValueError("release spin window must be within one 500 Hz period")
    schedule = IntegerRateSchedule()
    tick_count = spec.tick_count
    if not plant_already_reset:
        plant.reset()
    first_state = plant.read_state(sequence=0, wall_time_s=0.0)
    warm_solver(solver, first_state)
    policy = StrictRnnControlPolicy(solver)
    adapter = make_adapter(policy, capacity=tick_count)

    control_compute_ms = np.empty(tick_count, dtype=np.float64)
    oracle_snapshot_ms = np.empty(tick_count, dtype=np.float64)
    command_apply_and_physics_ms = np.empty(tick_count, dtype=np.float64)
    cycle_wall_ms = np.empty(tick_count, dtype=np.float64)
    release_lateness_ms = np.empty(tick_count, dtype=np.float64)
    absolute_finish_lateness_ms = np.empty(tick_count, dtype=np.float64)
    sim_time_s = np.empty(tick_count, dtype=np.float64)
    qdot = np.empty((tick_count, 6), dtype=np.float64)
    command_jacobian = np.empty((tick_count, 6, 6), dtype=np.float64)
    desired_twist = np.empty((tick_count, 6), dtype=np.float64)
    reaction_normal = np.empty((tick_count, 3), dtype=np.float64)
    approach_normal = np.empty((tick_count, 3), dtype=np.float64)
    wrench = np.empty((tick_count, 6), dtype=np.float64)
    native_contact_count = np.empty(tick_count, dtype=np.int32)
    cage_collision_count_per_tick = np.empty(tick_count, dtype=np.int32)
    tcp_inside_cage = np.empty(tick_count, dtype=np.uint8)
    accepted = np.empty(tick_count, dtype=np.uint8)
    deadline_rejected = np.empty(tick_count, dtype=np.uint8)
    command_stop_request = np.empty(tick_count, dtype=np.uint8)
    command_bytes_sha256 = np.full(tick_count, "", dtype="<U64")
    trace_buffers_prefaulted = prefault_numeric_buffers(
        control_compute_ms,
        oracle_snapshot_ms,
        command_apply_and_physics_ms,
        cycle_wall_ms,
        release_lateness_ms,
        absolute_finish_lateness_ms,
        sim_time_s,
        qdot,
        command_jacobian,
        desired_twist,
        reaction_normal,
        approach_normal,
        wrench,
        native_contact_count,
        cage_collision_count_per_tick,
        tcp_inside_cage,
        accepted,
        deadline_rejected,
        command_stop_request,
    ) and bool(adapter.deferred_diagnostics.prefaulted)
    actions: list[str | None] = [None] * tick_count
    reasons: list[str | None] = [None] * tick_count

    safe_holds = 0
    stops = 0
    nonfinite = 0
    over_cap = 0
    contacts = 0
    collisions = 0
    control_deadline_misses = 0
    cycle_compute_deadline_misses = 0
    absolute_deadline_misses = 0
    exact_zero_rejections = 0
    deadline_rejections = 0
    deadline_zero_rejections = 0
    nonzero_rejections = 0
    physics_tick = 0
    dbil_ticks = 0
    sim_start = float(first_state.sim_time_s)

    gc_was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    # The absolute 500 Hz schedule starts only after the one-off collection and
    # GC disable transition.  Starting it before gc.collect() silently made the
    # first releases late while the old compute-only counter hid that lateness.
    wall_start = time.perf_counter()
    try:
        for index in range(tick_count):
            release = wall_start + index / P0_V8_CONTROL_HZ
            absolute_deadline = release + 1.0 / P0_V8_CONTROL_HZ
            if pace_wall_clock and index:
                wait_until(release, spin_window_s=release_spin_window_s)
            cycle_started = time.perf_counter()
            state = plant.read_state(
                sequence=index,
                wall_time_s=time.perf_counter() - wall_start,
            )
            state_ready = time.perf_counter()
            result = adapter.step(state)
            control_finished = time.perf_counter()
            control_elapsed_ms = (control_finished - state_ready) * 1000.0
            command, was_deadline_rejected = classify_control_deadline(
                result.simulation_command,
                control_elapsed_ms=control_elapsed_ms,
            )
            plant.write_command(command)
            finished = time.perf_counter()
            oracle_elapsed_ms = (state_ready - cycle_started) * 1000.0
            physics_elapsed_ms = (finished - control_finished) * 1000.0
            cycle_elapsed_ms = (finished - cycle_started) * 1000.0
            release_lateness = max(0.0, (cycle_started - release) * 1000.0)
            absolute_finish_lateness = max(
                0.0,
                (finished - absolute_deadline) * 1000.0,
            )

            values = np.asarray(command.qdot, dtype=float)
            control_compute_ms[index] = control_elapsed_ms
            oracle_snapshot_ms[index] = oracle_elapsed_ms
            command_apply_and_physics_ms[index] = physics_elapsed_ms
            cycle_wall_ms[index] = cycle_elapsed_ms
            release_lateness_ms[index] = release_lateness
            absolute_finish_lateness_ms[index] = absolute_finish_lateness
            sim_time_s[index] = state.sim_time_s
            qdot[index, :] = values
            command_jacobian[index, :, :] = np.asarray(
                state.command_jacobian, dtype=float
            )
            desired_twist[index, :] = np.asarray(state.desired_twist, dtype=float)
            reaction_normal[index, :] = np.asarray(
                state.reaction_normal, dtype=float
            )
            approach_normal[index, :] = np.asarray(
                state.approach_normal, dtype=float
            )
            wrench[index, :] = np.asarray(state.wrench, dtype=float)
            native_contact_count[index] = int(state.native_contact_count)
            cage_collision_count_per_tick[index] = int(state.cage_collision_count)
            tcp_inside_cage[index] = 1 if state.tcp_inside_cage else 0
            accepted[index] = 1 if command.accepted else 0
            deadline_rejected[index] = 1 if was_deadline_rejected else 0
            command_stop_request[index] = 1 if command.stop_request else 0
            command_bytes_sha256[index] = command.command_bytes_sha256
            actions[index] = command.action
            reasons[index] = command.reason
            safe_holds += int(command.action == "safe_hold")
            stops += int(command.action == "stop")
            nonfinite += int(not np.all(np.isfinite(values)))
            over_cap += int(
                np.all(np.isfinite(values))
                and float(np.max(np.abs(values))) > P0_V8_QDOT_CAP_RAD_S + 1e-12
            )
            contacts += int(state.native_contact_count != 0)
            collisions += int(
                state.cage_collision_count != 0 or not state.tcp_inside_cage
            )
            control_deadline_misses += int(control_elapsed_ms >= 2.0)
            cycle_compute_deadline_misses += int(cycle_elapsed_ms >= 2.0)
            absolute_deadline_misses += int(absolute_finish_lateness > 0.0)
            rejected = not command.accepted
            exact_zero = command.qdot == ZERO6
            exact_zero_rejections += int(rejected and exact_zero)
            nonzero_rejections += int(rejected and not exact_zero)
            deadline_rejections += int(was_deadline_rejected)
            deadline_zero_rejections += int(
                was_deadline_rejected
                and rejected
                and exact_zero
                and command.action == "stop"
                and command.reason == CONTROL_DEADLINE_REASON
                and command.stop_request
            )
            for substep in range(schedule.control_stride):
                dbil_ticks += int(schedule.is_dbil_tick(physics_tick + substep))
            physics_tick += schedule.control_stride
    finally:
        if gc_was_enabled:
            gc.enable()

    final_state = plant.read_state(
        sequence=tick_count,
        wall_time_s=time.perf_counter() - wall_start,
    )
    expected_end = sim_start + tick_count / P0_V8_CONTROL_HZ
    sim_end = float(final_state.sim_time_s)
    return NominalPhaseResult(
        tick_count=tick_count,
        accepted_tick_count=int(np.count_nonzero(accepted)),
        safe_hold_count=safe_holds,
        stop_count=stops,
        nonfinite_output_count=nonfinite,
        qdot_bound_violation_count=over_cap,
        unexpected_contact_count=contacts,
        cage_collision_count=collisions,
        control_deadline_miss_count=control_deadline_misses,
        cycle_compute_deadline_miss_count=cycle_compute_deadline_misses,
        absolute_deadline_miss_count=absolute_deadline_misses,
        max_qdot_abs_rad_s=float(np.max(np.abs(qdot))),
        exact_zero_rejection_count=exact_zero_rejections,
        deadline_rejection_count=deadline_rejections,
        deadline_zero_rejection_count=deadline_zero_rejections,
        nonzero_rejection_count=nonzero_rejections,
        physics_tick_count=physics_tick,
        dbil_tick_count=dbil_ticks,
        sim_time_start_s=sim_start,
        sim_time_end_s=sim_end,
        sim_time_drift_s=sim_end - expected_end,
        first_sequence=0,
        last_sequence=tick_count - 1,
        control_compute_ms=control_compute_ms,
        oracle_snapshot_ms=oracle_snapshot_ms,
        command_apply_and_physics_ms=command_apply_and_physics_ms,
        cycle_wall_ms=cycle_wall_ms,
        release_lateness_ms=release_lateness_ms,
        absolute_finish_lateness_ms=absolute_finish_lateness_ms,
        sim_time_s=sim_time_s,
        qdot=qdot,
        command_jacobian=command_jacobian,
        desired_twist=desired_twist,
        reaction_normal=reaction_normal,
        approach_normal=approach_normal,
        wrench=wrench,
        native_contact_count=native_contact_count,
        cage_collision_count_per_tick=cage_collision_count_per_tick,
        tcp_inside_cage=tcp_inside_cage,
        accepted=accepted,
        deadline_rejected=deadline_rejected,
        command_stop_request=command_stop_request,
        command_bytes_sha256=command_bytes_sha256,
        actions=np.asarray(actions, dtype="<U96"),
        reasons=np.asarray(reasons, dtype="<U160"),
        deferred=adapter.deferred_diagnostics,
        trace_buffers_prefaulted=bool(trace_buffers_prefaulted),
    )


class _CandidateMutationPolicy:
    def __init__(
        self,
        base: Any,
        mutation: Callable[[Any, ControlCandidate], ControlCandidate],
    ) -> None:
        self.base = base
        self.mutation = mutation

    def compute(self, observation: Any) -> ControlCandidate:
        return self.mutation(observation, self.base.compute(observation))


class _MissingPolicy:
    def compute(self, observation: Any) -> None:
        del observation
        return None


def _negate_candidate(observation: Any, candidate: ControlCandidate) -> ControlCandidate:
    del observation
    qdot = tuple(-float(value) for value in candidate.qdot)
    twist = tuple(-float(value) for value in candidate.predicted_twist)
    return dataclasses.replace(
        candidate,
        qdot=qdot,
        predicted_twist=twist,
        residual_norm=float(
            np.linalg.norm(np.asarray(twist) - np.asarray(candidate.diagnostics.get("xdot_c", ZERO6)))
        ),
    )


def _rail_candidate(observation: Any, candidate: ControlCandidate) -> ControlCandidate:
    values = np.full(6, 0.051, dtype=float)
    predicted = np.asarray(observation.jacobian, dtype=float) @ values
    return dataclasses.replace(
        candidate,
        qdot=tuple(float(value) for value in values),
        predicted_twist=tuple(float(value) for value in predicted),
        residual_norm=float(
            np.linalg.norm(predicted - np.asarray(observation.desired_twist, dtype=float))
        ),
        active_bounds_count=0,
    )


def run_fault_matrix(*, plant: VelocityPlant, solver: Any) -> list[dict[str, object]]:
    """Exercise ingress/policy/safety faults through the shared adapter seam."""

    plant.reset()
    base = plant.read_state(sequence=0, wall_time_s=0.0)

    def prepared_adapter(policy: Any | None = None) -> Step5dSimulatorAdapter:
        warm_solver(solver, base)
        return make_adapter(
            policy or StrictRnnControlPolicy(solver),
            capacity=4,
        )

    def single(
        state: SimulatorState | None,
        *,
        policy: Any | None = None,
        previous_qdot: tuple[float, ...] | None = None,
    ) -> Any:
        adapter = prepared_adapter(policy)
        if previous_qdot is not None:
            adapter.previous_qdot = previous_qdot  # type: ignore[assignment]
        return adapter.step(state)

    def after_nominal(second: SimulatorState | None) -> Any:
        adapter = prepared_adapter()
        adapter.step(base)
        return adapter.step(second)

    base_policy = StrictRnnControlPolicy(solver)
    scenarios: dict[str, tuple[Any, str]] = {}
    scenarios["direction_unload"] = (
        single(
            base,
            policy=_CandidateMutationPolicy(base_policy, _negate_candidate),
        ),
        "approach_normal_unload_mismatch",
    )
    scenarios["wrong_frame"] = (
        single(
            dataclasses.replace(
                base,
                frame_lineage=dataclasses.replace(base.frame_lineage, wrench_frame="tool0"),
            )
        ),
        "frame_lineage_not_canonicalized",
    )
    scenarios["normal_mismatch"] = (
        single(dataclasses.replace(base, approach_normal=base.reaction_normal)),
        "normal_contract_mismatch",
    )
    scenarios["sequence_duplicate"] = (
        after_nominal(dataclasses.replace(base, sequence=0, sim_time_s=base.sim_time_s + 0.002)),
        "sequence_duplicate",
    )
    scenarios["sequence_gap"] = (
        after_nominal(dataclasses.replace(base, sequence=2, sim_time_s=base.sim_time_s + 0.002)),
        "sequence_gap",
    )
    scenarios["sequence_reordered"] = (
        after_nominal(dataclasses.replace(base, sequence=-1, sim_time_s=base.sim_time_s + 0.002)),
        "sequence_reordered",
    )
    scenarios["watchdog_stale"] = (
        single(dataclasses.replace(base, observation_age_s=0.100001)),
        "observation_watchdog_stale",
    )
    scenarios["missing_observation"] = (
        single(None),
        "missing_simulator_observation",
    )
    scenarios["missing_policy_output"] = (
        single(base, policy=_MissingPolicy()),
        "strict_rnn_policy_failure:TypeError",
    )
    bad_q = (math.nan, *base.q[1:])
    scenarios["nan_state"] = (
        single(dataclasses.replace(base, q=bad_q)),
        "nonfinite_simulator_state",
    )
    warm_solver(solver, base)
    rail_policy = _CandidateMutationPolicy(StrictRnnControlPolicy(solver), _rail_candidate)
    scenarios["qdot_rail"] = (
        single(base, policy=rail_policy, previous_qdot=(0.051,) * 6),
        "qdot_bound_exceeded",
    )
    scenarios["control_jitter"] = (
        after_nominal(dataclasses.replace(base, sequence=1, sim_time_s=base.sim_time_s + 0.003)),
        "control_tick_jitter",
    )
    scenarios["packet_drop"] = (
        after_nominal(None),
        "missing_simulator_observation",
    )
    scenarios["unexpected_contact"] = (
        single(dataclasses.replace(base, native_contact_count=1)),
        "unexpected_native_contact",
    )
    scenarios["cage_collision"] = (
        single(dataclasses.replace(base, cage_collision_count=1)),
        "tcp_cage_collision",
    )
    scenarios["tcp_outside_cage"] = (
        single(dataclasses.replace(base, tcp_inside_cage=False)),
        "tcp_outside_cage",
    )
    scenarios["force_guard"] = (
        single(dataclasses.replace(base, wrench=(5.000001, 0.0, 0.0, 0.0, 0.0, 0.0))),
        "force_guard_exceeded",
    )
    scenarios["torque_guard"] = (
        single(dataclasses.replace(base, wrench=(0.0, 0.0, 0.0, 3.000001, 0.0, 0.0))),
        "torque_guard_exceeded",
    )

    if set(scenarios) != P0_REQUIRED_FAULTS:
        raise RuntimeError("runner fault matrix drifted from evidence verifier")
    rows: list[dict[str, object]] = []
    for fault_id in sorted(scenarios):
        result, expected = scenarios[fault_id]
        reason = str(result.control.decision.reason)
        zero = result.simulation_command.qdot == ZERO6
        rows.append(
            {
                "id": fault_id,
                "expected_reason_contains": expected,
                "observed_reason": reason,
                "action": result.control.decision.action,
                "exact_zero_command": zero,
                "stop_request": bool(result.simulation_command.stop_request),
                "command_qdot": [
                    float(value) for value in result.simulation_command.qdot
                ],
                "command_bytes_sha256": result.simulation_command.command_bytes_sha256,
                "passed": (
                    not result.control.decision.accepted
                    and zero
                    and expected in reason
                ),
            }
        )
    return rows


def percentile(values: np.ndarray, quantile: float) -> float:
    return float(np.percentile(values, quantile))


def timing_distribution(values: np.ndarray) -> dict[str, float]:
    return {
        "p50_ms": percentile(values, 50),
        "p95_ms": percentile(values, 95),
        "p99_ms": percentile(values, 99),
        "max_ms": float(np.max(values)),
    }


def control_hard_timing(
    nominal: NominalPhaseResult,
    *,
    paced: bool,
) -> dict[str, object]:
    """Summarize only the production-shaped state-to-command control lane."""

    p50_ms = percentile(nominal.control_compute_ms, 50)
    p95_ms = percentile(nominal.control_compute_ms, 95)
    p99_ms = percentile(nominal.control_compute_ms, 99)
    max_ms = float(np.max(nominal.control_compute_ms))
    p99_within_limit = p99_ms <= 1.80
    max_within_deadline = max_ms < 2.0
    passed = (
        paced
        and nominal.trace_buffers_prefaulted
        and nominal.control_deadline_miss_count == 0
        and p99_within_limit
        and max_within_deadline
    )
    return {
        "scope": CONTROL_HARD_SCOPE,
        "paced": bool(paced),
        "samples": nominal.tick_count,
        "deadline_ms": 2.0,
        "p99_limit_ms": 1.80,
        "p50_ms": p50_ms,
        "p95_ms": p95_ms,
        "p99_ms": p99_ms,
        "max_ms": max_ms,
        "deadline_miss_count": nominal.control_deadline_miss_count,
        "p99_within_limit": p99_within_limit,
        "max_within_deadline": max_within_deadline,
        "prefault_required": True,
        "prefault_verified": nominal.trace_buffers_prefaulted,
        "pass": passed,
    }


def simulator_cycle_timing(nominal: NominalPhaseResult) -> dict[str, object]:
    """Report simulator/oracle/physics timing without gating control hard pass."""

    cycle_compute_misses = int(
        np.count_nonzero(nominal.cycle_wall_ms >= 2.0)
    )
    absolute_misses = int(
        np.count_nonzero(nominal.absolute_finish_lateness_ms > 0.0)
    )
    return {
        "scope": SIMULATOR_CYCLE_SCOPE,
        "diagnostic_only": True,
        "samples": nominal.tick_count,
        "physics_substeps_per_control_tick": (
            P0_V8_PHYSICS_HZ // P0_V8_CONTROL_HZ
        ),
        "oracle_snapshot_ms": timing_distribution(nominal.oracle_snapshot_ms),
        "command_apply_and_physics_ms": timing_distribution(
            nominal.command_apply_and_physics_ms
        ),
        "cycle_wall_ms": timing_distribution(nominal.cycle_wall_ms),
        "release_lateness_ms": timing_distribution(nominal.release_lateness_ms),
        "absolute_finish_lateness_ms": timing_distribution(
            nominal.absolute_finish_lateness_ms
        ),
        "cycle_compute_deadline_miss_count": cycle_compute_misses,
        "absolute_deadline_miss_count": absolute_misses,
        "meets_500hz_diagnostic": (
            cycle_compute_misses == 0 and absolute_misses == 0
        ),
    }


def wall_timing(nominal: NominalPhaseResult, *, paced: bool) -> dict[str, object]:
    """Compatibility name for profile sweeps; returns the v2 hard lane."""

    return control_hard_timing(nominal, paced=paced)


def phase_dir_name(spec: PhaseSpec) -> str:
    return f"phase_{spec.sequence_index}_{int(spec.duration_s)}s"


def write_phase_artifacts(
    *,
    output_dir: Path,
    spec: PhaseSpec,
    nominal: NominalPhaseResult,
    faults: Sequence[Mapping[str, object]],
    source_binding: Mapping[str, object],
    base_state: SimulatorState,
    plant_manifest: Mapping[str, Any],
    pace_wall_clock: bool,
    prewarm_binding: Mapping[str, object],
) -> tuple[Path, dict[str, object]]:
    phase_dir = output_dir / phase_dir_name(spec)
    phase_dir.mkdir(parents=True, exist_ok=False)
    trace_path = phase_dir / "control_trace.npz"
    np.savez_compressed(
        trace_path,
        sequence=np.arange(nominal.tick_count, dtype=np.int64),
        sim_time_s=nominal.sim_time_s,
        control_compute_ms=nominal.control_compute_ms,
        oracle_snapshot_ms=nominal.oracle_snapshot_ms,
        command_apply_and_physics_ms=nominal.command_apply_and_physics_ms,
        cycle_wall_ms=nominal.cycle_wall_ms,
        release_lateness_ms=nominal.release_lateness_ms,
        absolute_finish_lateness_ms=nominal.absolute_finish_lateness_ms,
        qdot=nominal.qdot,
        command_jacobian=nominal.command_jacobian,
        desired_twist=nominal.desired_twist,
        reaction_normal=nominal.reaction_normal,
        approach_normal=nominal.approach_normal,
        wrench=nominal.wrench,
        native_contact_count=nominal.native_contact_count,
        cage_collision_count=nominal.cage_collision_count_per_tick,
        tcp_inside_cage=nominal.tcp_inside_cage,
        accepted=nominal.accepted,
        deadline_rejected=nominal.deadline_rejected,
        command_stop_request=nominal.command_stop_request,
        command_bytes_sha256=nominal.command_bytes_sha256,
        action=nominal.actions,
        reason=nominal.reasons,
        deferred_numeric=nominal.deferred.numeric[: nominal.deferred.count],
        deferred_reason=np.asarray(
            nominal.deferred.reasons[: nominal.deferred.count], dtype="<U160"
        ),
        deferred_action=np.asarray(
            nominal.deferred.actions[: nominal.deferred.count], dtype="<U96"
        ),
    )
    trace_binding = {
        "role": "control_trace_npz",
        "path": trace_path.name,
        "sha256": sha256_path(trace_path),
        "size_bytes": trace_path.stat().st_size,
    }
    physics_provenance = str(
        plant_manifest.get("claim_boundary", {}).get(
            "physics_provenance", "geometry_provisional"
        )
    )
    control_hard = control_hard_timing(nominal, paced=pace_wall_clock)
    cycle_diagnostic = simulator_cycle_timing(nominal)
    blockers = sorted(
        set(str(value) for value in plant_manifest.get("blockers", []))
        | {
            "p0_sim_physics_pass_false_geometry_provisional",
            "offline_simulation_cannot_promote_live_state",
        }
        | (
            {"control_hard_500hz_gate_failed"}
            if control_hard["pass"] is not True
            else set()
        )
    )
    evidence: dict[str, object] = {
        "schema": EVIDENCE_SCHEMA_V4,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile": dict(PROFILE),
        "engine": {
            "name": "mujoco",
            "version": base_state.engine_version,
            "lane": "p0_v8_no_contact_air_motion",
            "physics_provenance": physics_provenance,
            "command_jacobian_source": "calibrated_pinocchio",
            "engine_oracle_is_command_source": False,
        },
        "source_binding": dict(source_binding),
        "schedule": {
            "physics_hz": P0_V8_PHYSICS_HZ,
            "control_hz": P0_V8_CONTROL_HZ,
            "dbil_hz": P0_V8_DBIL_HZ,
            "integer_schedule": True,
            "sim_tick_miss_count": int(abs(nominal.sim_time_drift_s) > 1e-9),
        },
        "phase": {
            "duration_s": float(spec.duration_s),
            "sequence_index": spec.sequence_index,
            "same_fingerprint_as_previous": spec.sequence_index > 0,
        },
        "prewarm_binding": dict(prewarm_binding),
        "control_hard_500hz": control_hard,
        "simulator_cycle_diagnostic": cycle_diagnostic,
        "nominal": {
            "tick_count": nominal.tick_count,
            "accepted_tick_count": nominal.accepted_tick_count,
            "safe_hold_count": nominal.safe_hold_count,
            "stop_count": nominal.stop_count,
            "missed_sequence_count": 0,
            "nonfinite_output_count": nominal.nonfinite_output_count,
            "qdot_bound_violation_count": nominal.qdot_bound_violation_count,
            "unexpected_contact_count": nominal.unexpected_contact_count,
            "cage_collision_count": nominal.cage_collision_count,
            "control_deadline_miss_count": nominal.control_deadline_miss_count,
            "cycle_compute_deadline_miss_count": (
                nominal.cycle_compute_deadline_miss_count
            ),
            "absolute_deadline_miss_count": nominal.absolute_deadline_miss_count,
            "max_qdot_abs_rad_s": nominal.max_qdot_abs_rad_s,
            "exact_zero_rejection_count": nominal.exact_zero_rejection_count,
            "deadline_rejection_count": nominal.deadline_rejection_count,
            "deadline_zero_rejection_count": (
                nominal.deadline_zero_rejection_count
            ),
            "nonzero_rejection_count": nominal.nonzero_rejection_count,
            "control_path_diagnostic_pass": nominal.control_path_pass,
            "sim_clock": {
                "physics_tick_count": nominal.physics_tick_count,
                "control_tick_count": nominal.tick_count,
                "dbil_tick_count": nominal.dbil_tick_count,
                "start_s": nominal.sim_time_start_s,
                "end_s": nominal.sim_time_end_s,
                "drift_s": nominal.sim_time_drift_s,
                "first_sequence": nominal.first_sequence,
                "last_sequence": nominal.last_sequence,
            },
        },
        "faults": list(faults),
        "control_contract": {
            "path": CONTROL_PATH,
            "dls_shadow_only": True,
            "exact_zero_rejection": True,
            "same_production_code": True,
            "hot_loop_gc_disabled": True,
            "gc_state_restored_after_loop": True,
            "timing_scope_version": TIMING_SCOPE_VERSION,
            "trace_buffers_prefaulted": nominal.trace_buffers_prefaulted,
            "measured_samples_excluded": 0,
            "prewarm_samples_in_control_trace": 0,
            "measured_sequence_restarts_at_zero": True,
            "deadline_command_contract_version": (
                DEADLINE_COMMAND_CONTRACT_VERSION
            ),
            "deadline_classification_point": (
                "after_adapter_step_before_plant_write"
            ),
            "deadline_comparison": "control_elapsed_ms_gte_deadline",
            "deadline_ms": CONTROL_DEADLINE_MS,
            "deadline_rejection_action": "stop",
            "deadline_rejection_reason": CONTROL_DEADLINE_REASON,
            "deadline_rejection_qdot": list(ZERO6),
            "deadline_rejection_cmd_valid": False,
            "deadline_rejection_stop_request": True,
            "deadline_rejection_may_satisfy_timing_gate": False,
        },
        "claims": {
            "p0_sim_physics_pass": False,
            "p0_ursim_protocol_pass": False,
            "contact_sim_pass": False,
            "direct_torque_ursim_software_pass": False,
            "v30_offline_ready": False,
        },
        "claim_boundary": simulation_claim_boundary(),
        "artifacts": [trace_binding],
        "blockers": blockers,
    }
    evidence_path = phase_dir / "evidence.json"
    write_json(evidence_path, evidence)
    return evidence_path, evidence


def parse_phases(values: Sequence[float]) -> list[PhaseSpec]:
    durations = tuple(float(value) for value in values)
    if not durations or durations != P0_V8_HISTORICAL_DIAGNOSTIC_PHASES_S[: len(durations)]:
        raise ValueError("phases must match the retained historical offline diagnostic prefix")
    return [
        PhaseSpec(duration_s=duration, sequence_index=index)
        for index, duration in enumerate(durations)
    ]


def runner_exit_code(
    *,
    control_diagnostic_pass: bool,
    complete: bool,
    control_hard_gate_pass: bool,
) -> int:
    """Keep partial smoke usable while making a failed final hard gate nonzero."""

    if not control_diagnostic_pass:
        return 3
    if complete and not control_hard_gate_pass:
        return 4
    return 0


def run_nominal_measurement_sequence(
    *,
    plant: VelocityPlant,
    solver: Any,
    specs: Sequence[PhaseSpec],
    pace_wall_clock: bool,
    release_spin_window_s: float,
) -> list[tuple[PhaseSpec, NominalPhaseResult]]:
    """Complete every nominal phase before any fault work or artifact I/O.

    Each phase still owns an independent plant reset and sequence-zero trace.
    Keeping only the bounded in-memory results here avoids fault injection and
    compressed NPZ/JSON writes between retained historical diagnostic lanes.
    """

    measured: list[tuple[PhaseSpec, NominalPhaseResult]] = []
    for spec in specs:
        measured.append(
            (
                spec,
                run_nominal_phase(
                    plant=plant,
                    solver=solver,
                    spec=spec,
                    pace_wall_clock=pace_wall_clock,
                    release_spin_window_s=release_spin_window_s,
                    plant_already_reset=spec.sequence_index == 0,
                ),
            )
        )
    return measured


def final_60_control_hard_gate_pass(
    phase_entries: Sequence[Mapping[str, object]],
    *,
    complete: bool,
) -> bool:
    """Accept timing only from the original complete 60 second phase."""

    final_60_entry = next(
        (entry for entry in phase_entries if entry.get("duration_s") == 60.0),
        None,
    )
    return bool(
        complete
        and final_60_entry is not None
        and final_60_entry.get("control_hard_500hz_pass") is True
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--phases", nargs="+", type=float,
        default=list(P0_V8_HISTORICAL_DIAGNOSTIC_PHASES_S),
        help="historical offline diagnostic phases; never the active live canary contract",
    )
    parser.add_argument(
        "--pace-wall-clock",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="pace releases at 500 Hz; sim-time counters remain authoritative",
    )
    parser.add_argument(
        "--release-spin-window-s",
        type=float,
        default=0.00025,
        help="busy-spin tail before each paced release; max is one 2 ms period",
    )
    args = parser.parse_args()

    require_numeric_thread_environment()
    root = EXPERIMENT_ROOT.resolve()
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise SystemExit(f"refusing to overwrite output directory: {output_dir}")
    specs = parse_phases(args.phases)

    plant = MuJoCoVelocityPlant(
        args.model_manifest.resolve(),
        output_key="no_contact_velocity",
    )
    no_contact_lane = require_hash_bound_no_contact_lane(plant)
    solver = make_solver(root)
    source_binding = build_source_binding(
        root=root,
        plant=plant,
        solver=solver,
        no_contact_lane=no_contact_lane,
        pace_wall_clock=args.pace_wall_clock,
        release_spin_window_s=args.release_spin_window_s,
    )
    output_dir.mkdir(parents=True)
    prewarm_result = run_production_path_prewarm(
        plant=plant,
        solver=solver,
        release_spin_window_s=args.release_spin_window_s,
    )
    reset_state = reset_after_production_path_prewarm(
        plant=plant,
        solver=solver,
    )
    prewarm_path, prewarm_payload, prewarm_binding = (
        write_production_path_prewarm_artifact(
            output_dir=output_dir,
            result=prewarm_result,
            source_binding=source_binding,
            reset_state=reset_state,
        )
    )
    prewarm_blockers = validate_prewarm_evidence(prewarm_payload)
    if prewarm_blockers:
        raise RuntimeError(
            "production-path prewarm failed: " + "; ".join(prewarm_blockers)
        )
    nominal_phase_results = run_nominal_measurement_sequence(
        plant=plant,
        solver=solver,
        specs=specs,
        pace_wall_clock=args.pace_wall_clock,
        release_spin_window_s=args.release_spin_window_s,
    )

    # Measured GPU work is now complete.  Fault matrices and artifact I/O stay
    # outside every nominal loop and remain independent for each phase.
    phase_entries: list[dict[str, object]] = []
    all_valid = True
    for spec, nominal in nominal_phase_results:
        faults = run_fault_matrix(plant=plant, solver=solver)
        plant.reset()
        base_state = plant.read_state(sequence=0, wall_time_s=0.0)
        evidence_path, evidence = write_phase_artifacts(
            output_dir=output_dir,
            spec=spec,
            nominal=nominal,
            faults=faults,
            source_binding=source_binding,
            base_state=base_state,
            plant_manifest=plant.manifest,
            pace_wall_clock=args.pace_wall_clock,
            prewarm_binding=prewarm_binding,
        )
        blockers = validate_phase_evidence(
            evidence,
            artifact_root=evidence_path.parent,
        )
        phase_valid = not blockers
        all_valid &= phase_valid
        phase_control_hard = control_hard_timing(
            nominal,
            paced=args.pace_wall_clock,
        )
        phase_cycle = simulator_cycle_timing(nominal)
        phase_entries.append(
            {
                "duration_s": spec.duration_s,
                "sequence_index": spec.sequence_index,
                "evidence_path": str(evidence_path.relative_to(output_dir)),
                "evidence_sha256": sha256_path(evidence_path),
                "evidence_size_bytes": evidence_path.stat().st_size,
                "structurally_valid": phase_valid,
                "validation_blockers": blockers,
                "control_path_diagnostic_pass": nominal.control_path_pass,
                "control_hard_500hz_pass": phase_control_hard["pass"],
                "control_deadline_miss_count": phase_control_hard[
                    "deadline_miss_count"
                ],
                "deadline_rejection_count": nominal.deadline_rejection_count,
                "deadline_zero_rejection_count": (
                    nominal.deadline_zero_rejection_count
                ),
                "nonzero_rejection_count": nominal.nonzero_rejection_count,
                "simulator_cycle_meets_500hz_diagnostic": phase_cycle[
                    "meets_500hz_diagnostic"
                ],
                "cycle_compute_deadline_miss_count": phase_cycle[
                    "cycle_compute_deadline_miss_count"
                ],
                "absolute_deadline_miss_count": phase_cycle[
                    "absolute_deadline_miss_count"
                ],
            }
        )

    complete = len(specs) == len(P0_V8_HISTORICAL_DIAGNOSTIC_PHASES_S)
    control_diagnostic_pass = all_valid and all(
        bool(entry["control_path_diagnostic_pass"]) for entry in phase_entries
    )
    control_hard_gate_pass = final_60_control_hard_gate_pass(
        phase_entries,
        complete=complete,
    )
    control_hard_gate = {
        "scope": CONTROL_HARD_SCOPE,
        "required_phase_duration_s": 60.0,
        "deadline_ms": 2.0,
        "p99_limit_ms": 1.80,
        "requires_zero_deadline_misses": True,
        "requires_prefault": True,
        "phase_results": [
            {
                "duration_s": entry["duration_s"],
                "deadline_miss_count": entry["control_deadline_miss_count"],
                "pass": entry["control_hard_500hz_pass"],
            }
            for entry in phase_entries
        ],
        "complete_sequence_evaluated": complete,
        "pass": control_hard_gate_pass,
    }
    cycle_diagnostic = {
        "scope": SIMULATOR_CYCLE_SCOPE,
        "diagnostic_only": True,
        "phase_results": [
            {
                "duration_s": entry["duration_s"],
                "cycle_compute_deadline_miss_count": entry[
                    "cycle_compute_deadline_miss_count"
                ],
                "absolute_deadline_miss_count": entry[
                    "absolute_deadline_miss_count"
                ],
                "meets_500hz_diagnostic": entry[
                    "simulator_cycle_meets_500hz_diagnostic"
                ],
            }
            for entry in phase_entries
        ],
    }
    if control_diagnostic_pass and complete and control_hard_gate_pass:
        result = "diagnostic_pass"
    elif control_diagnostic_pass and complete:
        result = "control_diagnostic_pass_control_hard_500hz_blocked"
    elif control_diagnostic_pass:
        result = "diagnostic_partial_pass"
    else:
        result = "diagnostic_fail"
    manifest_blockers = (
        set(str(value) for value in plant.manifest.get("blockers", []))
        | {"geometry_provisional_no_p0_physics_claim"}
    )
    if control_diagnostic_pass and complete and not control_hard_gate_pass:
        manifest_blockers.add("control_hard_500hz_gate_failed_60s")
    manifest = {
        "schema": RUN_SCHEMA_V4,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile": dict(PROFILE),
        "source_composite_sha256": source_binding["composite_sha256"],
        "production_path_prewarm": prewarm_binding,
        "canonical_phase_sequence_complete": complete,
        "phases": phase_entries,
        "control_hard_500hz_gate": control_hard_gate,
        "simulator_cycle_diagnostic": cycle_diagnostic,
        "result": result,
        "claims": {
            "p0_sim_physics_pass": False,
            "live_accepted": False,
            "reproduction_complete": False,
        },
        "claim_boundary": simulation_claim_boundary(),
        "blockers": sorted(manifest_blockers),
    }
    manifest_path = output_dir / "run_manifest.json"
    write_json(manifest_path, manifest)
    print(json.dumps({"manifest": str(manifest_path), **manifest}, indent=2, sort_keys=True))
    return runner_exit_code(
        control_diagnostic_pass=control_diagnostic_pass,
        complete=complete,
        control_hard_gate_pass=control_hard_gate_pass,
    )


if __name__ == "__main__":
    raise SystemExit(main())
