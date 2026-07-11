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
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import numpy as np

from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver
from step5d_control_contract import (
    STRICT_RNN_SOLVER_OK_STATUS,
    ZERO6,
    ControlCandidate,
    DeferredV30Diagnostics,
    SafetyEnvelope,
    StrictRnnControlPolicy,
)
from step5d_simulator_adapter import (
    P0_V8_CANARY_PHASES_S,
    P0_V8_CONTROL_HZ,
    P0_V8_DBIL_HZ,
    P0_V8_EFFECTIVE_KO,
    P0_V8_EPSILON,
    P0_V8_INNER_ITERATIONS,
    P0_V8_PHYSICS_HZ,
    P0_V8_QDOT_CAP_RAD_S,
    P0_V8_SIGR_EXPONENT_R,
    IntegerRateSchedule,
    SimulatorState,
    Step5dSimulatorAdapter,
    simulation_claim_boundary,
)
from ur10e_mujoco_adapter import MuJoCoVelocityPlant
from verify_step5d_sim_evidence import (
    P0_REQUIRED_FAULTS,
    source_composite_sha256,
    validate_evidence,
)


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
    "config/step5d_liveprep_solver_gate.json",
)


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
class NominalPhaseResult:
    tick_count: int
    accepted_tick_count: int
    safe_hold_count: int
    stop_count: int
    nonfinite_output_count: int
    qdot_bound_violation_count: int
    unexpected_contact_count: int
    cage_collision_count: int
    deadline_miss_count: int
    max_qdot_abs_rad_s: float
    exact_zero_rejection_count: int
    physics_tick_count: int
    dbil_tick_count: int
    sim_time_start_s: float
    sim_time_end_s: float
    sim_time_drift_s: float
    first_sequence: int
    last_sequence: int
    compute_ms: np.ndarray
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
    actions: np.ndarray
    reasons: np.ndarray
    deferred: DeferredV30Diagnostics

    @property
    def passed(self) -> bool:
        return (
            self.accepted_tick_count == self.tick_count
            and self.safe_hold_count == 0
            and self.stop_count == 0
            and self.nonfinite_output_count == 0
            and self.qdot_bound_violation_count == 0
            and self.unexpected_contact_count == 0
            and self.cage_collision_count == 0
            and self.deadline_miss_count == 0
            and self.exact_zero_rejection_count == 0
            and abs(self.sim_time_drift_s) <= 1e-9
            and float(np.percentile(self.compute_ms, 99)) <= 1.80
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
    no_contact_lane: Mapping[str, object],
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
    }
    source["composite_sha256"] = source_composite_sha256(source)
    return source


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


