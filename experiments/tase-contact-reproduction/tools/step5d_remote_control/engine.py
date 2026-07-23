"""Pure logic core for the minimal Step5d remote-control path."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from step5d_control_contract import SafetyEnvelope, Step5dObservation, StrictRnnControlPolicy, apply_direction_preserving_slew
from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver
from step5d_paper_outer_loop import (
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
    rotvec_to_matrix,
)
from .core import (
    RemoteControlStateMachine,
    ReactionNormalFilter,
    PreloadGate,
    RemotePhase,
    apply_linear_caps,
    compute_outer_force_terms,
    cycloid_xy_reference,
    desired_retract_vectors,
    desired_search_vectors,
    joint_omega_bounds,
    normalize_vector,
)
from . import primitives

FloatSeq = Sequence[float]
Vector3 = tuple[float, float, float]
Vector6 = tuple[float, float, float, float, float, float]


def _as_float(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path}: expected float")
    value_f = float(value)
    if not math.isfinite(value_f):
        raise ValueError(f"{path}: non-finite")
    return value_f


def _as_float_list(value: object, n: int, path: str) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or len(value) != n:
        raise ValueError(f"{path}: expected len {n}")
    return tuple(_as_float(v, f"{path}[{i}]") for i, v in enumerate(value))


@dataclass(frozen=True)
class KinematicSample:
    q: Vector6
    qd: Vector6
    tcp_pose: tuple[float, float, float, float, float, float]
    tcp_twist: Vector6
    wrench_tcp: Vector6
    jacobian: tuple[tuple[float, ...], ...]
    q_min: Vector6
    q_max: Vector6
    timestamp_s: float

    def __post_init__(self) -> None:
        _as_float_list(self.q, 6, "q")
        _as_float_list(self.qd, 6, "qd")
        _as_float_list(self.tcp_pose, 6, "tcp_pose")
        _as_float_list(self.tcp_twist, 6, "tcp_twist")
        _as_float_list(self.wrench_tcp, 6, "wrench_tcp")
        _as_float_list(self.q_min, 6, "q_min")
        _as_float_list(self.q_max, 6, "q_max")
        _as_float(self.timestamp_s, "timestamp_s")
        jac = np.asarray(self.jacobian, dtype=float)
        if jac.shape != (6, 6):
            raise ValueError("jacobian must be 6x6")


def _extract_qdot(candidate: object) -> Vector6:
    raw = getattr(candidate, "qdot", None)
    if raw is None and isinstance(candidate, Mapping):
        raw = candidate.get("qdot")
    if raw is None:
        raise RuntimeError("decision missing qdot")
    return tuple(float(v) for v in raw)


def _is_nonzero(twist: Vector6) -> bool:
    return any(abs(v) > 0.0 for v in twist)


def _as_twist6(value: object, path: str) -> Vector6:
    if not isinstance(value, Sequence) or len(value) != 6:
        raise ValueError(f"{path}: expected twist[6]")
    return tuple(float(v) for v in value)


def _resolve_paper_truth_path(path_raw: object) -> Path:
    path = Path(str(path_raw))
    if not path.is_absolute():
        path = (Path(__file__).resolve().parents[4] / path).resolve()
    return path


@dataclass(frozen=True)
class ContactProgramResult:
    phase: RemotePhase
    qdot: Vector6
    desired_twist: Vector6
    faulted: bool


@dataclass
class DirectControlKernel:
    cfg: Mapping[str, Any]
    solver: Any
    policy: Any
    envelope: Any
    rate_hz: float
    host_slew_rad_s2: float
    last_qdot: Vector6 = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    warmed: bool = False
    backend: str | None = None

    @classmethod
    def create(
        cls,
        cfg: Mapping[str, Any],
        *,
        solver_factory=StrictTaseRnnSolver,
        policy_factory=StrictRnnControlPolicy,
        envelope_factory=SafetyEnvelope,
        backend_override: str | None = None,
    ) -> "DirectControlKernel":
        solver_cfg = StrictRnnConfig(
            paper_truth_path=_resolve_paper_truth_path(cfg["solver"]["paper_truth_path"]),
            qdot_limit_rad_s=_as_float(cfg["limits"]["qdot_limit_rad_s"], "limits.qdot_limit_rad_s"),
            epsilon=_as_float(cfg["rates"]["rnn_epsilon"], "rates.rnn_epsilon"),
            sigr_exponent_r=_as_float(cfg["rates"]["rnn_sigr_exponent_r"], "rates.rnn_sigr_exponent_r"),
            inner_iterations=int(cfg["rates"]["rnn_inner_iterations"]),
            backend=str(backend_override or cfg["rates"]["rnn_backend"]),
        )
        solver = solver_factory(solver_cfg)
        backend = str(backend_override or cfg["rates"]["rnn_backend"])
        return cls(
            cfg=cfg,
            solver=solver,
            policy=policy_factory(solver),
            envelope=envelope_factory(
                qdot_cap_rad_s=_as_float(cfg["limits"]["qdot_limit_rad_s"], "limits.qdot_limit_rad_s"),
                max_normal_tracking_error_m_s=_as_float(
                    cfg["path"]["normal_velocity_limit_m_s"], "path.normal_velocity_limit_m_s"
                ),
                max_residual_norm=_as_float(cfg["path"]["total_linear_limit_m_s"], "path.total_linear_limit_m_s"),
                normal_contract_tolerance=1e-6,
                predicted_twist_tolerance=_as_float(cfg["path"]["path_gain"], "path.path_gain") * 1e-3,
            ),
            rate_hz=_as_float(cfg["command_rate_hz"], "command_rate_hz"),
            host_slew_rad_s2=_as_float(cfg["limits"]["host_slew_rad_s2"], "limits.host_slew_rad_s2"),
            backend=backend,
        )

    def reset(self) -> None:
        self.solver.reset_state()
        self.warmed = False
        self.last_qdot = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def warm_start(self, sample: KinematicSample, *, desired_twist: Vector6) -> None:
        omega_minus, omega_plus = joint_omega_bounds(
            sample.q,
            sample.q_min,
            sample.q_max,
            alpha_s_inv=_as_float(self.cfg["kinematics"]["position_bound_gain_s_inv"], "kinematics.position_bound_gain_s_inv"),
            qdot_limit_rad_s=_as_float(self.cfg["limits"]["qdot_limit_rad_s"], "limits.qdot_limit_rad_s"),
        )
        self.solver.warm_start(
            J=np.asarray(sample.jacobian, dtype=float),
            xdot_c=tuple(float(x) for x in desired_twist),
            omega_minus=omega_minus,
            omega_plus=omega_plus,
            damping=1e-4,
        )
        self.warmed = True

    def compute(
        self,
        sample: KinematicSample,
        *,
        desired_twist: Vector6,
        reaction_normal: Vector3,
        approach_normal: Vector3,
        dt_s: float,
        normal_motion_policy: str = "frame_contract_only",
        path_time_s: float = 0.0,
        raw_desired_twist: Vector6 | None = None,
    ) -> Vector6:
        if not self.warmed:
            raise RuntimeError("kernel must be warm_started before compute")
        dt = _as_float(dt_s, "dt_s")
        if dt <= 0.0:
            raise ValueError("dt_s must be > 0")
        omega_minus, omega_plus = joint_omega_bounds(
            sample.q,
            sample.q_min,
            sample.q_max,
            alpha_s_inv=_as_float(self.cfg["kinematics"]["position_bound_gain_s_inv"], "kinematics.position_bound_gain_s_inv"),
            qdot_limit_rad_s=_as_float(self.cfg["limits"]["qdot_limit_rad_s"], "limits.qdot_limit_rad_s"),
        )
        observation = Step5dObservation(
            sequence=0,
            timestamp_s=_as_float(sample.timestamp_s, "timestamp_s"),
            q=sample.q,
            qd=sample.qd,
            tcp_pose=sample.tcp_pose,
            tcp_twist=sample.tcp_twist,
            wrench=sample.wrench_tcp,
            jacobian=sample.jacobian,
            desired_twist=desired_twist,
            reaction_normal=reaction_normal,
            approach_normal=approach_normal,
            command_frame="base",
            normal_frame="base",
            path_time_s=_as_float(path_time_s, "path_time_s"),
            dt_s=dt,
            raw_desired_twist=raw_desired_twist,
            omega_minus=omega_minus,
            omega_plus=omega_plus,
            normal_motion_policy=normal_motion_policy,
        )
        candidate = self.policy.compute(observation)
        governed = apply_direction_preserving_slew(
            observation,
            candidate,
            previous_qdot=self.last_qdot,
            dt_s=dt,
            max_slew_rad_s2=self.host_slew_rad_s2,
            dt_max_s=1.0 / max(self.rate_hz, 1e-9),
        )
        decision = self.envelope.evaluate(observation, governed, _workspace=None)
        if not decision.accepted:
            raise RuntimeError(f"decision rejected: {decision.action}")
        qdot = _extract_qdot(decision)
        self.last_qdot = qdot
        return qdot


def _cycloid_reference_velocity(cfg: Mapping[str, Any], elapsed_s: float) -> Vector6:
    basis = cfg["path"]["basis"]
    amp = _as_float(cfg["path"]["amplitude_m"], "path.amplitude_m")
    omega = _as_float(cfg["path"]["omega_rad_s"], "path.omega_rad_s")
    t = _as_float(elapsed_s, "elapsed_s")
    if t < 0.0:
        raise ValueError("elapsed_s must be non-negative")
    phi = omega * t
    ux, uy = _as_float_list(basis["u_along_xy"], 2, "path.basis.u_along_xy")
    px, py = _as_float_list(basis["p_lateral_xy"], 2, "path.basis.p_lateral_xy")
    vxy = amp * omega * (1.0 - math.cos(phi))
    vlat = amp * math.sin(phi)
    return (
        float(ux * vxy + px * vlat),
        float(uy * vxy + py * vlat),
        0.0,
        0.0,
        0.0,
        0.0,
    )


@dataclass
class ContactProgram:
    cfg: Mapping[str, Any]
    kernel: DirectControlKernel
    state_machine: RemoteControlStateMachine
    normal_filter: ReactionNormalFilter
    preload_gate: PreloadGate
    outer_cfg: Step5dOuterLoopConfig
    outer_state: Step5dOuterLoopState
    warmed_phase: RemotePhase | None = None
    retract_origin: Vector3 | None = None
    retract_progress: float = 0.0

    @classmethod
    def create(cls, cfg: Mapping[str, Any], *, kernel: DirectControlKernel | None = None) -> "ContactProgram":
        kernel = kernel if kernel is not None else DirectControlKernel.create(cfg)
        terms = compute_outer_force_terms(cfg["force"])
        outer_cfg = Step5dOuterLoopConfig(
            kp=_as_float(cfg["path"]["path_gain"], "path.path_gain"),
            ko=_as_float(cfg["force"]["orientation_ko"], "force.orientation_ko"),
            orientation_gain_scale=1.0,
            kf=_as_float(terms["kf"], "kf"),
            Md_scalar=_as_float(terms["Md"], "Md"),
            Bd_scalar=_as_float(terms["Bd"], "Bd"),
            force_target_n=_as_float(cfg["force"]["target_force_n"], "force.target_force_n"),
            force_integral_limit_n_s=_as_float(cfg["path"]["integral_limit_n_s"], "path.integral_limit_n_s"),
            min_force_norm_n=1e-9,
            control_reaction_normal_fallback_base=normalize_vector(cfg["frame"]["reaction_normal_b"]),
            force_sign_convention="step5_step6_positive_normal_load",
        )
        return cls(
            cfg=cfg,
            kernel=kernel,
            state_machine=RemoteControlStateMachine(cfg),
            normal_filter=ReactionNormalFilter(cfg),
            preload_gate=PreloadGate(cfg),
            outer_cfg=outer_cfg,
            outer_state=Step5dOuterLoopState(),
        )

    def _track_twist(self, sample: KinematicSample, dt_s: float, filtered_normal: Vector3) -> Vector6:
        dt = _as_float(dt_s, "dt_s")
        if dt <= 0.0:
            raise ValueError("dt_s must be > 0")
        prior_xyz = _as_float_list(self.cfg["preflight"]["prior_xyz"], 3, "preflight.prior_xyz")
        force_tcp = _as_float_list(sample.wrench_tcp, 6, "wrench_tcp")[:3]
        track_elapsed_s = float(self.state_machine.state.track_elapsed_s)
        x, y = cycloid_xy_reference(
            track_elapsed_s,
            basis=self.cfg["path"]["basis"],
            amplitude_m=_as_float(self.cfg["path"]["amplitude_m"], "path.amplitude_m"),
            omega_rad_s=_as_float(self.cfg["path"]["omega_rad_s"], "path.omega_rad_s"),
        )
        vxd, vyd, _, _, _, _ = _cycloid_reference_velocity(self.cfg, track_elapsed_s)
        inputs = Step5dOuterLoopInputs(
            tcp_pose_base=sample.tcp_pose,
            tcp_speed_base=sample.tcp_twist,
            force_tcp_n=force_tcp,
            x_pd_base=(x, y, prior_xyz[2]),
            xdot_pd_base=(vxd, vyd, 0.0),
            dt_s=dt,
            cmd_valid=True,
            control_reaction_normal_base=filtered_normal,
        )
        output = compute_step5d_outer_loop(
            self.outer_cfg,
            self.outer_state,
            inputs,
            include_diagnostics=False,
        )
        self.outer_state = output.next_state
        capped_linear, capped_angular = apply_linear_caps(
            linear_twist=output.xdot_c[:3],
            angular_twist=output.xdot_c[3:],
            normal_axis_b=filtered_normal,
            tangent_limit_m_s=_as_float(self.cfg["path"]["motion_limit_m_s"], "path.motion_limit_m_s"),
            normal_limit_m_s=_as_float(self.cfg["path"]["normal_velocity_limit_m_s"], "path.normal_velocity_limit_m_s"),
            total_linear_limit_m_s=_as_float(self.cfg["path"]["total_linear_limit_m_s"], "path.total_linear_limit_m_s"),
            angular_limit_rad_s=_as_float(self.cfg["guard"]["angular_limit_rad_s"], "guard.angular_limit_rad_s"),
        )
        return (*capped_linear, *capped_angular)

    def _update_retract_progress(self, sample: KinematicSample, reaction: Vector3) -> float:
        if self.retract_origin is None:
            self.retract_origin = sample.tcp_pose[:3]
            return 0.0
        delta = np.asarray(sample.tcp_pose[:3], dtype=float) - np.asarray(self.retract_origin)
        projection = float(np.dot(delta, np.asarray(reaction)))
        distance = _as_float(self.cfg["retract"]["distance_m"], "retract.distance_m")
        if distance <= 0.0:
            return 1.0
        return min(1.0, max(0.0, projection / distance))

    def _desired_for_phase(self, sample: KinematicSample, filtered_normal: Vector3, dt_s: float) -> tuple[Vector6, str]:
        state = self.state_machine.state
        if state.phase == RemotePhase.SEARCH:
            search_twist, _ = desired_search_vectors(self.cfg, float(state.phase_elapsed_s))
            return search_twist, "frame_contract_only"
        if state.phase == RemotePhase.TRACK:
            twist = self._track_twist(sample, dt_s, filtered_normal)
            return twist, "frame_contract_only"
        if state.phase == RemotePhase.RETRACT:
            retract_twist, _ = desired_retract_vectors(self.cfg, float(self.retract_progress))
            return retract_twist, "frame_contract_only"
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0), "frame_contract_only"

    def step(self, sample: KinematicSample, dt_s: float) -> ContactProgramResult:
        dt = _as_float(dt_s, "dt_s")
        if dt <= 0.0:
            raise ValueError("dt_s must be > 0")
        reaction = _as_float_list(self.cfg["frame"]["reaction_normal_b"], 3, "frame.reaction_normal_b")
        approach = _as_float_list(self.cfg["frame"]["approach_normal_b"], 3, "frame.approach_normal_b")
        R_base = rotvec_to_matrix(sample.tcp_pose[3:6])
        force_base = tuple(float(v) for v in R_base @ np.asarray(sample.wrench_tcp[:3], dtype=float))
        raw_load = float(np.dot(force_base, np.asarray(reaction)))
        filtered_normal = self.normal_filter.update(force_base, load_force_n=raw_load, dt=dt)
        filtered_normal = tuple(float(v) for v in normalize_vector(filtered_normal))
        preload_ready = self.preload_gate.step(
            dt=dt,
            raw_force_n=raw_load,
            filtered_force_n=raw_load,
            force_norm_n=float(np.linalg.norm(force_base)),
        )
        guard = primitives.guard_wrench(self.cfg, sample.wrench_tcp)
        state = self.state_machine.step(
            dt,
            preload_ready=preload_ready,
            preload_fault=self.preload_gate.fault,
            retract_progress=self.retract_progress,
            fault=(not guard.ok),
        )

        phase = state.phase
        if phase == RemotePhase.STOP:
            return ContactProgramResult(
                phase=phase,
                desired_twist=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                qdot=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                faulted=False,
            )
        if phase == RemotePhase.RETRACT:
            self.retract_progress = self._update_retract_progress(sample, tuple(reaction))
        else:
            self.retract_progress = 0.0
            self.retract_origin = None

        desired, policy = self._desired_for_phase(sample, filtered_normal, dt)
        if self.retract_progress >= 1.0 and phase == RemotePhase.RETRACT:
            desired = _as_twist6(desired_retract_vectors(self.cfg, 1.0)[0], "desired_retract_full")
        if state.faulted or phase == RemotePhase.ABORT or not guard.ok:
            return ContactProgramResult(
                phase=phase,
                desired_twist=desired,
                qdot=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                faulted=True,
            )

        if _is_nonzero(desired):
            if self.warmed_phase != phase:
                self.kernel.reset()
                self.kernel.warm_start(sample, desired_twist=desired)
                self.warmed_phase = phase
            qdot = self.kernel.compute(
                sample,
                desired_twist=desired,
                reaction_normal=tuple(filtered_normal),
                approach_normal=tuple(approach),
                dt_s=dt,
                normal_motion_policy=policy,
                path_time_s=float(self.state_machine.state.track_elapsed_s) if phase == RemotePhase.TRACK else 0.0,
            )
        else:
            qdot = desired
            self.kernel.reset()
        return ContactProgramResult(phase=phase, qdot=qdot, desired_twist=desired, faulted=False)
