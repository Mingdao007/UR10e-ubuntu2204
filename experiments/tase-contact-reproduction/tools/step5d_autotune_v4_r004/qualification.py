"""Thin r004 adapter around the canonical V4 baseline control stack.

This module owns no transport and performs no robot action.  It maps an r004
candidate and observation onto the already-reviewed V4 control, calibrated
runtime, and Jacobian gate, then returns the bounded command to be published by
the single live writer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .contracts import Candidate, R004Contract
from .motion_profile import R004_MOTION_PROFILE, V4MotionProfile
from .transport import R004OutputSnapshot
from .wire import CommandMode, SensorPacket


class QualificationControlError(RuntimeError):
    """The canonical qualification stack failed closed."""


BASELINE_TANGENTIAL_NUMERIC_TOLERANCE_M_S = 2e-6
BASELINE_ANGULAR_NUMERIC_TOLERANCE_RAD_S = 2e-6


@dataclass(frozen=True)
class QualificationCommand:
    command_mode: CommandMode
    qdot: tuple[float, float, float, float, float, float]
    internal_setpoint_n: float
    filtered_normal_n: float
    sticky_one_newton_latched: int
    canonical_phase: str
    canonical_reason: str


@dataclass
class CanonicalQualificationControl:
    """Compose canonical V4 primitives for one qualification attempt."""

    candidate: Candidate
    attempt_id: str
    release_contract: R004Contract
    path_requested: bool = False
    motion_profile: V4MotionProfile | None = R004_MOTION_PROFILE
    canonical_runtime_only: bool = False
    force_integral_limit_n_s: float = 1.0
    _setpoint_n: float = 1.0
    _sticky_latched: int = 0
    _last_monotonic_s: float | None = None
    _origin_monotonic_s: float | None = None
    _contract: Any = field(init=False, repr=False)
    _canonical_candidate: Any = field(init=False, repr=False)
    _runtime: Any = field(init=False, repr=False)
    _path_controller: Any = field(init=False, repr=False)
    _baseline_state: Any = field(init=False, repr=False)
    _timing: Any = field(init=False, repr=False)
    _startup: Any = field(init=False, repr=False)
    _startup_ready_latched: bool = field(default=False, init=False, repr=False)
    _path_origin_monotonic_s: float | None = field(default=None, init=False, repr=False)
    _tangential_tolerance_m_s: float = field(init=False, repr=False)
    _angular_tolerance_rad_s: float = field(init=False, repr=False)
    _required_hold_s: float = field(init=False, repr=False)
    _readiness_gate: Any = field(init=False, repr=False)
    _qualification_retract_issued: bool = field(default=False, init=False, repr=False)
    _previous_qdot: tuple[float, float, float, float, float, float] = field(
        default=(0.0,) * 6, init=False, repr=False
    )
    _tube_cbf: Any = field(default=None, init=False, repr=False)
    last_tube_cbf: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _path_entry_rate_limit: Any = field(default=None, init=False, repr=False)
    _path_entry_rate_limit_init_failed: bool = field(
        default=False, init=False, repr=False
    )
    last_path_entry_rate_limit: dict[str, Any] | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise QualificationControlError("qualification attempt identity is missing")
        if not isinstance(self.canonical_runtime_only, bool):
            raise QualificationControlError("canonical runtime-only policy is not typed")
        if self.motion_profile is not None and not isinstance(self.motion_profile, V4MotionProfile):
            raise QualificationControlError("qualification motion profile is not typed")
        policy = self.release_contract.raw["live_boundary"][
            "canonical_calibrated_numeric_residual_policy"
        ]
        self._tangential_tolerance_m_s = float(policy["tangential_m_s_max"])
        self._angular_tolerance_rad_s = float(policy["angular_rad_s_max"])
        self._required_hold_s = (
            float(self.release_contract.raw["live_boundary"]["qualified_path_trial_hold_s"])
            if self.path_requested
            else 10.0
        )
        if (
            self._tangential_tolerance_m_s != BASELINE_TANGENTIAL_NUMERIC_TOLERANCE_M_S
            or self._angular_tolerance_rad_s != BASELINE_ANGULAR_NUMERIC_TOLERANCE_RAD_S
        ):
            raise QualificationControlError("qualification residual policy differs")
        try:
            from step5d_autotune_v4_r008.tube_cbf_live import TubeCbfLiveFilter

            self._tube_cbf = TubeCbfLiveFilter.from_environ()
        except Exception:
            # Soft seam must never block qualification construction; stay off.
            self._tube_cbf = None
        try:
            from step5d_autotune_v4.contracts import V4Candidate, load_contract
            from step5d_autotune_v4_r004.path_controller import V4PathController
            from step5d_autotune_v4_r004.baseline_runtime import (
                BaselineReadinessGate,
                BaselineState,
            )
            from step5d_autotune_v4_r004.calibrated_runtime import V4CalibratedRuntime
            from step5d_autotune_v4_r004.runtime import StartupHeartbeatGate, TimingGuard

            canonical_candidate = V4Candidate(**self.candidate.canonical)
            if canonical_candidate.candidate_uid != self.candidate.uid:
                raise QualificationControlError("r004/canonical candidate identity differs")
            self._contract = load_contract(runtime_only=self.canonical_runtime_only)
            self._canonical_candidate = canonical_candidate
            self._runtime = V4CalibratedRuntime(
                self._contract,
                canonical_candidate,
                motion_profile=self.motion_profile,
                force_integral_limit_n_s=float(self.force_integral_limit_n_s),
            )
            # The canonical PATH producer has a legacy no-profile default for
            # finite r003 callers.  r004 production must bind its typed V3
            # effective profile explicitly at this seam.
            self._path_controller = V4PathController(
                canonical_candidate,
                motion_profile=self.motion_profile,
            )
            self._baseline_state = BaselineState()
            self._readiness_gate = (
                BaselineReadinessGate(
                    filtered_min_n=3.0,
                    filtered_max_n=7.0,
                    raw_min_n=3.0,
                    raw_max_n=8.0,
                    force_norm_max_n=10.0,
                    torque_norm_max_nm=0.30,
                )
                if self.path_requested
                else BaselineReadinessGate()
            )
            self._timing = TimingGuard()
            self._startup = StartupHeartbeatGate()
        except QualificationControlError:
            raise
        except Exception as exc:
            raise QualificationControlError(
                f"canonical V4 qualification stack is unavailable: {exc}"
            ) from exc

    @property
    def sticky_one_newton_latched(self) -> int:
        return self._sticky_latched

    def step(
        self,
        *,
        output: R004OutputSnapshot,
        sensor: SensorPacket,
        monotonic_s: float,
        command_sequence: int,
    ) -> QualificationCommand:
        try:
            from step5d_autotune_v4_r004.baseline_runtime import (
                BaselineHardLimits,
                BaselineObservation,
                BaselinePhase,
                step_baseline,
            )
            from step5d_autotune_v4_r004.policies import V4InvariantEnvelope
            from step5d_autotune_v4_r004.runtime import (
                gate_qdot,
                project_qdot_to_gate,
                rescale_qdot_to_gate,
            )

            now = float(monotonic_s)
            if not math.isfinite(now):
                raise QualificationControlError("qualification monotonic time is nonfinite")
            if self._origin_monotonic_s is None:
                self._origin_monotonic_s = now
            actual_dt_s = (
                0.002
                if self.motion_profile is None
                else self.motion_profile.period_s
            ) if self._last_monotonic_s is None else now - self._last_monotonic_s
            if not 0.0 < actual_dt_s < 0.080:
                raise QualificationControlError("qualification actual dt is outside (0,80ms)")
            self._last_monotonic_s = now

            if sensor.normal_load_n >= 0.8 or sensor.force_norm_n >= 1.0:
                self._sticky_latched = 1
            elapsed = now - self._origin_monotonic_s
            observed_dt = self._timing.observe(elapsed)
            startup_ready = self._startup.observe(sensor.heartbeat, elapsed)
            # StartupHeartbeatGate reports whether two fresh increments are
            # present in its current 250 ms window.  Qualification startup is
            # a one-way transition: once proven, a later window rollover must
            # not reset an in-progress 1-to-5 N ramp back to 1 N.  We continue
            # observing the gate so heartbeat regressions still fault closed.
            self._startup_ready_latched = self._startup_ready_latched or startup_ready
            if self._timing.stopped or self._startup.stopped:
                raise QualificationControlError(
                    self._timing.stop_reason or self._startup.stop_reason
                )
            tick_log = self._path_controller.step(
                actual_dt_s=actual_dt_s,
                raw_normal_n=sensor.normal_load_n,
                setpoint_n=self._setpoint_n,
                mode="baseline",
            )
            if observed_dt is None or not self._startup_ready_latched:
                self._previous_qdot = (0.0,) * 6
                return QualificationCommand(
                    command_mode=CommandMode.BASELINE,
                    qdot=(0.0,) * 6,
                    internal_setpoint_n=1.0,
                    filtered_normal_n=tick_log.filtered_normal_n,
                    sticky_one_newton_latched=self._sticky_latched,
                    canonical_phase="startup",
                    canonical_reason="startup_two_increments_pending",
                )
            self._baseline_state, baseline_command = step_baseline(
                self._canonical_candidate,
                self._baseline_state,
                BaselineObservation(
                    dt_s=actual_dt_s,
                    one_newton_latched=bool(self._sticky_latched),
                    filtered_normal_n=tick_log.filtered_normal_n,
                    raw_normal_n=sensor.normal_load_n,
                    force_norm_n=sensor.force_norm_n,
                    torque_norm_nm=sensor.torque_norm_nm,
                    sensor_fresh=sensor.sensor_fresh,
                    stationary=output.stationary,
                ),
                required_hold_s=self._required_hold_s,
                readiness_gate=self._readiness_gate,
                hard_limits=BaselineHardLimits(
                    max_abs_normal_n=60.0,
                    max_force_norm_n=100.0,
                    max_torque_norm_nm=3.0,
                ),
            )
            if self._baseline_state.phase is BaselinePhase.FAILED or baseline_command.stop:
                raise QualificationControlError(
                    baseline_command.reason or "canonical baseline failed"
                )
            self._setpoint_n = float(baseline_command.internal_setpoint_n)
            if self._baseline_state.phase is BaselinePhase.SUCCESS:
                if not self.path_requested:
                    self._previous_qdot = (0.0,) * 6
                    if self._qualification_retract_issued:
                        return QualificationCommand(
                            command_mode=CommandMode.RETRACT,
                            qdot=(0.0,) * 6,
                            internal_setpoint_n=self._setpoint_n,
                            filtered_normal_n=tick_log.filtered_normal_n,
                            sticky_one_newton_latched=self._sticky_latched,
                            canonical_phase=self._baseline_state.phase.value,
                            canonical_reason="",
                        )
                    if not baseline_command.retract_allowed:
                        # SUCCESS can be reached on the same feedback sample
                        # that still reports the preceding force-control
                        # velocity.  Command a zero-qdot baseline hold until a
                        # fresh stationary sample opens the retract gate.
                        return QualificationCommand(
                            command_mode=CommandMode.BASELINE,
                            qdot=(0.0,) * 6,
                            internal_setpoint_n=self._setpoint_n,
                            filtered_normal_n=tick_log.filtered_normal_n,
                            sticky_one_newton_latched=self._sticky_latched,
                            canonical_phase="success_wait_stationary",
                            canonical_reason="stationary_retract_gate_pending",
                        )
                    self._qualification_retract_issued = True
                    return QualificationCommand(
                        command_mode=CommandMode.RETRACT,
                        qdot=(0.0,) * 6,
                        internal_setpoint_n=self._setpoint_n,
                        filtered_normal_n=tick_log.filtered_normal_n,
                        sticky_one_newton_latched=self._sticky_latched,
                        canonical_phase=self._baseline_state.phase.value,
                        canonical_reason="",
                    )
                if self._path_origin_monotonic_s is None:
                    self._path_origin_monotonic_s = now
                path_time_s = now - self._path_origin_monotonic_s
                tangential_error, orientation_error = self._runtime.path_errors(
                    actual_tcp_pose=output.tcp_pose_m_rad,
                    path_time_s=path_time_s,
                    motion_kp=self.candidate.motion_kp,
                )
                tick_log = self._path_controller.step(
                    actual_dt_s=actual_dt_s,
                    raw_normal_n=sensor.normal_load_n,
                    setpoint_n=self._setpoint_n,
                    mode="path",
                    orientation_error_rad=orientation_error,
                    tangential_error_m=tangential_error,
                )
                mode = "path"
            else:
                path_time_s = 0.0
                mode = "baseline"
            desired_twist = self._runtime.desired_twist(
                actual_tcp_pose=output.tcp_pose_m_rad,
                actual_tcp_speed=output.tcp_speed_m_s_rad_s,
                force_tcp_n=sensor.wrench[:3],
                filtered_normal_n=tick_log.filtered_normal_n,
                internal_setpoint_n=baseline_command.internal_setpoint_n,
                actual_dt_s=actual_dt_s,
                mode=mode,
                path_time_s=path_time_s,
            )
            # PATH entry rate-limit (HOOK_POINT): post outer-loop normal xdot,
            # pre command / ActiveMotionEnvelopeV3. Default OFF unless
            # R008_PATH_ENTRY_RATE_LIMIT=1.
            if (
                self._path_entry_rate_limit is None
                and not self._path_entry_rate_limit_init_failed
            ):
                try:
                    from step5d_autotune_v4_r008.path_entry_rate_limit import (
                        PathEntryRateLimitConfig,
                        PathEntryRateLimitRamp,
                    )

                    self._path_entry_rate_limit = PathEntryRateLimitRamp(
                        config=PathEntryRateLimitConfig.from_environ()
                    )
                except Exception:
                    self._path_entry_rate_limit_init_failed = True
            self.last_path_entry_rate_limit = None
            if self._path_entry_rate_limit is not None:
                # Reaction normal is +Z; into-surface commanded normal is −vz.
                commanded_normal = -float(desired_twist[2])
                ramp_out = self._path_entry_rate_limit.apply(
                    commanded_normal_m_s=commanded_normal,
                    mode=mode,
                    dt_s=float(actual_dt_s),
                    monotonic_s=now,
                )
                self.last_path_entry_rate_limit = {
                    "active": bool(ramp_out.active),
                    "clipped": bool(ramp_out.clipped),
                    "reason": str(ramp_out.reason),
                    "commanded_normal_m_s": float(ramp_out.commanded_normal_m_s),
                    "limited_normal_m_s": float(ramp_out.limited_normal_m_s),
                    "amp_ceiling_m_s": float(ramp_out.amp_ceiling_m_s),
                    "elapsed_s": ramp_out.elapsed_s,
                }
                if ramp_out.limited_normal_m_s != commanded_normal:
                    desired_twist = (
                        float(desired_twist[0]),
                        float(desired_twist[1]),
                        -float(ramp_out.limited_normal_m_s),
                        float(desired_twist[3]),
                        float(desired_twist[4]),
                        float(desired_twist[5]),
                    )
            # Soft Tube+CBF: after desired_twist, before command(). Default OFF.
            if self._tube_cbf is None and not getattr(self, "_tube_cbf_init_failed", False):
                try:
                    from step5d_autotune_v4_r008.tube_cbf_live import TubeCbfLiveFilter

                    self._tube_cbf = TubeCbfLiveFilter.from_environ()
                except Exception:
                    self._tube_cbf_init_failed = True
            self.last_tube_cbf = None
            if self._tube_cbf is not None:
                cbf_out = self._tube_cbf.apply(
                    desired_twist,
                    mode=mode,
                    actual_tcp_pose=output.tcp_pose_m_rad,
                    path_time_s=path_time_s,
                )
                self.last_tube_cbf = cbf_out.as_dict()
                desired_twist = cbf_out.desired_twist
            calibrated = self._runtime.command(
                actual_q=output.q_rad,
                actual_qd=output.qd_rad_s,
                actual_tcp_pose=output.tcp_pose_m_rad,
                desired_twist=desired_twist,
                actual_dt_s=actual_dt_s,
                mode=mode,
                path_time_s=path_time_s,
            )
            if self.motion_profile is not None:
                pre_gate, _slew_scale = rescale_qdot_to_gate(
                    self._contract,
                    qdot=calibrated.qdot,
                    previous_qdot=self._previous_qdot,
                    jacobian_6x6=calibrated.jacobian_6x6,
                    normal_base=(0.0, 0.0, 1.0),
                    observed_model_hashes=calibrated.observed_model_hashes,
                    actual_dt_s=actual_dt_s,
                    motion_profile=self.motion_profile,
                )
            elif mode == "path":
                pre_gate, _projection_scale = project_qdot_to_gate(
                    self._contract,
                    qdot=calibrated.qdot,
                    jacobian_6x6=calibrated.jacobian_6x6,
                    normal_base=(0.0, 0.0, 1.0),
                    observed_model_hashes=calibrated.observed_model_hashes,
                )
            else:
                pre_gate = gate_qdot(
                    self._contract,
                    qdot=calibrated.qdot,
                    jacobian_6x6=calibrated.jacobian_6x6,
                    normal_base=(0.0, 0.0, 1.0),
                    observed_model_hashes=calibrated.observed_model_hashes,
                )
            pre_gate = V4InvariantEnvelope(
                motion_profile=self.motion_profile,
            ).enforce_gate(pre_gate)
            if not pre_gate.allowed:
                raise QualificationControlError(pre_gate.reason or "canonical qdot gate blocked")
            if mode == "baseline" and (
                pre_gate.tangential_m_s > self._tangential_tolerance_m_s
                or pre_gate.angular_rad_s > self._angular_tolerance_rad_s
            ):
                # The strict-RNN warm start can have a tiny Cartesian residual.
                # Never publish that residual during the one-dimensional
                # baseline; keep ticking the canonical solver behind a
                # zero-qdot BASELINE packet until it converges.
                self._previous_qdot = (0.0,) * 6
                return QualificationCommand(
                    command_mode=CommandMode.BASELINE,
                    qdot=(0.0,) * 6,
                    internal_setpoint_n=self._setpoint_n,
                    filtered_normal_n=tick_log.filtered_normal_n,
                    sticky_one_newton_latched=self._sticky_latched,
                    canonical_phase=self._baseline_state.phase.value,
                    canonical_reason="calibrated_numeric_residual_hold",
                )
            self._previous_qdot = tuple(float(value) for value in pre_gate.qdot)
            return QualificationCommand(
                command_mode=CommandMode.PATH if mode == "path" else CommandMode.BASELINE,
                qdot=tuple(float(value) for value in pre_gate.qdot),
                internal_setpoint_n=self._setpoint_n,
                filtered_normal_n=tick_log.filtered_normal_n,
                sticky_one_newton_latched=self._sticky_latched,
                canonical_phase=self._baseline_state.phase.value,
                canonical_reason="",
            )
        except QualificationControlError:
            raise
        except Exception as exc:
            raise QualificationControlError(
                f"canonical V4 qualification tick failed: {exc}"
            ) from exc


__all__ = [
    "CanonicalQualificationControl",
    "QualificationCommand",
    "QualificationControlError",
]
