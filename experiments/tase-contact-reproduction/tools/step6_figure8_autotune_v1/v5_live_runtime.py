"""Single-writer V5 live control primitives and offline whole-flow seam.

This module owns no transport or filesystem.  It composes the already-reviewed
V4 actual-dt filter, calibrated outer loop, Figure-eight guard stack, and qdot
envelope behind a small backend protocol so the lifecycle can be falsified
without a robot.  A prepared candidate never observes the sensor before the
controller acknowledges the atomic rollover.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable, Mapping, Protocol, Sequence

try:
    from step5d_paper_outer_loop import CONDITIONAL_DOUBLE_CLAMP_POLICY
    from step5d_autotune_v4_r004.runtime import StartupHeartbeatGate, TimingGuard
    from step5d_autotune_v4_r004.wire import CommandMode, SensorPacket
    from step5d_autotune_v4_r013.path_context import PathContextSampleV1
    from step6_figure8_autotune_v1.core import (
        CorrectionPolicyV1,
        CorrectionReceiptV1,
        CorrectionStateV1,
    )
    from step6_figure8_autotune_v1.live_composition import (
        FigureEightPathGuardStackV1,
        figure8_motion_profile,
        figure8_path_provider,
    )
    from step6_figure8_autotune_v1.v5_composition_contract import V5TPState
    from step6_figure8_autotune_v1.v5_rollover import (
        FORMAL_METRIC_START_S,
        PATH_END_S,
        TAIL_END_S,
        CandidateIdentityV1,
        V5RolloverStateV2,
        entry_target_n,
        figure8_analytic_sample,
    )
except ModuleNotFoundError:  # pragma: no cover - repository-root imports
    from tools.step5d_paper_outer_loop import CONDITIONAL_DOUBLE_CLAMP_POLICY
    from tools.step5d_autotune_v4_r004.runtime import StartupHeartbeatGate, TimingGuard
    from tools.step5d_autotune_v4_r004.wire import CommandMode, SensorPacket
    from tools.step5d_autotune_v4_r013.path_context import PathContextSampleV1
    from tools.step6_figure8_autotune_v1.core import (
        CorrectionPolicyV1,
        CorrectionReceiptV1,
        CorrectionStateV1,
    )
    from tools.step6_figure8_autotune_v1.live_composition import (
        FigureEightPathGuardStackV1,
        figure8_motion_profile,
        figure8_path_provider,
    )
    from tools.step6_figure8_autotune_v1.v5_composition_contract import V5TPState
    from tools.step6_figure8_autotune_v1.v5_rollover import (
        FORMAL_METRIC_START_S,
        PATH_END_S,
        TAIL_END_S,
        CandidateIdentityV1,
        V5RolloverStateV2,
        entry_target_n,
        figure8_analytic_sample,
    )


V5_LIVE_RUNTIME_SCHEMA = "step6.autotune/figure8-v5-live-runtime-v1"
V5_LIVE_RUNTIME_VERSION = 1
_ZERO_QDOT = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
ACTIVATION_PENDING_MAX_S = 0.020
_PATH_STATES = frozenset(
    {
        V5TPState.PATH,
        V5TPState.CLOSURE_TAIL,
        V5TPState.ROLLOVER_PREPARED,
        V5TPState.ROLLOVER_COMMITTED,
    }
)


class V5LiveRuntimeError(RuntimeError):
    """The V5 control composition failed closed."""


def _sha256(value: str, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise V5LiveRuntimeError(f"{role} must be a lowercase SHA-256")
    return value


def _qdot(value: Sequence[float], role: str) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or len(value) != 6:
        raise V5LiveRuntimeError(f"{role} must contain six values")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise V5LiveRuntimeError(f"{role} must be finite")
    return result


def v5_path_context(
    path_time_s: float,
    *,
    anchor_pose: Sequence[float],
) -> PathContextSampleV1:
    """Return one typed Figure-eight context through the periodic closure tail."""

    time_s = float(path_time_s)
    if not math.isfinite(time_s) or not 0.0 <= time_s <= TAIL_END_S:
        raise V5LiveRuntimeError("V5 path time is outside [0,20pi]")
    provider = figure8_path_provider(anchor_pose=anchor_pose)
    if time_s <= PATH_END_S:
        return provider.sample(time_s)

    analytic = figure8_analytic_sample(time_s)
    basis = provider.basis_receipt
    along = basis.along_base
    lateral = basis.lateral_base
    displacement = tuple(
        analytic.along_m * along[index] + analytic.lateral_m * lateral[index]
        for index in range(3)
    )
    velocity = tuple(
        analytic.along_velocity_m_s * along[index]
        + analytic.lateral_velocity_m_s * lateral[index]
        for index in range(3)
    )
    acceleration = tuple(
        analytic.along_acceleration_m_s2 * along[index]
        + analytic.lateral_acceleration_m_s2 * lateral[index]
        for index in range(3)
    )
    speed = math.hypot(
        analytic.along_velocity_m_s,
        analytic.lateral_velocity_m_s,
    )
    if speed <= 1e-12:
        tangential_acceleration = 0.0
        curvature = 0.0
        degenerate = True
    else:
        tangential_acceleration = (
            analytic.along_velocity_m_s * analytic.along_acceleration_m_s2
            + analytic.lateral_velocity_m_s * analytic.lateral_acceleration_m_s2
        ) / speed
        curvature = (
            analytic.along_velocity_m_s * analytic.lateral_acceleration_m_s2
            - analytic.lateral_velocity_m_s * analytic.along_acceleration_m_s2
        ) / speed**3
        degenerate = False
    anchor = tuple(float(value) for value in anchor_pose)
    if len(anchor) != 6 or not all(math.isfinite(value) for value in anchor):
        raise V5LiveRuntimeError("V5 path anchor must contain six finite values")
    return PathContextSampleV1(
        path_id=provider.path_id,
        path_time_s=time_s,
        phase_rad=analytic.phase_rad,
        normalized_progress=1.0,
        desired_pose_base=(
            anchor[0] + displacement[0],
            anchor[1] + displacement[1],
            anchor[2] + displacement[2],
            anchor[3],
            anchor[4],
            anchor[5],
        ),
        desired_twist_base=(*velocity, 0.0, 0.0, 0.0),
        desired_acceleration_base=(*acceleration, 0.0, 0.0, 0.0),
        scalar_speed_m_s=speed,
        signed_tangential_acceleration_m_s2=tangential_acceleration,
        signed_planar_curvature_m_inv=curvature,
        degenerate_speed=degenerate,
        identity_receipt=provider.identity_receipt,
    )


def make_v5_runtime_path_reference(
    anchor_pose: Sequence[float],
) -> Callable[[str, tuple[float, float], float], Mapping[str, Any]]:
    """Build the exact mature-runtime mapping, including the analytic tail."""

    anchor = tuple(float(value) for value in anchor_pose)
    if len(anchor) != 6 or not all(math.isfinite(value) for value in anchor):
        raise V5LiveRuntimeError("V5 path-reference anchor is invalid")

    def reference(
        stage_id: str,
        pose_xy: tuple[float, float],
        elapsed_s: float,
    ) -> Mapping[str, Any]:
        if stage_id != "step5d_strict_rnn_autotune_v1":
            raise V5LiveRuntimeError("V5 runtime path stage differs")
        pose = tuple(float(value) for value in pose_xy)
        if len(pose) != 2 or not all(math.isfinite(value) for value in pose):
            raise V5LiveRuntimeError("V5 runtime pose_xy is invalid")
        context = v5_path_context(float(elapsed_s), anchor_pose=anchor)
        desired = context.desired_pose_base[:2]
        velocity = context.desired_twist_base[:2]
        return {
            "stage_id": stage_id,
            "path_id": context.path_id,
            "progress": context.normalized_progress,
            "path_time_s": context.path_time_s,
            "phase_rad": context.phase_rad,
            "desired_xy": desired,
            "desired_velocity_xy": velocity,
            "path_error_xy": (desired[0] - pose[0], desired[1] - pose[1]),
            "local": {
                "path_time_s": context.path_time_s,
                "phase_rad": context.phase_rad,
                "normalized_progress": context.normalized_progress,
                "speed_m_s": context.scalar_speed_m_s,
                "signed_acceleration_m_s2": context.signed_tangential_acceleration_m_s2,
                "signed_curvature_m_inv": context.signed_planar_curvature_m_inv,
            },
            "path_identity": context.identity_receipt.as_dict(),
        }

    return reference


@dataclass(frozen=True)
class V5CandidateRuntimeSpecV1:
    identity: CandidateIdentityV1
    controller_candidate: Any
    correction_policy: CorrectionPolicyV1
    correction_fingerprint_sha256: str
    schema: str = V5_LIVE_RUNTIME_SCHEMA
    version: int = V5_LIVE_RUNTIME_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.identity, CandidateIdentityV1):
            raise TypeError("V5 runtime identity must be typed")
        if not hasattr(self.controller_candidate, "canonical"):
            raise TypeError("V5 runtime controller candidate is not typed")
        if not isinstance(self.correction_policy, CorrectionPolicyV1):
            raise TypeError("V5 runtime correction policy is not typed")
        _sha256(self.correction_fingerprint_sha256, "correction fingerprint")
        if self.schema != V5_LIVE_RUNTIME_SCHEMA or self.version != V5_LIVE_RUNTIME_VERSION:
            raise V5LiveRuntimeError("V5 runtime spec schema/version differs")


@dataclass(frozen=True)
class V5BackendCommandV1:
    filtered_normal_n: float
    qdot: tuple[float, ...]
    gate_receipt: Mapping[str, Any]

    def __post_init__(self) -> None:
        filtered = float(self.filtered_normal_n)
        if not math.isfinite(filtered):
            raise V5LiveRuntimeError("backend filtered normal is nonfinite")
        object.__setattr__(self, "filtered_normal_n", filtered)
        object.__setattr__(self, "qdot", _qdot(self.qdot, "backend qdot"))
        if not isinstance(self.gate_receipt, Mapping):
            raise TypeError("backend gate receipt must be a mapping")


class V5ControlBackend(Protocol):
    @property
    def filtered_normal_n(self) -> float: ...

    def observe_filter(
        self,
        *,
        raw_normal_n: float,
        actual_dt_s: float,
        setpoint_n: float,
        mode: str,
    ) -> float: ...

    def command(
        self,
        *,
        output: Any,
        sensor: SensorPacket,
        effective_target_n: float,
        actual_dt_s: float,
        mode: str,
        path_time_s: float,
        previous_qdot: Sequence[float],
        state_age_s: float,
    ) -> V5BackendCommandV1: ...

    def seed_physical_continuity(
        self,
        *,
        filtered_normal_n: float,
        previous_qdot: Sequence[float],
    ) -> None: ...

    def validate_activation_hold(
        self,
        *,
        output: Any,
        qdot: Sequence[float],
        path_time_s: float,
        actual_dt_s: float,
        state_age_s: float,
    ) -> Mapping[str, Any]: ...


class MatureV5ControlBackend:
    """Production backend over the reviewed V4 controller primitives."""

    def __init__(
        self,
        spec: V5CandidateRuntimeSpecV1,
        *,
        path_reference: Callable[[str, tuple[float, float], float], Mapping[str, Any]],
        guard_stack: FigureEightPathGuardStackV1,
    ) -> None:
        try:
            from step5d_autotune_v4.contracts import load_contract
            from step5d_autotune_v4_r004 import calibrated_runtime as calibrated_module
            from step5d_autotune_v4_r004.calibrated_runtime import V4CalibratedRuntime
            from step5d_autotune_v4_r004.path_controller import V4PathController
            from step5d_autotune_v4_r004.policies import V4InvariantEnvelope
        except ModuleNotFoundError:  # pragma: no cover - repository-root imports
            from tools.step5d_autotune_v4.contracts import load_contract
            from tools.step5d_autotune_v4_r004 import calibrated_runtime as calibrated_module
            from tools.step5d_autotune_v4_r004.calibrated_runtime import V4CalibratedRuntime
            from tools.step5d_autotune_v4_r004.path_controller import V4PathController
            from tools.step5d_autotune_v4_r004.policies import V4InvariantEnvelope

        if calibrated_module.step5_path_reference is not path_reference:
            raise V5LiveRuntimeError(
                "V5 mature path reference is not installed in the scoped runtime"
            )
        self.spec = spec
        self.motion_profile = figure8_motion_profile()
        self.contract = load_contract(runtime_only=True)
        self.path_controller = V4PathController(
            spec.controller_candidate,
            motion_profile=self.motion_profile,
        )
        self.runtime = V4CalibratedRuntime(
            self.contract,
            spec.controller_candidate,
            motion_profile=self.motion_profile,
            force_integral_limit_n_s=1.0,
            internal_setpoint_bounds_n=(1.0, 6.25),
            force_integral_policy=CONDITIONAL_DOUBLE_CLAMP_POLICY,
            force_integral_authority_error_n=0.5,
            force_normal_velocity_limit_m_s=0.003,
        )
        self.guard_stack = guard_stack
        if self.guard_stack.path_reference is not path_reference:
            raise V5LiveRuntimeError("V5 guard stack path reference differs")
        self.invariant = V4InvariantEnvelope(motion_profile=self.motion_profile)
        self._last_filter_receipt: Mapping[str, Any] = {}
        self._last_state_map_receipt: Mapping[str, Any] = {
            "schema": "step6.autotune/figure8-v5-state-map-receipt-v1",
            "mapped": False,
            "reason": "initial_backend",
        }
        self._last_rollover_state_v2: V5RolloverStateV2 | None = None

    @property
    def filtered_normal_n(self) -> float:
        return float(self.path_controller.filtered_normal_n)

    def export_continuity_state(self) -> dict[str, Any]:
        """Export the state needed by a typed successor handoff."""

        candidate = getattr(self.spec.controller_candidate, "canonical", None)
        if not isinstance(candidate, Mapping):
            raise V5LiveRuntimeError("V5 controller candidate has no canonical state")
        result: dict[str, Any] = {
            "schema": "step6.autotune/figure8-v5-continuity-state-v2",
            "candidate": dict(candidate),
            "filtered_normal_n": self.filtered_normal_n,
            "runtime_state": self.runtime.dynamic_state_snapshot(),
        }
        if self._last_rollover_state_v2 is not None:
            result["state_v2"] = self._last_rollover_state_v2.as_dict()
        return result

    def seed_bumpless_state(
        self,
        *,
        filtered_normal_n: float,
        previous_qdot: Sequence[float],
        continuity_state: Mapping[str, Any] | None,
    ) -> None:
        """Map old outer-loop memory and carry the inner solver state."""

        self.seed_physical_continuity(
            filtered_normal_n=filtered_normal_n,
            previous_qdot=previous_qdot,
        )
        receipt: dict[str, Any] = {
            "schema": "step6.autotune/figure8-v5-state-map-receipt-v1",
            "convention": "last_applied_normal_velocity_no_jump",
            "previous_qdot": list(_qdot(previous_qdot, "rollover qdot seed")),
            "mapped": False,
        }
        if continuity_state is None:
            self._last_state_map_receipt = receipt
            return
        if continuity_state.get("schema") != "step6.autotune/figure8-v5-continuity-state-v2":
            raise V5LiveRuntimeError("V5 continuity state schema differs")
        state_v2 = continuity_state.get("state_v2")
        old_candidate = continuity_state.get("candidate")
        old_runtime_state = continuity_state.get("runtime_state")
        if isinstance(state_v2, Mapping):
            old_candidate = state_v2.get("candidate", old_candidate)
        if not isinstance(old_candidate, Mapping) or not isinstance(old_runtime_state, Mapping):
            raise V5LiveRuntimeError("V5 continuity state sections are missing")
        current_candidate = getattr(self.spec.controller_candidate, "canonical", None)
        if not isinstance(current_candidate, Mapping):
            raise V5LiveRuntimeError("V5 successor candidate has no canonical state")
        outer = old_runtime_state.get("outer_state")
        if isinstance(state_v2, Mapping) and "force_integral_n_s" in state_v2:
            outer = {
                **({} if not isinstance(outer, Mapping) else dict(outer)),
                "force_integral_n_s": state_v2["force_integral_n_s"],
                "xdot_p_prev_m_s": state_v2.get(
                    "xdot_p_prev_m_s",
                    (0.0, 0.0, 0.0),
                ),
            }
        if not isinstance(outer, Mapping):
            raise V5LiveRuntimeError("V5 continuity outer state is missing")
        old_integral = float(outer.get("force_integral_n_s"))
        old_prev = tuple(float(value) for value in outer.get("xdot_p_prev_m_s", ()))
        if len(old_prev) != 3 or not all(math.isfinite(value) for value in old_prev):
            raise V5LiveRuntimeError("V5 continuity previous velocity is invalid")
        same_force_law = all(
            math.isclose(float(old_candidate.get(name)), float(current_candidate.get(name)), rel_tol=0.0, abs_tol=1e-15)
            for name in ("force_p_gain", "force_i_gain", "force_damping")
        )
        mapped_integral = old_integral
        map_reason = "same_force_law_carry"
        if not same_force_law:
            p_gain = float(current_candidate.get("force_p_gain"))
            i_gain = float(current_candidate.get("force_i_gain"))
            damping = float(current_candidate.get("force_damping"))
            if not all(math.isfinite(value) for value in (p_gain, i_gain, damping)) or p_gain <= 0.0 or i_gain < 0.0 or damping <= 0.0:
                raise V5LiveRuntimeError("V5 successor force law is invalid")
            old_v = -old_prev[2]
            force_error = 5.0 - float(filtered_normal_n)
            if i_gain == 0.0:
                mapped_integral = 0.0
                map_reason = "zero_successor_i_gain_projection"
            else:
                mapped_integral = -(p_gain * force_error - damping * old_v) / i_gain
                map_reason = "mapped_to_last_applied_normal_velocity"
            authority_limit = min(1.0, 0.5 * p_gain / i_gain) if i_gain > 0.0 else 1.0
            if abs(mapped_integral) > authority_limit + 1e-12:
                raise V5LiveRuntimeError("V5 mapped integral exceeds authority bound")
        mapped_runtime_state = dict(old_runtime_state)
        mapped_outer = dict(outer)
        mapped_outer["force_integral_n_s"] = float(mapped_integral)
        mapped_runtime_state["outer_state"] = mapped_outer
        self.runtime.restore_dynamic_state(mapped_runtime_state)
        self._last_state_map_receipt = {
            **receipt,
            "mapped": True,
            "reason": map_reason,
            "old_integral_n_s": old_integral,
            "mapped_integral_n_s": mapped_integral,
            "old_previous_velocity_m_s": list(old_prev),
            "successor_force_law": {
                name: float(current_candidate.get(name))
                for name in ("force_p_gain", "force_i_gain", "force_damping")
            },
        }

    def observe_filter(
        self,
        *,
        raw_normal_n: float,
        actual_dt_s: float,
        setpoint_n: float,
        mode: str,
    ) -> float:
        log = self.path_controller.step(
            actual_dt_s=actual_dt_s,
            raw_normal_n=raw_normal_n,
            setpoint_n=setpoint_n,
            mode=mode,
        )
        self._last_filter_receipt = {
            "alpha": log.filter_alpha,
            "actual_dt_s": log.actual_dt_s,
            "tau_s": log.normal_filter_tau_s,
            "filtered_normal_n": log.filtered_normal_n,
            "implementation": "V4PathController.actual_dt_one_pole",
        }
        return float(log.filtered_normal_n)

    def command(
        self,
        *,
        output: Any,
        sensor: SensorPacket,
        effective_target_n: float,
        actual_dt_s: float,
        mode: str,
        path_time_s: float,
        previous_qdot: Sequence[float],
        state_age_s: float,
    ) -> V5BackendCommandV1:
        try:
            from step5d_autotune_v4_r004.runtime import rescale_qdot_to_gate
        except ModuleNotFoundError:  # pragma: no cover
            from tools.step5d_autotune_v4_r004.runtime import rescale_qdot_to_gate
        desired = self.runtime.desired_twist(
            actual_tcp_pose=output.tcp_pose_m_rad,
            actual_tcp_speed=output.tcp_speed_m_s_rad_s,
            force_tcp_n=sensor.wrench[:3],
            filtered_normal_n=self.filtered_normal_n,
            internal_setpoint_n=effective_target_n,
            actual_dt_s=actual_dt_s,
            mode=mode,
            path_time_s=path_time_s,
        )
        guarded = self.guard_stack.apply(
            desired,
            mode=mode,
            actual_tcp_pose=output.tcp_pose_m_rad,
            path_time_s=path_time_s,
            state_age_s=state_age_s,
        )
        calibrated = self.runtime.command(
            actual_q=output.q_rad,
            actual_qd=output.qd_rad_s,
            actual_tcp_pose=output.tcp_pose_m_rad,
            desired_twist=guarded.desired_twist,
            actual_dt_s=actual_dt_s,
            mode=mode,
            path_time_s=path_time_s,
        )
        runtime_state = self.runtime.dynamic_state_snapshot()
        outer_state = runtime_state.get("outer_state", {})
        solver_state = runtime_state.get("solver_state", {})
        jacobian = tuple(
            float(value)
            for row in calibrated.jacobian_6x6
            for value in row
        )
        try:
            self._last_rollover_state_v2 = V5RolloverStateV2(
                candidate=dict(self.spec.controller_candidate.canonical),
                filtered_force_n=self.filtered_normal_n,
                filter_initialized=bool(getattr(self.path_controller, "_initialized", True)),
                force_integral_n_s=float(outer_state.get("force_integral_n_s", 0.0)),
                xdot_p_prev_m_s=tuple(float(value) for value in outer_state.get("xdot_p_prev_m_s", (0.0, 0.0, 0.0))),
                theta_dot_state=tuple(float(value) for value in solver_state.get("theta_dot_state", (0.0,) * 6)),
                lambda_state=tuple(float(value) for value in solver_state.get("lambda_state", (0.0,) * 6)),
                last_cartesian_twist=tuple(float(value) for value in guarded.desired_twist),
                previous_qdot=tuple(float(value) for value in previous_qdot),
                pose=tuple(float(value) for value in output.tcp_pose_m_rad),
                q=tuple(float(value) for value in output.q_rad),
                qd=tuple(float(value) for value in output.qd_rad_s),
                jacobian_6x6=jacobian,
                reaction_normal=(0.0, 0.0, 1.0),
                approach_normal=(0.0, 0.0, 1.0),
                target_force_n=5.0,
                actual_dt_s=float(actual_dt_s),
                controller_timestamp=float(getattr(output, "timestamp", path_time_s)),
                generation=int(self.spec.identity.ordinal),
            )
        except (TypeError, ValueError) as exc:
            raise V5LiveRuntimeError("V5 rollover state-v2 capture failed") from exc
        gated, slew_scale = rescale_qdot_to_gate(
            self.contract,
            qdot=calibrated.qdot,
            previous_qdot=previous_qdot,
            jacobian_6x6=calibrated.jacobian_6x6,
            normal_base=(0.0, 0.0, 1.0),
            observed_model_hashes=calibrated.observed_model_hashes,
            actual_dt_s=actual_dt_s,
            motion_profile=self.motion_profile,
        )
        gated = self.invariant.enforce_gate(gated)
        if not gated.allowed:
            raise V5LiveRuntimeError(gated.reason or "V5 qdot gate blocked")
        return V5BackendCommandV1(
            filtered_normal_n=self.filtered_normal_n,
            qdot=gated.qdot,
            gate_receipt={
                "filter": dict(self._last_filter_receipt),
                "guard_stack": guarded.as_dict(),
                "qdot": {
                    "allowed": gated.allowed,
                    "reason": gated.reason,
                    "total_linear_m_s": gated.total_linear_m_s,
                    "normal_m_s": gated.normal_m_s,
                    "tangential_m_s": gated.tangential_m_s,
                    "angular_rad_s": gated.angular_rad_s,
                    "slew_scale": slew_scale,
                },
                "state_map": dict(getattr(self, "_last_state_map_receipt", {})),
            },
        )

    def seed_physical_continuity(
        self,
        *,
        filtered_normal_n: float,
        previous_qdot: Sequence[float],
    ) -> None:
        filtered = float(filtered_normal_n)
        if not math.isfinite(filtered):
            raise V5LiveRuntimeError("rollover filter seed is nonfinite")
        _qdot(previous_qdot, "rollover qdot seed")
        self.path_controller.filtered_normal_n = filtered
        self.path_controller._initialized = True

    def validate_activation_hold(
        self,
        *,
        output: Any,
        qdot: Sequence[float],
        path_time_s: float,
        actual_dt_s: float,
        state_age_s: float,
    ) -> Mapping[str, Any]:
        """Revalidate the immutable seam seed without running either candidate.

        The controller has already crossed the periodic seam, while the host
        is still waiting for the activation journal to become durable.  The
        only command permitted in that bounded interval is the exact qdot that
        passed the switch gate at the seam.  Recompute the current Jacobian,
        hard tube, soft CBF and command envelope around that fixed seed; a CBF
        intervention cannot be applied without changing the seed, so it is a
        fail-closed condition here.
        """

        try:
            from step5d_autotune_v4_r004.calibrated_runtime import (
                tcp_jacobian_base,
            )
            from step5d_autotune_v4_r004.runtime import gate_qdot
        except ModuleNotFoundError:  # pragma: no cover
            from tools.step5d_autotune_v4_r004.calibrated_runtime import (
                tcp_jacobian_base,
            )
            from tools.step5d_autotune_v4_r004.runtime import gate_qdot

        seed = _qdot(qdot, "V5 activation-hold qdot")
        jacobian = tcp_jacobian_base(self.runtime.model, output.q_rad)
        gated = gate_qdot(
            self.contract,
            qdot=seed,
            jacobian_6x6=jacobian,
            normal_base=(0.0, 0.0, 1.0),
            observed_model_hashes=self.runtime.model_hashes,
            motion_profile=self.motion_profile,
        )
        gated = self.invariant.enforce_gate(gated)
        if not gated.allowed or tuple(gated.qdot) != seed:
            raise V5LiveRuntimeError(
                gated.reason or "V5 activation-hold qdot gate blocked"
            )
        guarded = self.guard_stack.apply(
            gated.twist,
            mode="path",
            actual_tcp_pose=output.tcp_pose_m_rad,
            path_time_s=float(path_time_s),
            state_age_s=float(state_age_s),
        )
        if guarded.soft.applied:
            raise V5LiveRuntimeError(
                "V5 activation-hold seed requires a CBF intervention"
            )
        return {
            "filter": dict(self._last_filter_receipt),
            "guard_stack": guarded.as_dict(),
            "qdot": {
                "allowed": True,
                "reason": "",
                "total_linear_m_s": gated.total_linear_m_s,
                "normal_m_s": gated.normal_m_s,
                "tangential_m_s": gated.tangential_m_s,
                "angular_rad_s": gated.angular_rad_s,
                "slew_scale": 1.0,
            },
            "activation_pending": {
                "seed_exact": True,
                "candidate_backend_executed": False,
                "source": "seam_commit_seed",
            },
        }


BackendFactory = Callable[[V5CandidateRuntimeSpecV1], V5ControlBackend]


@dataclass(frozen=True)
class V5ControlCommandV1:
    identity: CandidateIdentityV1
    command_mode: CommandMode
    qdot: tuple[float, ...]
    base_target_n: float
    effective_target_n: float
    filtered_normal_n: float
    sticky_one_newton_latched: int
    path_time_s: float
    actual_dt_s: float
    correction_receipt: CorrectionReceiptV1 | None
    gate_receipt: Mapping[str, Any]
    schema: str = V5_LIVE_RUNTIME_SCHEMA
    version: int = V5_LIVE_RUNTIME_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.identity, CandidateIdentityV1):
            raise TypeError("V5 command identity is not typed")
        if not isinstance(self.command_mode, CommandMode):
            raise TypeError("V5 command mode is not typed")
        object.__setattr__(self, "qdot", _qdot(self.qdot, "V5 command qdot"))
        for role in (
            "base_target_n",
            "effective_target_n",
            "filtered_normal_n",
            "path_time_s",
            "actual_dt_s",
        ):
            value = float(getattr(self, role))
            if not math.isfinite(value):
                raise V5LiveRuntimeError(f"V5 command {role} is nonfinite")
            object.__setattr__(self, role, value)
        if self.sticky_one_newton_latched not in (0, 1):
            raise V5LiveRuntimeError("V5 command latch is not binary")
        if self.correction_receipt is not None and not isinstance(
            self.correction_receipt, CorrectionReceiptV1
        ):
            raise TypeError("V5 correction receipt is not typed")


class V5ChainControlV1:
    """One continuous control chain with one active and one prepared slot."""

    def __init__(
        self,
        active: V5CandidateRuntimeSpecV1,
        *,
        backend_factory: BackendFactory,
        anchor_pose: Sequence[float],
        nominal_period_s: float = 0.002,
    ) -> None:
        if not isinstance(active, V5CandidateRuntimeSpecV1):
            raise TypeError("V5 active runtime spec is not typed")
        period = float(nominal_period_s)
        if not math.isfinite(period) or not 0.0 < period < 0.08:
            raise V5LiveRuntimeError("V5 nominal period is outside (0,80ms)")
        self.backend_factory = backend_factory
        self.anchor_pose = tuple(float(value) for value in anchor_pose)
        if len(self.anchor_pose) != 6 or not all(
            math.isfinite(value) for value in self.anchor_pose
        ):
            raise V5LiveRuntimeError("V5 chain anchor is invalid")
        self.nominal_period_s = period
        self.active_spec = active
        self.active_backend = backend_factory(active)
        self.prepared_spec: V5CandidateRuntimeSpecV1 | None = None
        self.prepared_backend: V5ControlBackend | None = None
        self.correction_state = CorrectionStateV1(
            active.correction_fingerprint_sha256
        )
        self.timing = TimingGuard()
        self.startup = StartupHeartbeatGate()
        self._origin_monotonic_s: float | None = None
        self._last_monotonic_s: float | None = None
        self._startup_ready_latched = False
        self.sticky_one_newton_latched = 0
        self.previous_qdot = _ZERO_QDOT
        self.commit_seed_qdot: tuple[float, ...] | None = None
        self.commit_armed = False

    def prepare(self, spec: V5CandidateRuntimeSpecV1) -> None:
        if self.prepared_spec is not None or self.commit_armed:
            raise V5LiveRuntimeError("V5 prepared slot is already occupied")
        if spec.identity.epoch != self.active_spec.identity.epoch:
            raise V5LiveRuntimeError("V5 prepared candidate epoch differs")
        if spec.identity.ordinal <= self.active_spec.identity.ordinal:
            raise V5LiveRuntimeError("V5 prepared ordinal is not increasing")
        self.prepared_backend = self.backend_factory(spec)
        self.prepared_spec = spec

    def cancel_prepare(self) -> None:
        if self.commit_armed:
            raise V5LiveRuntimeError("V5 armed COMMIT cannot be cancelled locally")
        self.prepared_backend = None
        self.prepared_spec = None

    def arm_commit(self) -> tuple[float, ...]:
        if self.prepared_spec is None or self.prepared_backend is None:
            raise V5LiveRuntimeError("V5 COMMIT requires one prepared candidate")
        if self.commit_armed:
            assert self.commit_seed_qdot is not None
            return self.commit_seed_qdot
        self.commit_seed_qdot = _qdot(self.previous_qdot, "V5 COMMIT seed qdot")
        self.commit_armed = True
        return self.commit_seed_qdot

    def refresh_commit_seed(self) -> tuple[float, ...]:
        """Track the last old-candidate command while a COMMIT is in flight."""
        if not self.commit_armed or self.commit_seed_qdot is None:
            raise V5LiveRuntimeError("V5 COMMIT seed refresh is not armed")
        self.commit_seed_qdot = _qdot(
            self.previous_qdot,
            "V5 refreshed COMMIT seed qdot",
        )
        return self.commit_seed_qdot

    def acknowledge_commit(self) -> V5CandidateRuntimeSpecV1:
        if (
            not self.commit_armed
            or self.commit_seed_qdot is None
            or self.prepared_spec is None
            or self.prepared_backend is None
        ):
            raise V5LiveRuntimeError("V5 COMMIT acknowledgement is not armed")
        filtered = float(self.active_backend.filtered_normal_n)
        continuity_state = None
        exporter = getattr(self.active_backend, "export_continuity_state", None)
        if callable(exporter):
            continuity_state = exporter()
        bumpless = getattr(self.prepared_backend, "seed_bumpless_state", None)
        if callable(bumpless):
            bumpless(
                filtered_normal_n=filtered,
                previous_qdot=self.commit_seed_qdot,
                continuity_state=continuity_state,
            )
        else:
            self.prepared_backend.seed_physical_continuity(
                filtered_normal_n=filtered,
                previous_qdot=self.commit_seed_qdot,
            )
        self.active_spec = self.prepared_spec
        self.active_backend = self.prepared_backend
        self.correction_state = CorrectionStateV1(
            self.active_spec.correction_fingerprint_sha256,
            generation=self.correction_state.generation + 1,
        )
        self.previous_qdot = self.commit_seed_qdot
        self.prepared_spec = None
        self.prepared_backend = None
        self.commit_seed_qdot = None
        self.commit_armed = False
        return self.active_spec

    def step_activation_pending(
        self,
        *,
        output: Any,
        sensor: SensorPacket,
        successor_identity: CandidateIdentityV1,
        commit_seed_qdot: Sequence[float],
        path_time_s: float,
        monotonic_s: float,
        state_age_s: float = 0.0,
    ) -> V5ControlCommandV1:
        """Gate one post-seam, pre-durability duplicate-COMMIT tick.

        This deliberately does not call either candidate's command producer.
        The old candidate remains the host authority and the prepared backend
        remains sensor-silent until ``acknowledge_commit``.  Only the immutable
        seam seed is allowed to keep the controller cycle continuous.
        """

        if (
            not isinstance(sensor, SensorPacket)
            or not isinstance(successor_identity, CandidateIdentityV1)
            or self.prepared_spec is None
            or self.prepared_backend is None
            or successor_identity != self.prepared_spec.identity
            or not self.commit_armed
            or self.commit_seed_qdot is None
        ):
            raise V5LiveRuntimeError(
                "V5 activation-pending state is not armed and typed"
            )
        seed = _qdot(commit_seed_qdot, "V5 activation-pending seed")
        if seed != self.commit_seed_qdot or seed != self.previous_qdot:
            raise V5LiveRuntimeError("V5 activation-pending seed changed")
        time_s = float(path_time_s)
        if not math.isfinite(time_s) or not 0.0 <= time_s < FORMAL_METRIC_START_S:
            raise V5LiveRuntimeError(
                "V5 activation durability exceeded the metric-free entry window"
            )
        if time_s > ACTIVATION_PENDING_MAX_S:
            raise V5LiveRuntimeError(
                "V5 activation durability exceeded the bounded contact seam"
            )
        actual_dt, elapsed = self._actual_dt(monotonic_s)
        startup_ready = self.startup.observe(sensor.heartbeat, elapsed)
        self._startup_ready_latched = self._startup_ready_latched or startup_ready
        if self.timing.stopped or self.startup.stopped:
            raise V5LiveRuntimeError(
                self.timing.stop_reason or self.startup.stop_reason
            )
        base_target = entry_target_n(time_s)
        filtered = self.active_backend.observe_filter(
            raw_normal_n=sensor.normal_load_n,
            actual_dt_s=actual_dt,
            setpoint_n=base_target,
            mode="path",
        )
        gate_receipt = self.active_backend.validate_activation_hold(
            output=output,
            qdot=seed,
            path_time_s=time_s,
            actual_dt_s=actual_dt,
            state_age_s=float(state_age_s),
        )
        return V5ControlCommandV1(
            identity=successor_identity,
            command_mode=CommandMode.PATH,
            qdot=seed,
            base_target_n=base_target,
            effective_target_n=base_target,
            filtered_normal_n=filtered,
            sticky_one_newton_latched=self.sticky_one_newton_latched,
            path_time_s=time_s,
            actual_dt_s=actual_dt,
            correction_receipt=None,
            gate_receipt=gate_receipt,
        )

    def _actual_dt(self, monotonic_s: float) -> tuple[float, float]:
        now = float(monotonic_s)
        if not math.isfinite(now):
            raise V5LiveRuntimeError("V5 monotonic time is nonfinite")
        if self._origin_monotonic_s is None:
            self._origin_monotonic_s = now
        actual_dt = (
            self.nominal_period_s
            if self._last_monotonic_s is None
            else now - self._last_monotonic_s
        )
        if not 0.0 < actual_dt < 0.08:
            raise V5LiveRuntimeError("V5 actual dt is outside (0,80ms)")
        self._last_monotonic_s = now
        elapsed = now - self._origin_monotonic_s
        self.timing.observe(elapsed)
        return actual_dt, elapsed

    def step(
        self,
        *,
        output: Any,
        sensor: SensorPacket,
        tp_state: V5TPState,
        path_time_s: float,
        monotonic_s: float,
        state_age_s: float = 0.0,
    ) -> V5ControlCommandV1:
        if not isinstance(sensor, SensorPacket):
            raise TypeError("V5 control sensor packet is not typed")
        if not isinstance(tp_state, V5TPState):
            raise TypeError("V5 control TP state is not typed")
        time_s = float(path_time_s)
        if not math.isfinite(time_s) or not 0.0 <= time_s <= TAIL_END_S:
            raise V5LiveRuntimeError("V5 control path time is outside [0,20pi]")
        actual_dt, elapsed = self._actual_dt(monotonic_s)
        startup_ready = self.startup.observe(sensor.heartbeat, elapsed)
        self._startup_ready_latched = self._startup_ready_latched or startup_ready
        if self.timing.stopped or self.startup.stopped:
            raise V5LiveRuntimeError(
                self.timing.stop_reason or self.startup.stop_reason
            )

        is_path_state = tp_state in _PATH_STATES
        latch_now = bool(
            sensor.sensor_fresh
            and self._startup_ready_latched
            and (sensor.normal_load_n >= 0.8 or sensor.force_norm_n >= 1.0)
        )
        if latch_now:
            self.sticky_one_newton_latched = 1
        same_tick_path = (
            tp_state is V5TPState.ONE_NEWTON_ENTRY
            and self.sticky_one_newton_latched == 1
        )
        path_mode = is_path_state or same_tick_path
        backend_mode = "path" if path_mode else "baseline" if tp_state is V5TPState.ONE_NEWTON_ENTRY else "hold"
        base_target = entry_target_n(time_s) if path_mode else 1.0
        filtered = self.active_backend.observe_filter(
            raw_normal_n=sensor.normal_load_n,
            actual_dt_s=actual_dt,
            setpoint_n=base_target,
            mode=backend_mode,
        )

        correction_receipt: CorrectionReceiptV1 | None = None
        effective_target = base_target
        if path_mode and time_s >= FORMAL_METRIC_START_S:
            context = v5_path_context(time_s, anchor_pose=self.anchor_pose)
            self.correction_state, correction_receipt = (
                self.active_spec.correction_policy.apply(
                    self.correction_state,
                    context,
                    fingerprint_sha256=(
                        self.active_spec.correction_fingerprint_sha256
                    ),
                    allow_equal_endpoint_hold=bool(
                        tp_state
                        in {
                            V5TPState.CLOSURE_TAIL,
                            V5TPState.ROLLOVER_PREPARED,
                        }
                        and time_s == math.nextafter(TAIL_END_S, 0.0)
                    ),
                )
            )
            effective_target = correction_receipt.effective_target_n
        if not 1.0 <= effective_target <= 6.25:
            raise V5LiveRuntimeError("V5 effective target exceeds its fixed bounds")

        can_move = bool(
            path_mode
            or (
                tp_state is V5TPState.ONE_NEWTON_ENTRY
                and self._startup_ready_latched
            )
        )
        if can_move:
            backend_command = self.active_backend.command(
                output=output,
                sensor=sensor,
                effective_target_n=effective_target,
                actual_dt_s=actual_dt,
                mode="path" if path_mode else "baseline",
                path_time_s=time_s if path_mode else 0.0,
                previous_qdot=self.previous_qdot,
                state_age_s=float(state_age_s),
            )
            qdot = backend_command.qdot
            gate_receipt = backend_command.gate_receipt
            command_mode = CommandMode.PATH if path_mode else CommandMode.BASELINE
        else:
            qdot = _ZERO_QDOT
            gate_receipt = {
                "startup": "two_fresh_increments_pending",
                "filter_only": True,
            }
            command_mode = CommandMode.HOLD if backend_mode == "hold" else CommandMode.BASELINE
        self.previous_qdot = _qdot(qdot, "V5 previous qdot")
        return V5ControlCommandV1(
            identity=self.active_spec.identity,
            command_mode=command_mode,
            qdot=self.previous_qdot,
            base_target_n=base_target,
            effective_target_n=effective_target,
            filtered_normal_n=filtered,
            sticky_one_newton_latched=self.sticky_one_newton_latched,
            path_time_s=time_s,
            actual_dt_s=actual_dt,
            correction_receipt=correction_receipt,
            gate_receipt=gate_receipt,
        )


def mature_backend_factory(
    *,
    path_reference: Callable[[str, tuple[float, float], float], Mapping[str, Any]],
    guard_stack: FigureEightPathGuardStackV1,
) -> BackendFactory:
    def factory(spec: V5CandidateRuntimeSpecV1) -> MatureV5ControlBackend:
        return MatureV5ControlBackend(
            spec,
            path_reference=path_reference,
            guard_stack=guard_stack,
        )

    return factory


__all__ = [
    "BackendFactory",
    "MatureV5ControlBackend",
    "V5BackendCommandV1",
    "V5CandidateRuntimeSpecV1",
    "V5ChainControlV1",
    "V5ControlBackend",
    "V5ControlCommandV1",
    "V5LiveRuntimeError",
    "V5_LIVE_RUNTIME_SCHEMA",
    "V5_LIVE_RUNTIME_VERSION",
    "make_v5_runtime_path_reference",
    "mature_backend_factory",
    "v5_path_context",
]
