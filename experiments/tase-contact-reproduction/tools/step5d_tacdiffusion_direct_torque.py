#!/usr/bin/env python3
"""Deterministic Step5d Direct Torque command core.

This module is deliberately free of network, Dashboard, RTDE, serial, upload,
and motion side effects.  The 50 Hz TacDiffusion fixture is diagnostic-only;
no fixture value is reachable from the serialized controller command.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from pathlib import Path
import struct
import sys
from typing import Any, Callable, Mapping, Sequence


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
VARIABLE_IMPEDANCE_ROOT = EXPERIMENT_ROOT.parent / "ur10e-variable-impedance"
for candidate in (EXPERIMENT_ROOT / "tools", VARIABLE_IMPEDANCE_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from step5_table import step5_path_reference  # noqa: E402
from step5d_paper_outer_loop import (  # noqa: E402
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
)
from ur10e_vic.backends import (  # noqa: E402
    DIRECT_TORQUE_FRAME_TOKEN,
    DirectTorquePacket,
    validate_mainline_stiffness,
)
from ur10e_vic.tacdiffusion.action import (  # noqa: E402
    ActionProfile,
    TacDiffusionAction,
    derive_damping,
    guard_action,
)
from ur10e_vic.tacdiffusion.dynamic_filter import (  # noqa: E402
    DynamicFilterProfile,
    RateInvariantForceFilter,
)
from ur10e_vic.tacdiffusion.expert import (  # noqa: E402
    DeterministicExpert,
    ExpertFrameSemantics,
    ExpertInput,
)
from ur10e_vic.tacdiffusion.mailbox import LatestModelMailbox  # noqa: E402
from ur10e_vic.tacdiffusion.trajectory import EpisodeReference  # noqa: E402
from ur10e_vic.tacdiffusion.promotion import validate_live_authorization  # noqa: E402


CONTROL_HZ = 500
CONTROL_DT_S = 1.0 / CONTROL_HZ
SHADOW_HZ = 50
SHADOW_PERIOD_TICKS = CONTROL_HZ // SHADOW_HZ
# V3 explicitly binds its trajectory/control behavior to the frozen v35 row;
# the V3 selection row itself intentionally omits duplicate trajectory fields.
STAGE_ID = "step5d_strict_rnn_ablation_v35"
FIXED_STIFFNESS = (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
FIXED_DAMPING = (
    69.2820323,
    69.2820323,
    69.2820323,
    4.898979486,
    4.898979486,
    4.898979486,
)
JOINT_DAMPING = (1.5, 1.5, 1.2, 0.3, 0.3, 0.2)
FORCE_NORM_LIMIT_N = 50.0
TORQUE_NORM_LIMIT_NM = 3.0
JOINT_SPEED_LIMIT_RAD_S = 1.0
JOINT_TORQUE_LIMIT_NM = (20.0, 20.0, 20.0, 8.0, 8.0, 8.0)
TRANSLATION_SLEW_LIMIT_M_S = 0.05
ORIENTATION_SLEW_LIMIT_RAD_S = 0.05
TCP_CAGE_MIN_M = (0.3, 0.0, -0.02)
TCP_CAGE_MAX_M = (0.55, 0.24, 0.12)
SOFTWARE_BASELINE_SAMPLES = 1000
MODEL_MODE_MAINLINE = 1
MODEL_RATE_HZ = 100
MODEL_PERIOD_US = int(1_000_000 / MODEL_RATE_HZ)
LEGACY_ZERO_WRENCH = tuple(0.0 for _ in range(6))

OUTER_CONFIG = Step5dOuterLoopConfig(
    kp=4.0,
    ko=0.5,
    kf=1.0,
    Md_scalar=240.0,
    Bd_scalar=11_000.0,
    force_target_n=12.0,
    delay_T_s=CONTROL_DT_S,
    force_sign_convention="step5_step6_positive_normal_load",
)


def _vector(values: Sequence[float], length: int, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != length or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain {length} finite values")
    return result


def _norm(values: Sequence[float]) -> float:
    return math.sqrt(sum(float(value) ** 2 for value in values))


def _limit_norm(values: Sequence[float], maximum: float) -> tuple[float, ...]:
    vector = _vector(values, len(values), "limited vector")
    magnitude = _norm(vector)
    if magnitude <= maximum or magnitude <= 1e-15:
        return vector
    scale = maximum / magnitude
    return tuple(value * scale for value in vector)


@dataclass(frozen=True)
class RuntimeSample:
    tick: int
    elapsed_s: float
    tcp_pose_base: tuple[float, ...] | Sequence[float]
    tcp_speed_base: tuple[float, ...] | Sequence[float]
    joint_position_rad: tuple[float, ...] | Sequence[float]
    joint_speed_rad_s: tuple[float, ...] | Sequence[float]
    joint_torque_nm: tuple[float, ...] | Sequence[float]
    wrench_tcp_si: tuple[float, ...] | Sequence[float]
    control_reaction_normal_base: tuple[float, ...] | Sequence[float]
    trajectory_progress_s: float | None = None
    trajectory_duration_s: float | None = None
    episode_reference: Mapping[str, Any] | EpisodeReference | None = None

    def __post_init__(self) -> None:
        if self.tick < 0:
            raise ValueError("tick must be non-negative")
        if not math.isfinite(self.elapsed_s) or self.elapsed_s < 0.0:
            raise ValueError("elapsed_s must be finite and non-negative")
        for name, length in (
            ("tcp_pose_base", 6),
            ("tcp_speed_base", 6),
            ("joint_position_rad", 6),
            ("joint_speed_rad_s", 6),
            ("joint_torque_nm", 6),
            ("wrench_tcp_si", 6),
            ("control_reaction_normal_base", 3),
        ):
            object.__setattr__(self, name, _vector(getattr(self, name), length, name))
        if (self.trajectory_progress_s is None) != (self.trajectory_duration_s is None):
            raise ValueError("trajectory progress and duration must be supplied together")
        if self.trajectory_progress_s is not None:
            if not math.isfinite(self.trajectory_progress_s) or not math.isfinite(self.trajectory_duration_s) or self.trajectory_progress_s < 0.0 or self.trajectory_duration_s <= 0.0:
                raise ValueError("trajectory progress/duration must be finite and bounded")


@dataclass(frozen=True)
class FixtureShadowDiagnostic:
    sequence: int
    status: str
    finite: bool
    value: tuple[float, ...]


@dataclass(frozen=True)
class CommandResult:
    packet: DirectTorquePacket
    command_bytes: bytes
    equilibrium_pose: tuple[float, ...]
    outer_state: Step5dOuterLoopState
    shadow: FixtureShadowDiagnostic
    model_sequence: int = 0
    model_mode: int = 0
    episode_failed: bool = False
    failure_reason: str = ""
    shadow_model_action: tuple[float, ...] | None = None


class SoftwareWrenchBaseline:
    """Exactly 1000 software samples; never issues a sensor tare command."""

    def __init__(self, required_samples: int = SOFTWARE_BASELINE_SAMPLES) -> None:
        if required_samples != SOFTWARE_BASELINE_SAMPLES:
            raise ValueError("Step5d requires exactly 1000 software baseline samples")
        self._count = 0
        self._sum = [0.0] * 6

    @property
    def ready(self) -> bool:
        return self._count == SOFTWARE_BASELINE_SAMPLES

    @property
    def count(self) -> int:
        return self._count

    def add(self, wrench_tcp_si: Sequence[float]) -> None:
        if self.ready:
            raise RuntimeError("software baseline is already complete")
        values = _vector(wrench_tcp_si, 6, "wrench_tcp_si")
        for index, value in enumerate(values):
            self._sum[index] += value
        self._count += 1

    def bias(self) -> tuple[float, ...]:
        if not self.ready:
            raise RuntimeError("software baseline is incomplete")
        return tuple(value / SOFTWARE_BASELINE_SAMPLES for value in self._sum)


class FixtureShadowRunner:
    """50 Hz deterministic diagnostic adapter with fail-closed diagnostics."""

    def __init__(self, evaluator: Callable[[RuntimeSample], Sequence[float]] | None) -> None:
        self._evaluator = evaluator
        self._sequence = 0
        self._last = FixtureShadowDiagnostic(0, "disabled", True, (0.0,) * 6)

    def tick(self, sample: RuntimeSample) -> FixtureShadowDiagnostic:
        if sample.tick % SHADOW_PERIOD_TICKS != 0:
            return self._last
        self._sequence += 1
        if self._evaluator is None:
            self._last = FixtureShadowDiagnostic(
                self._sequence, "disabled", True, (0.0,) * 6
            )
            return self._last
        try:
            value = tuple(float(item) for item in self._evaluator(sample))
            finite = len(value) == 6 and all(math.isfinite(item) for item in value)
            status = "ok" if finite else "invalid"
        except Exception:
            value = (0.0,) * 6
            finite = False
            status = "crashed"
        self._last = FixtureShadowDiagnostic(self._sequence, status, finite, value)
        return self._last


def encode_controller_packet(packet: DirectTorquePacket) -> bytes:
    """Serialize only authoritative RTDE command fields in register order."""

    integers = (
        packet.mode,
        packet.sequence_after,
        packet.heartbeat,
        packet.lease_id,
        packet.model_sequence_after,
        packet.model_period_us,
        packet.model_mode,
        packet.wrench_frame_token,
        packet.model_timestamp_us,
        packet.home_ack_identity,
        packet.home_consume_identity,
        packet.episode_identity,
    )
    doubles = (
        *packet.equilibrium_pose,
        *packet.stiffness,
        *packet.damping,
        *packet.raw_feedforward_wrench,
    )
    return struct.pack("!12i24d", *integers, *doubles)


class Step5dDirectTorqueCore:
    def __init__(
        self,
        *,
        lease_id: int,
        shadow: FixtureShadowRunner,
        mainline: bool = False,
        action_provider: Callable[[RuntimeSample], TacDiffusionAction] | None = None,
        expert: DeterministicExpert | None = None,
        model_mailbox: LatestModelMailbox | None = None,
        expert_input_provider: Callable[[RuntimeSample, Mapping[str, Any]], ExpertInput] | None = None,
        episode_reference_provider: Callable[[RuntimeSample], Mapping[str, Any] | EpisodeReference] | None = None,
        active_authorization_path: str | Path | None = None,
    ) -> None:
        if lease_id <= 0:
            raise ValueError("lease_id must be positive")
        self.lease_id = lease_id
        self._episode_identity = lease_id
        self.shadow = shadow
        self._last_tick = -1
        self._equilibrium_pose: tuple[float, ...] | None = None
        self._outer_state = Step5dOuterLoopState()
        self.mainline = bool(mainline)
        self.action_profile = ActionProfile()
        self._action_provider = action_provider
        self._expert = expert
        self._model_mailbox = model_mailbox
        self._expert_input_provider = expert_input_provider
        self._episode_reference_provider = episode_reference_provider
        self._active_allowed = self._validate_active_authorization(active_authorization_path)
        if self.mainline and self._action_provider is None and self._expert is None and self._model_mailbox is None:
            raise ValueError("mainline core requires DeterministicExpert or LatestModelMailbox")
        self._previous_action: TacDiffusionAction | None = None
        self._last_model_sequence = 0
        self._last_model_input: tuple[float, ...] | None = None
        self._last_model_guarded: TacDiffusionAction | None = None
        self._force_filter = RateInvariantForceFilter(
            DynamicFilterProfile(settling_time_s=0.05, damping_ratio=1.0, rate_hz=500)
        )
        self.episode_failed = False
        self.failure_reason = ""

    def reset_episode(self, *, episode_identity: int | None = None) -> None:
        """Reset episode-local state without changing the exclusive lease."""

        self._last_tick = -1
        if episode_identity is not None:
            if int(episode_identity) <= 0:
                raise ValueError("episode_identity must be positive")
            self._episode_identity = int(episode_identity)
        self._equilibrium_pose = None
        self._outer_state = Step5dOuterLoopState()
        self._previous_action = None
        self._last_model_sequence = 0
        self._last_model_input = None
        self._last_model_guarded = None
        self._force_filter.reset()
        if self._expert is not None:
            self._expert.reset()
        self.episode_failed = False
        self.failure_reason = ""

    @staticmethod
    def _episode_reference(
        sample: RuntimeSample,
        provider: Callable[[RuntimeSample], Mapping[str, Any] | EpisodeReference] | None,
    ) -> Mapping[str, Any]:
        supplied = provider(sample) if provider is not None else sample.episode_reference
        if supplied is None:
            raise RuntimeError("mainline_episode_reference_not_injected")
        if isinstance(supplied, EpisodeReference):
            reference: Mapping[str, Any] = supplied.as_mapping()
        elif isinstance(supplied, Mapping):
            reference = dict(supplied)
        else:
            raise TypeError("episode reference must be EpisodeReference or mapping")
        required = (
            "desired_pose_base",
            "desired_twist_base",
            "desired_acceleration_base",
            "progress_s",
            "duration_s",
            "target_load_n",
            "preload_n",
            "reaction_normal_base",
        )
        for key in required:
            if key not in reference:
                raise ValueError(f"episode reference missing {key}")
        for key in ("desired_pose_base", "desired_twist_base", "desired_acceleration_base"):
            reference[key] = _vector(reference[key], 6, key)
        for key in ("progress_s", "duration_s", "target_load_n", "preload_n"):
            value = float(reference[key])
            if not math.isfinite(value):
                raise ValueError(f"episode reference {key} must be finite")
            reference[key] = value
        if reference["progress_s"] < 0.0 or reference["duration_s"] <= 0.0:
            raise ValueError("episode reference progress/duration is invalid")
        if reference["target_load_n"] < 0.0 or reference["preload_n"] < 0.0:
            raise ValueError("episode reference loads must be non-negative")
        reaction = _vector(reference["reaction_normal_base"], 3, "reaction_normal_base")
        if abs(_norm(reaction) - 1.0) > 1e-6:
            raise ValueError("episode reference reaction normal must be unit length")
        reference["reaction_normal_base"] = reaction
        reference.setdefault("desired_xy", tuple(reference["desired_pose_base"][:2]))
        reference.setdefault("desired_velocity_xy", tuple(reference["desired_twist_base"][:2]))
        if len(tuple(reference["desired_xy"])) != 2 or len(tuple(reference["desired_velocity_xy"])) != 2:
            raise ValueError("episode reference xy fields must contain two values")
        if not all(math.isfinite(float(value)) for value in tuple(reference["desired_xy"]) + tuple(reference["desired_velocity_xy"])):
            raise ValueError("episode reference xy fields must be finite")
        return reference

    @staticmethod
    def _rotation_base_tcp(rotvec: Sequence[float]) -> tuple[tuple[float, ...], ...]:
        vector = _vector(rotvec, 3, "tcp rotation vector")
        angle = _norm(vector)
        if angle <= 1e-12:
            return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        x, y, z = (value / angle for value in vector)
        c, s = math.cos(angle), math.sin(angle)
        v = 1.0 - c
        return (
            (c + x * x * v, x * y * v - z * s, x * z * v + y * s),
            (y * x * v + z * s, c + y * y * v, y * z * v - x * s),
            (z * x * v - y * s, z * y * v + x * s, c + z * z * v),
        )

    @staticmethod
    def _rotate6(rotation: Sequence[Sequence[float]], vector: Sequence[float]) -> tuple[float, ...]:
        values = _vector(vector, 6, "twist/wrench")
        return tuple(
            sum(rotation[row][column] * values[column] for column in range(3))
            for row in range(3)
        ) + tuple(
            sum(rotation[row][column] * values[column + 3] for column in range(3))
            for row in range(3)
        )

    @staticmethod
    def _validate_active_authorization(path: str | Path | None) -> bool:
        if path is None:
            return False
        if isinstance(path, bool):
            raise TypeError("active authorization requires a validated path, not a boolean")
        return validate_live_authorization(path).active_allowed

    @staticmethod
    def _expert_input(sample: RuntimeSample, reference: Mapping[str, Any]) -> ExpertInput:
        reaction_base = tuple(float(value) for value in reference["reaction_normal_base"])
        if len(reaction_base) != 3 or not all(math.isfinite(value) for value in reaction_base):
            raise ValueError("expert reaction normal is non-finite")
        norm = _norm(reaction_base)
        if not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError("expert reaction/approach normal must be unit length")
        approach_base = tuple(-value for value in reaction_base)
        r_base_tcp = Step5dDirectTorqueCore._rotation_base_tcp(sample.tcp_pose_base[3:6])
        r_tcp_base = tuple(tuple(r_base_tcp[column][row] for column in range(3)) for row in range(3))
        reaction_tcp = tuple(sum(r_tcp_base[row][column] * reaction_base[column] for column in range(3)) for row in range(3))
        approach_tcp = tuple(-value for value in reaction_tcp)
        semantics = ExpertFrameSemantics(
            reaction_normal_base=reaction_base,
            approach_normal_base=approach_base,
            base_to_tcp_rotation=r_tcp_base,
        )
        normal_load = sum(sample.wrench_tcp_si[index] * reaction_tcp[index] for index in range(3))
        pose_error_base = (
            *tuple(reference["desired_pose_base"])[:3],
            *tuple(reference["desired_pose_base"])[3:],
        )
        pose_error_base = tuple(
            desired - actual for desired, actual in zip(pose_error_base, sample.tcp_pose_base)
        )
        desired_twist_base = tuple(reference["desired_twist_base"])
        twist_tcp = Step5dDirectTorqueCore._rotate6(r_tcp_base, sample.tcp_speed_base)
        desired_twist = Step5dDirectTorqueCore._rotate6(r_tcp_base, desired_twist_base)
        pose_error = Step5dDirectTorqueCore._rotate6(r_tcp_base, pose_error_base)
        desired_acceleration = Step5dDirectTorqueCore._rotate6(
            r_tcp_base, tuple(reference["desired_acceleration_base"])
        )
        tangential_speed = math.sqrt(twist_tcp[0] ** 2 + twist_tcp[1] ** 2)
        return ExpertInput(
            normal_load_n=normal_load,
            target_load_n=float(reference["target_load_n"]),
            pose_error=pose_error,
            twist=twist_tcp,
            path_progress=min(1.0, max(0.0, float(reference["progress_s"]) / float(reference["duration_s"]))),
            tangential_speed_m_s=tangential_speed,
            desired_twist=desired_twist,
            desired_acceleration=desired_acceleration,
            frame_semantics=semantics,
        )

    @staticmethod
    def assert_runtime_guards(sample: RuntimeSample) -> None:
        if _norm(sample.wrench_tcp_si[:3]) > FORCE_NORM_LIMIT_N:
            raise RuntimeError("force_norm_guard")
        if _norm(sample.wrench_tcp_si[3:]) > TORQUE_NORM_LIMIT_NM:
            raise RuntimeError("torque_norm_guard")
        if max(abs(value) for value in sample.joint_speed_rad_s) > JOINT_SPEED_LIMIT_RAD_S:
            raise RuntimeError("joint_speed_guard")
        if any(
            abs(value) > JOINT_TORQUE_LIMIT_NM[index]
            for index, value in enumerate(sample.joint_torque_nm)
        ):
            raise RuntimeError("joint_torque_guard")
        if any(
            sample.tcp_pose_base[index] < TCP_CAGE_MIN_M[index]
            or sample.tcp_pose_base[index] > TCP_CAGE_MAX_M[index]
            for index in range(3)
        ):
            raise RuntimeError("tcp_cage_guard")

    def tick(self, sample: RuntimeSample) -> CommandResult:
        if sample.tick != self._last_tick + 1:
            raise RuntimeError("non_contiguous_control_tick")
        self.assert_runtime_guards(sample)
        if self.mainline:
            # Mainline references are injected from SurfaceCalibration /
            # BoundedTrajectory.  The frozen cycloid is legacy-only and must
            # never become a hidden progress or pose source here.
            reference = self._episode_reference(sample, self._episode_reference_provider)
        else:
            reference = step5_path_reference(
                STAGE_ID,
                (sample.tcp_pose_base[0], sample.tcp_pose_base[1]),
                sample.elapsed_s,
            )
            reference = dict(reference)
            reference.setdefault("desired_pose_base", (*reference["desired_xy"], sample.tcp_pose_base[2], 0.0, 0.0, 0.0))
            reference.setdefault("desired_twist_base", (*reference["desired_velocity_xy"], 0.0, 0.0, 0.0, 0.0))
            reference.setdefault("desired_acceleration_base", (0.0,) * 6)
            reference.setdefault("progress_s", float(reference.get("progress", sample.elapsed_s)))
            reference.setdefault("duration_s", float(reference.get("duration_s", 1.0)))
            reference.setdefault("target_load_n", OUTER_CONFIG.force_target_n)
            reference.setdefault("preload_n", 0.0)
            reference.setdefault("reaction_normal_base", sample.control_reaction_normal_base)
        outer_config = OUTER_CONFIG
        if self.mainline:
            outer_config = replace(OUTER_CONFIG, force_target_n=float(reference["target_load_n"]))
        outer = compute_step5d_outer_loop(
            outer_config,
            self._outer_state,
            Step5dOuterLoopInputs(
                tcp_pose_base=sample.tcp_pose_base,
                tcp_speed_base=sample.tcp_speed_base,
                force_tcp_n=sample.wrench_tcp_si[:3],
                x_pd_base=(
                    reference["desired_xy"][0],
                    reference["desired_xy"][1],
                    sample.tcp_pose_base[2],
                ),
                xdot_pd_base=(
                    reference["desired_velocity_xy"][0],
                    reference["desired_velocity_xy"][1],
                    0.0,
                ),
                dt_s=CONTROL_DT_S,
                cmd_valid=True,
                control_reaction_normal_base=reference["reaction_normal_base"],
            ),
            include_diagnostics=False,
        )
        if not outer.cmd_valid:
            raise RuntimeError("outer_loop_invalid")
        linear = _limit_norm(outer.xdot_c[:3], TRANSLATION_SLEW_LIMIT_M_S)
        angular = _limit_norm(outer.xdot_c[3:], ORIENTATION_SLEW_LIMIT_RAD_S)
        previous = self._equilibrium_pose or tuple(sample.tcp_pose_base)
        equilibrium = tuple(
            previous[index] + (linear + angular)[index] * CONTROL_DT_S
            for index in range(6)
        )
        if any(
            equilibrium[index] < TCP_CAGE_MIN_M[index]
            or equilibrium[index] > TCP_CAGE_MAX_M[index]
            for index in range(3)
        ):
            raise RuntimeError("equilibrium_tcp_cage_guard")
        sequence = sample.tick + 1
        model_sequence = sample.tick // SHADOW_PERIOD_TICKS + 1
        model_mode = 1
        model_timestamp_us = 0
        model_period_us = 0
        episode_failed = False
        failure_reason = ""
        shadow_model_action: tuple[float, ...] | None = None
        if self.mainline:
            try:
                model_timestamp_us = int(round(sample.elapsed_s * 1_000_000.0))
                # An authorization artifact alone never makes an expert
                # command ACTIVE.  ACTIVE is selected only for a fresh,
                # packet-mode=active mailbox row after full validation.
                model_mode = 1
                model_sequence = sample.tick + 1
                model_period_us = MODEL_PERIOD_US
                if self._model_mailbox is not None:
                    mailbox_read = self._model_mailbox.read(
                        now_s=sample.elapsed_s, max_age_s=0.020
                    )
                    if mailbox_read.packet is not None:
                        if mailbox_read.packet.mode == "active" and not self._active_allowed:
                            raise RuntimeError("active_model_authorization_missing")
                        model_mode = 2 if mailbox_read.packet.mode == "active" else 1
                        if mailbox_read.packet.mode == "active":
                            proposed = mailbox_read.packet.action
                            model_sequence = mailbox_read.packet.sequence
                        else:
                            # Shadow model output is retained as diagnostics;
                            # the deterministic expert/action provider remains
                            # the sole serialized authority.
                            shadow_model_action = tuple(mailbox_read.packet.action.vector12)
                            proposed = None
                        model_period_us = MODEL_PERIOD_US
                        if model_mode == 2:
                            model_timestamp_us = int(round(mailbox_read.packet.timestamp_s * 1_000_000.0))
                        if model_mode == 2 and model_sequence == self._last_model_sequence:
                            if self._last_model_input is None or tuple(proposed.vector12) != self._last_model_input:
                                raise RuntimeError("held_model_payload_changed")
                            proposed = self._last_model_guarded
                        elif model_mode == 2 and self._last_model_sequence and model_sequence != self._last_model_sequence + 1:
                            raise RuntimeError("model_sequence_gap")
                    elif self._model_mailbox.latest_mode == "active":
                        raise RuntimeError(f"active_model_{mailbox_read.reason}")
                    elif self._expert is None and self._action_provider is None:
                        raise RuntimeError(f"model_{mailbox_read.reason}")
                    else:
                        proposed = None
                else:
                    proposed = None
                if proposed is None and self._action_provider is not None:
                    proposed = self._action_provider(sample)
                if proposed is None and self._expert is not None:
                    expert_input = (
                        self._expert_input_provider(sample, reference)
                        if self._expert_input_provider is not None
                        else self._expert_input(sample, reference)
                    )
                    proposed = self._expert.step(expert_input, dt_s=CONTROL_DT_S).action
                if proposed is None:
                    raise RuntimeError("mainline_action_source_empty")
                if not isinstance(proposed, TacDiffusionAction):
                    proposed = TacDiffusionAction(
                        tuple(proposed[:6]), tuple(proposed[6:12])
                    )
                guarded = guard_action(
                    proposed,
                    previous=self._previous_action,
                    dt_s=CONTROL_DT_S,
                    profile=self.action_profile,
                )
                self._force_filter.step(guarded.raw_f_df, dt_s=CONTROL_DT_S)
                self._previous_action = guarded
                self._last_model_sequence = model_sequence
                self._last_model_input = tuple(proposed.vector12)
                self._last_model_guarded = guarded
                raw_feedforward = guarded.raw_f_df
                stiffness = guarded.stiffness
                validate_mainline_stiffness(stiffness)
                damping = derive_damping(stiffness, self.action_profile)
            except (TypeError, ValueError, IndexError, RuntimeError) as exc:
                # A stale/nonfinite provider fails only this episode and the
                # explicit filter transition smooths the force command to zero.
                self.episode_failed = True
                self.failure_reason = f"model_action:{type(exc).__name__}"
                episode_failed = True
                failure_reason = self.failure_reason
                self._force_filter.smooth_to_zero(dt_s=CONTROL_DT_S)
                raw_feedforward = LEGACY_ZERO_WRENCH
                stiffness = self._previous_action.stiffness if self._previous_action else self.action_profile.stiffness_baseline
                damping = derive_damping(stiffness, self.action_profile)
                model_timestamp_us = int(round(sample.elapsed_s * 1_000_000.0))
                model_sequence = sample.tick + 1
                model_period_us = MODEL_PERIOD_US
                model_mode = 1
        else:
            # Legacy fixture path is retained solely for the old packet oracle;
            # run_live constructs this core with mainline=True.
            stiffness = FIXED_STIFFNESS
            damping = FIXED_DAMPING
            raw_feedforward = LEGACY_ZERO_WRENCH
            model_mode = MODEL_MODE_MAINLINE
        packet = DirectTorquePacket(
            sequence_before=sequence,
            sequence_after=sequence,
            heartbeat=sequence,
            lease_id=self.lease_id,
            mode=1,
            equilibrium_pose=equilibrium,
            stiffness=stiffness,
            damping=damping,
            raw_feedforward_wrench=raw_feedforward,
            model_sequence_before=model_sequence,
            model_sequence_after=model_sequence,
            model_period_us=model_period_us,
            model_timestamp_us=model_timestamp_us,
            model_mode=model_mode,
            episode_identity=self._episode_identity,
            wrench_frame_token=DIRECT_TORQUE_FRAME_TOKEN,
        )
        diagnostic = self.shadow.tick(sample)
        result = CommandResult(
            packet=packet,
            command_bytes=encode_controller_packet(packet),
            equilibrium_pose=equilibrium,
            outer_state=outer.next_state,
            shadow=diagnostic,
            model_sequence=model_sequence,
            model_mode=model_mode,
            episode_failed=episode_failed,
            failure_reason=failure_reason,
            shadow_model_action=shadow_model_action,
        )
        self._last_tick = sample.tick
        self._equilibrium_pose = equilibrium
        self._outer_state = outer.next_state
        return result
