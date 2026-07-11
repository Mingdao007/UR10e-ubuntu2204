#!/usr/bin/env python3
"""Engine-neutral adapter for the production Step5d v30 control path.

Engine plug-ins may only provide simulator state and consume the resulting
command.  They must not call the strict-RNN solver, DLS, or SafetyEnvelope
directly.  The adapter is intentionally free of MuJoCo, Gazebo, ROS, RTDE, and
controller imports so its fail-closed behavior can be tested everywhere.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from dataclasses import dataclass, field, replace
from typing import Mapping, Protocol, runtime_checkable

import numpy as np

from step5d_control_contract import (
    ZERO6,
    ControlPolicy,
    DeferredV30Diagnostics,
    Matrix3,
    Matrix6,
    SafetyEnvelope,
    Step5dControlStepResult,
    Step5dObservation,
    Vector3,
    Vector6,
    fail_closed_control_step,
    step5d_v30_control_step,
)


P0_V8_PROFILE = "step5d_strict_rnn_no_contact_p0_v8"
P0_V8_EFFECTIVE_KO = 0.01
P0_V8_CONTROL_HZ = 500
P0_V8_PHYSICS_HZ = 2_000
P0_V8_DBIL_HZ = 200
P0_V8_QDOT_CAP_RAD_S = 0.05
P0_V8_EPSILON = 0.010
P0_V8_SIGR_EXPONENT_R = 0.8
# Review-v2 timing recovery revision.  The frozen 1024-iteration evidence stays
# historical. The current P0/v30 candidate returns to the 128-step profile:
# the 32-step formal Ubuntu run reduced solver time but increased full-tick and
# schedule misses, so it remains negative selection evidence. qdot and every
# safety gate are unchanged.
P0_V8_INNER_ITERATIONS = 128
P0_V8_FORCE_GUARD_N = 5.0
P0_V8_TORQUE_GUARD_NM = 3.0
P0_V8_SENSOR_STALE_S = 0.100
P0_V8_CANARY_PHASES_S = (2.0, 10.0, 60.0)

SIMULATION_CLAIMS = (
    "p0_sim_physics_pass",
    "p0_ursim_protocol_pass",
    "contact_sim_pass",
    "direct_torque_ursim_software_pass",
    "v30_offline_ready",
)

SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class IntegerRateSchedule:
    """Drift-free integer schedule for physics, control, and DBIL clocks."""

    physics_hz: int = P0_V8_PHYSICS_HZ
    control_hz: int = P0_V8_CONTROL_HZ
    dbil_hz: int = P0_V8_DBIL_HZ

    def __post_init__(self) -> None:
        if min(self.physics_hz, self.control_hz, self.dbil_hz) <= 0:
            raise ValueError("simulation rates must be positive")
        if self.physics_hz % self.control_hz:
            raise ValueError("physics/control schedule must be integer")
        if self.physics_hz % self.dbil_hz:
            raise ValueError("physics/DBIL schedule must be integer")

    @property
    def control_stride(self) -> int:
        return self.physics_hz // self.control_hz

    @property
    def dbil_stride(self) -> int:
        return self.physics_hz // self.dbil_hz

    def is_control_tick(self, physics_tick: int) -> bool:
        return int(physics_tick) >= 0 and int(physics_tick) % self.control_stride == 0

    def is_dbil_tick(self, physics_tick: int) -> bool:
        return int(physics_tick) >= 0 and int(physics_tick) % self.dbil_stride == 0


@dataclass(frozen=True)
class FrameLineage:
    """Frame provenance supplied by an engine-specific state adapter."""

    command_frame: str
    pose_frame: str
    twist_frame: str
    wrench_frame: str
    jacobian_frame: str
    normal_frame: str
    transform_chain: tuple[str, ...]
    sha256: str

    def validate(self) -> str | None:
        if not self.command_frame:
            return "frame_lineage_missing_command_frame"
        canonical = (
            self.pose_frame,
            self.twist_frame,
            self.wrench_frame,
            self.jacobian_frame,
        )
        if any(frame != self.command_frame for frame in canonical):
            return "frame_lineage_not_canonicalized"
        if not self.normal_frame:
            return "frame_lineage_missing_normal_frame"
        if not self.transform_chain:
            return "frame_lineage_transform_chain_missing"
        if SHA256_RE.fullmatch(self.sha256) is None:
            return "frame_lineage_hash_invalid"
        return None


@dataclass(frozen=True)
class SimulatorState:
    """Canonical state delivered at one 500 Hz control tick."""

    engine: str
    engine_version: str
    sequence: int
    sim_time_s: float
    wall_time_s: float
    observation_age_s: float
    q: Vector6
    qd: Vector6
    tcp_pose: Vector6
    tcp_twist: Vector6
    wrench: Vector6
    command_jacobian: Matrix6
    desired_twist: Vector6
    reaction_normal: Vector3
    approach_normal: Vector3
    frame_lineage: FrameLineage
    calibration_hash: str
    model_hash: str
    normal_to_command_rotation: Matrix3 | None = None
    oracle_tcp_pose: Vector6 | None = None
    oracle_jacobian: Matrix6 | None = None
    omega_minus: Vector6 = (-P0_V8_QDOT_CAP_RAD_S,) * 6
    omega_plus: Vector6 = (P0_V8_QDOT_CAP_RAD_S,) * 6
    path_time_s: float = 0.0
    force_error_n: float = 0.0
    orientation_error_rad: float = 0.0
    dt_s: float = 1.0 / P0_V8_CONTROL_HZ
    native_contact_count: int = 0
    cage_collision_count: int = 0
    tcp_inside_cage: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_observation(self) -> Step5dObservation:
        return Step5dObservation(
            sequence=int(self.sequence),
            timestamp_s=float(self.sim_time_s),
            q=self.q,
            qd=self.qd,
            tcp_pose=self.tcp_pose,
            tcp_twist=self.tcp_twist,
            wrench=self.wrench,
            jacobian=self.command_jacobian,
            desired_twist=self.desired_twist,
            reaction_normal=self.reaction_normal,
            approach_normal=self.approach_normal,
            command_frame=self.frame_lineage.command_frame,
            normal_frame=self.frame_lineage.normal_frame,
            normal_to_command_rotation=self.normal_to_command_rotation,
            path_time_s=float(self.path_time_s),
            force_error_n=float(self.force_error_n),
            orientation_error_rad=float(self.orientation_error_rad),
            omega_minus=self.omega_minus,
            omega_plus=self.omega_plus,
            dt_s=float(self.dt_s),
        )


@dataclass(frozen=True)
class SimulationCommand:
    """The only command value an engine sink may consume."""

    engine: str
    sequence: int
    mode: str
    qdot: Vector6
    accepted: bool
    action: str
    reason: str
    stop_request: bool
    command_bytes_sha256: str

    @staticmethod
    def from_control_result(
        state: SimulatorState,
        result: Step5dControlStepResult,
    ) -> "SimulationCommand":
        qdot = tuple(float(value) for value in result.register_command.qdot)
        encoded = struct.pack("<6d??", *qdot, result.register_command.cmd_valid, result.register_command.stop_request)
        return SimulationCommand(
            engine=state.engine,
            sequence=int(state.sequence),
            mode="joint_velocity",
            qdot=qdot,  # type: ignore[arg-type]
            accepted=result.decision.accepted,
            action=result.decision.action,
            reason=result.decision.reason,
            stop_request=result.register_command.stop_request,
            command_bytes_sha256=hashlib.sha256(encoded).hexdigest(),
        )


@dataclass(frozen=True)
class AdapterTickResult:
    state: SimulatorState
    observation: Step5dObservation
    control: Step5dControlStepResult
    simulation_command: SimulationCommand
    ingress_reason: str
    command_jacobian_source: str = "calibrated_pinocchio"
    engine_oracle_command_source: bool = False


@dataclass
class Step5dIngressGuard:
    """Stateful sequence, watchdog, frame, and physical P0 guard."""

    expected_period_s: float = 1.0 / P0_V8_CONTROL_HZ
    timing_tolerance_s: float = 0.00025
    max_observation_age_s: float = P0_V8_SENSOR_STALE_S
    force_guard_n: float = P0_V8_FORCE_GUARD_N
    torque_guard_nm: float = P0_V8_TORQUE_GUARD_NM
    forbid_contact: bool = True
    last_sequence: int | None = None
    last_sim_time_s: float | None = None
    latched_stop_reason: str | None = None

    def inspect(self, state: SimulatorState) -> str | None:
        if self.latched_stop_reason is not None:
            return f"latched:{self.latched_stop_reason}"
        reason = self._inspect_unlatched(state)
        if reason is not None:
            self.latched_stop_reason = reason
            return reason
        self.last_sequence = int(state.sequence)
        self.last_sim_time_s = float(state.sim_time_s)
        return None

    def _inspect_unlatched(self, state: SimulatorState) -> str | None:
        if self.last_sequence is None:
            if int(state.sequence) != 0:
                return "sequence_initial_not_zero"
        elif int(state.sequence) != self.last_sequence + 1:
            if int(state.sequence) == self.last_sequence:
                return "sequence_duplicate"
            if int(state.sequence) < self.last_sequence:
                return "sequence_reordered"
            return "sequence_gap"

        scalars = (
            state.sim_time_s,
            state.wall_time_s,
            state.observation_age_s,
            state.dt_s,
            state.path_time_s,
            state.force_error_n,
            state.orientation_error_rad,
        )
        if any(not math.isfinite(float(value)) for value in scalars):
            return "nonfinite_simulator_state"
        if state.observation_age_s < 0.0 or state.observation_age_s > self.max_observation_age_s:
            return "observation_watchdog_stale"
        if self.last_sim_time_s is not None:
            delta = float(state.sim_time_s) - self.last_sim_time_s
            if delta <= 0.0:
                return "sim_time_not_monotonic"
            if abs(delta - self.expected_period_s) > self.timing_tolerance_s:
                return "control_tick_jitter"
        if abs(float(state.dt_s) - self.expected_period_s) > self.timing_tolerance_s:
            return "control_dt_mismatch"

        lineage_error = state.frame_lineage.validate()
        if lineage_error is not None:
            return lineage_error
        if not state.calibration_hash:
            return "calibration_hash_missing"
        if SHA256_RE.fullmatch(state.model_hash) is None:
            return "model_hash_invalid"

        arrays = (
            (state.q, (6,)),
            (state.qd, (6,)),
            (state.tcp_pose, (6,)),
            (state.tcp_twist, (6,)),
            (state.wrench, (6,)),
            (state.command_jacobian, (6, 6)),
            (state.desired_twist, (6,)),
            (state.reaction_normal, (3,)),
            (state.approach_normal, (3,)),
            (state.omega_minus, (6,)),
            (state.omega_plus, (6,)),
        )
        for values, shape in arrays:
            array = np.asarray(values, dtype=float)
            if array.shape != shape or not np.all(np.isfinite(array)):
                return "nonfinite_simulator_state"

        wrench = np.asarray(state.wrench, dtype=float)
        if float(np.linalg.norm(wrench[:3])) > self.force_guard_n + 1e-12:
            return "force_guard_exceeded"
        if float(np.linalg.norm(wrench[3:])) > self.torque_guard_nm + 1e-12:
            return "torque_guard_exceeded"
        if self.forbid_contact and int(state.native_contact_count) != 0:
            return "unexpected_native_contact"
        if int(state.cage_collision_count) != 0:
            return "tcp_cage_collision"
        if not state.tcp_inside_cage:
            return "tcp_outside_cage"
        return None


@runtime_checkable
class SimulatorStateSource(Protocol):
    def read_state(self) -> SimulatorState | None:
        ...


@runtime_checkable
class SimulationCommandSink(Protocol):
    def write_command(self, command: SimulationCommand) -> None:
        ...


class Step5dSimulatorAdapter:
    """Single production-code adapter used by every simulation engine."""

    def __init__(
        self,
        *,
        policy: ControlPolicy,
        safety_envelope: SafetyEnvelope,
        deferred_diagnostics: DeferredV30Diagnostics,
        ingress_guard: Step5dIngressGuard | None = None,
    ) -> None:
        self.policy = policy
        self.safety_envelope = safety_envelope
        self.deferred_diagnostics = deferred_diagnostics
        self.ingress_guard = ingress_guard or Step5dIngressGuard()
        self.previous_qdot: Vector6 | None = None
        self.last_state: SimulatorState | None = None

    def step(self, state: SimulatorState | None) -> AdapterTickResult:
        if state is None:
            state = self._synthetic_missing_state()
            ingress_reason = "missing_simulator_observation"
            self.ingress_guard.latched_stop_reason = ingress_reason
        else:
            ingress_reason = self.ingress_guard.inspect(state) or "ok"
        observation = state.to_observation()
        if ingress_reason == "ok":
            control = step5d_v30_control_step(
                observation,
                self.policy,
                previous_qdot=self.previous_qdot,
                safety_envelope=self.safety_envelope,
                deferred_diagnostics=self.deferred_diagnostics,
            )
        else:
            control = fail_closed_control_step(
                observation,
                reason=f"simulator_ingress_failure:{ingress_reason}",
                deferred_diagnostics=self.deferred_diagnostics,
            )
        command = SimulationCommand.from_control_result(state, control)
        if not control.decision.accepted and command.qdot != ZERO6:
            raise RuntimeError("simulator rejection did not reach exact-zero sink command")
        self.previous_qdot = control.decision.qdot if control.decision.accepted else None
        self.last_state = state
        return AdapterTickResult(
            state=state,
            observation=observation,
            control=control,
            simulation_command=command,
            ingress_reason=ingress_reason,
        )

    def run_one_tick(
        self,
        source: SimulatorStateSource,
        sink: SimulationCommandSink,
    ) -> AdapterTickResult:
        result = self.step(source.read_state())
        sink.write_command(result.simulation_command)
        return result

    def _synthetic_missing_state(self) -> SimulatorState:
        if self.last_state is not None:
            return replace(
                self.last_state,
                sequence=self.last_state.sequence + 1,
                sim_time_s=self.last_state.sim_time_s + 1.0 / P0_V8_CONTROL_HZ,
                observation_age_s=P0_V8_SENSOR_STALE_S + 1.0,
            )
        identity6: Matrix6 = tuple(
            tuple(float(row == column) for column in range(6))
            for row in range(6)
        )  # type: ignore[assignment]
        lineage = FrameLineage(
            command_frame="base",
            pose_frame="base",
            twist_frame="base",
            wrench_frame="base",
            jacobian_frame="base",
            normal_frame="base",
            transform_chain=("synthetic_missing_observation",),
            sha256="0" * 64,
        )
        return SimulatorState(
            engine="missing",
            engine_version="missing",
            sequence=0,
            sim_time_s=0.0,
            wall_time_s=0.0,
            observation_age_s=P0_V8_SENSOR_STALE_S + 1.0,
            q=ZERO6,
            qd=ZERO6,
            tcp_pose=ZERO6,
            tcp_twist=ZERO6,
            wrench=ZERO6,
            command_jacobian=identity6,
            desired_twist=(0.0, 0.0, 0.001, 0.0, 0.0, 0.0),
            reaction_normal=(0.0, 0.0, -1.0),
            approach_normal=(0.0, 0.0, 1.0),
            frame_lineage=lineage,
            calibration_hash="missing",
            model_hash="0" * 64,
        )


def assert_p0_v8_profile(profile: Mapping[str, object]) -> None:
    """Reject silent drift from the frozen P0 v8 production profile."""

    expected: Mapping[str, object] = {
        "backend": "cupy",
        "inner_iterations": P0_V8_INNER_ITERATIONS,
        "epsilon": P0_V8_EPSILON,
        "sigr_exponent_r": P0_V8_SIGR_EXPONENT_R,
        "qdot_cap_rad_s": P0_V8_QDOT_CAP_RAD_S,
        "effective_ko": P0_V8_EFFECTIVE_KO,
        "dls_runtime_fallback_allowed": False,
    }
    for key, value in expected.items():
        actual = profile.get(key)
        if isinstance(value, float):
            try:
                matches = math.isclose(float(actual), value, abs_tol=1e-12)
            except (TypeError, ValueError):
                matches = False
        else:
            matches = actual == value
        if not matches:
            raise ValueError(f"P0 v8 profile drift: {key}={actual!r}, expected {value!r}")


def simulation_claim_boundary() -> dict[str, object]:
    """Canonical non-promotion boundary for every simulator artifact."""

    return {
        "workflow_state": "liveprep_blocked",
        "current_program": "step5d_strict_rnn_ablation_v29",
        "v30_active": False,
        "live_motion_authorized": False,
        "package_accepted": False,
        "live_accepted": False,
        "reproduction_complete": False,
        "sim_pass_cannot_promote_live_state": True,
    }