def run_nominal_phase(
    *,
    plant: VelocityPlant,
    solver: Any,
    spec: PhaseSpec,
    pace_wall_clock: bool = False,
) -> NominalPhaseResult:
    """Run one phase with no I/O or dynamically growing tick log in the loop."""

    schedule = IntegerRateSchedule()
    tick_count = spec.tick_count
    plant.reset()
    first_state = plant.read_state(sequence=0, wall_time_s=0.0)
    warm_solver(solver, first_state)
    policy = StrictRnnControlPolicy(solver)
    adapter = make_adapter(policy, capacity=tick_count)

    compute_ms = np.empty(tick_count, dtype=np.float64)
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
    actions: list[str | None] = [None] * tick_count
    reasons: list[str | None] = [None] * tick_count

    safe_holds = 0
    stops = 0
    nonfinite = 0
    over_cap = 0
    contacts = 0
    collisions = 0
    deadline_misses = 0
    exact_zero_rejections = 0
    physics_tick = 0
    dbil_ticks = 0
    wall_start = time.perf_counter()
    sim_start = float(first_state.sim_time_s)

    gc_was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        for index in range(tick_count):
            release = wall_start + index / P0_V8_CONTROL_HZ
            if pace_wall_clock and index:
                wait_until(release)
            started = time.perf_counter()
            state = plant.read_state(
                sequence=index,
                wall_time_s=time.perf_counter() - wall_start,
            )
            result = adapter.step(state)
            plant.write_command(result.simulation_command)
            elapsed_ms = (time.perf_counter() - started) * 1000.0

            values = np.asarray(result.simulation_command.qdot, dtype=float)
            compute_ms[index] = elapsed_ms
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
            accepted[index] = 1 if result.control.decision.accepted else 0
            actions[index] = result.control.decision.action
            reasons[index] = result.control.decision.reason
            safe_holds += int(result.control.decision.action == "safe_hold")
            stops += int(result.control.decision.action == "stop")
            nonfinite += int(not np.all(np.isfinite(values)))
            over_cap += int(
                np.all(np.isfinite(values))
                and float(np.max(np.abs(values))) > P0_V8_QDOT_CAP_RAD_S + 1e-12
            )
            contacts += int(state.native_contact_count != 0)
            collisions += int(
                state.cage_collision_count != 0 or not state.tcp_inside_cage
            )
            deadline_misses += int(elapsed_ms >= 2.0)
            exact_zero_rejections += int(
                not result.control.decision.accepted
                and result.simulation_command.qdot != ZERO6
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
        deadline_miss_count=deadline_misses,
        max_qdot_abs_rad_s=float(np.max(np.abs(qdot))),
        exact_zero_rejection_count=exact_zero_rejections,
        physics_tick_count=physics_tick,
        dbil_tick_count=dbil_ticks,
        sim_time_start_s=sim_start,
        sim_time_end_s=sim_end,
        sim_time_drift_s=sim_end - expected_end,
        first_sequence=0,
        last_sequence=tick_count - 1,
        compute_ms=compute_ms,
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
        actions=np.asarray(actions, dtype="<U96"),
        reasons=np.asarray(reasons, dtype="<U160"),
        deferred=adapter.deferred_diagnostics,
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
) -> tuple[Path, dict[str, object]]:
    phase_dir = output_dir / phase_dir_name(spec)
    phase_dir.mkdir(parents=True, exist_ok=False)
    trace_path = phase_dir / "control_trace.npz"
    np.savez_compressed(
        trace_path,
        sequence=np.arange(nominal.tick_count, dtype=np.int64),
        sim_time_s=nominal.sim_time_s,
        compute_ms=nominal.compute_ms,
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
    blockers = sorted(
        set(str(value) for value in plant_manifest.get("blockers", []))
        | {
            "p0_sim_physics_pass_false_geometry_provisional",
            "offline_simulation_cannot_promote_live_state",
        }
    )
    evidence: dict[str, object] = {
        "schema": "ur10e_simulation_evidence_v1",
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
            "deadline_miss_count": nominal.deadline_miss_count,
            "max_qdot_abs_rad_s": nominal.max_qdot_abs_rad_s,
            "exact_zero_rejection_count": nominal.exact_zero_rejection_count,
            "control_path_diagnostic_pass": nominal.passed,
            "compute_timing_ms": {
                "first": float(nominal.compute_ms[0]),
                "p50": percentile(nominal.compute_ms, 50),
                "p95": percentile(nominal.compute_ms, 95),
                "p99": percentile(nominal.compute_ms, 99),
                "max": float(np.max(nominal.compute_ms)),
            },
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
    if not durations or durations != P0_V8_CANARY_PHASES_S[: len(durations)]:
        raise ValueError("phases must be the canonical prefix: 2, then 10, then 60 seconds")
    return [
        PhaseSpec(duration_s=duration, sequence_index=index)
        for index, duration in enumerate(durations)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--phases", nargs="+", type=float, default=list(P0_V8_CANARY_PHASES_S))
    parser.add_argument(
        "--pace-wall-clock",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="pace releases at 500 Hz; sim-time counters remain authoritative",
    )
    args = parser.parse_args()

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
    source_binding = build_source_binding(
        root=root,
        plant=plant,
        no_contact_lane=no_contact_lane,
    )
    solver = make_solver(root)
    output_dir.mkdir(parents=True)
    phase_entries: list[dict[str, object]] = []
    all_valid = True
    for spec in specs:
        nominal = run_nominal_phase(
            plant=plant,
            solver=solver,
            spec=spec,
            pace_wall_clock=args.pace_wall_clock,
        )
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
        )
        blockers = validate_evidence(evidence, artifact_root=evidence_path.parent)
        phase_valid = not blockers
        all_valid &= phase_valid
        phase_entries.append(
            {
                "duration_s": spec.duration_s,
                "sequence_index": spec.sequence_index,
                "evidence_path": str(evidence_path.relative_to(output_dir)),
                "evidence_sha256": sha256_path(evidence_path),
                "evidence_size_bytes": evidence_path.stat().st_size,
                "structurally_valid": phase_valid,
                "validation_blockers": blockers,
                "control_path_diagnostic_pass": nominal.passed,
            }
        )

    complete = len(specs) == len(P0_V8_CANARY_PHASES_S)
    diagnostic_pass = all_valid and all(
        bool(entry["control_path_diagnostic_pass"]) for entry in phase_entries
    )
    manifest = {
        "schema": "step5d_p0_v8_mujoco_run_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile": dict(PROFILE),
        "source_composite_sha256": source_binding["composite_sha256"],
        "canonical_phase_sequence_complete": complete,
        "phases": phase_entries,
        "result": (
            "diagnostic_pass" if diagnostic_pass and complete
            else "diagnostic_partial_pass" if diagnostic_pass
            else "diagnostic_fail"
        ),
        "claims": {
            "p0_sim_physics_pass": False,
            "live_accepted": False,
            "reproduction_complete": False,
        },
        "claim_boundary": simulation_claim_boundary(),
        "blockers": sorted(
            set(str(value) for value in plant.manifest.get("blockers", []))
            | {"geometry_provisional_no_p0_physics_claim"}
        ),
    }
    manifest_path = output_dir / "run_manifest.json"
    write_json(manifest_path, manifest)
    print(json.dumps({"manifest": str(manifest_path), **manifest}, indent=2, sort_keys=True))
    return 0 if diagnostic_pass else 3


if __name__ == "__main__":
    raise SystemExit(main())
