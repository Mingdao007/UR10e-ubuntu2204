"""Deterministic state-dependent expert and pre-filter imitation labels."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Sequence

from .action import ActionProfile, TacDiffusionAction, derive_damping, guard_action


@dataclass(frozen=True)
class ExpertFrameSemantics:
    reaction_normal_base: tuple[float, float, float] = (0.0, 0.0, 1.0)
    approach_normal_base: tuple[float, float, float] = (0.0, 0.0, -1.0)
    normal_load_definition: str = "dot(environment_on_tool_reaction, reaction_normal_base)"
    normal_command_definition: str = "command_along_approach_normal_base"
    base_to_tcp_rotation: tuple[tuple[float, ...], ...] = (
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    )

    def __post_init__(self) -> None:
        if tuple(-value for value in self.reaction_normal_base) != tuple(self.approach_normal_base):
            raise ValueError("approach_normal must be the negative reaction_normal")
        if "reaction_normal_base" not in self.normal_load_definition or "approach_normal_base" not in self.normal_command_definition:
            raise ValueError("expert normal frame semantics must be named")
        rotation = tuple(tuple(float(value) for value in row) for row in self.base_to_tcp_rotation)
        if len(rotation) != 3 or any(len(row) != 3 for row in rotation):
            raise ValueError("base_to_tcp_rotation must be 3x3")
        if not all(math.isfinite(value) for row in rotation for value in row):
            raise ValueError("base_to_tcp_rotation must be finite")
        for row in range(3):
            for column in range(3):
                dot = sum(rotation[row][index] * rotation[column][index] for index in range(3))
                if abs(dot - (1.0 if row == column else 0.0)) > 1e-6:
                    raise ValueError("base_to_tcp_rotation must be orthonormal")
        object.__setattr__(self, "base_to_tcp_rotation", rotation)

    def base_vector_to_tcp(self, vector: Sequence[float]) -> tuple[float, float, float]:
        values = tuple(float(value) for value in vector)
        if len(values) != 3 or not all(math.isfinite(value) for value in values):
            raise ValueError("base vector must contain three finite values")
        return tuple(
            sum(self.base_to_tcp_rotation[row][column] * values[column] for column in range(3))
            for row in range(3)
        )


class ExpertState(str, Enum):
    CONTACT_ACQUIRE = "CONTACT_ACQUIRE"
    TRACK = "TRACK"
    LOW_CONTACT_RECOVERY = "LOW_CONTACT_RECOVERY"
    HIGH_FORCE_RELIEF = "HIGH_FORCE_RELIEF"
    STUCK_RECOVERY = "STUCK_RECOVERY"
    COMPLETE = "COMPLETE"
    FAILED_RETRACT = "FAILED_RETRACT"


@dataclass(frozen=True)
class ExpertInput:
    normal_load_n: float
    target_load_n: float
    pose_error: tuple[float, ...] | Sequence[float]
    twist: tuple[float, ...] | Sequence[float]
    path_progress: float
    tangential_speed_m_s: float
    fault: bool = False
    desired_twist: tuple[float, ...] | Sequence[float] = (0.0,) * 6
    desired_acceleration: tuple[float, ...] | Sequence[float] = (0.0,) * 6
    frame_semantics: ExpertFrameSemantics = ExpertFrameSemantics()

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in (self.normal_load_n, self.target_load_n, self.path_progress, self.tangential_speed_m_s)):
            raise ValueError("expert input scalars must be finite")
        if any(len(tuple(values)) != 6 for values in (self.pose_error, self.twist, self.desired_twist, self.desired_acceleration)):
            raise ValueError("expert pose_error/twist/acceleration must contain six values")
        if not all(math.isfinite(value) for value in tuple(self.pose_error) + tuple(self.twist) + tuple(self.desired_twist) + tuple(self.desired_acceleration)):
            raise ValueError("expert pose_error/twist/acceleration must be finite")


@dataclass(frozen=True)
class ExpertDecision:
    state: ExpertState
    action: TacDiffusionAction
    pre_filter_label: tuple[float, ...]
    damping: tuple[float, ...]
    frame_semantics: ExpertFrameSemantics
    normal_load_error_n: float


class DeterministicExpert:
    def __init__(self, *, profile: ActionProfile = ActionProfile(), contact_threshold_ratio: float = 0.65, high_force_ratio: float = 1.5) -> None:
        if not 0.0 < contact_threshold_ratio < 1.0 or high_force_ratio <= 1.0:
            raise ValueError("expert thresholds are invalid")
        self.profile = profile
        self.contact_threshold_ratio = contact_threshold_ratio
        self.high_force_ratio = high_force_ratio
        self.state = ExpertState.CONTACT_ACQUIRE
        self._previous_action: TacDiffusionAction | None = None

    def reset(self) -> None:
        self.state = ExpertState.CONTACT_ACQUIRE
        self._previous_action = None

    def step(self, observation: ExpertInput, *, dt_s: float = 0.002) -> ExpertDecision:
        previous_state = self.state
        target = max(1e-6, observation.target_load_n)
        if observation.fault:
            self.state = ExpertState.FAILED_RETRACT
        elif observation.path_progress >= 1.0:
            self.state = ExpertState.COMPLETE
        elif observation.normal_load_n > self.high_force_ratio * target:
            self.state = ExpertState.HIGH_FORCE_RELIEF
        elif observation.normal_load_n < self.contact_threshold_ratio * target:
            self.state = ExpertState.CONTACT_ACQUIRE if self.state == ExpertState.CONTACT_ACQUIRE else ExpertState.LOW_CONTACT_RECOVERY
        elif observation.tangential_speed_m_s < 1e-4 and observation.path_progress < 0.98:
            self.state = ExpertState.STUCK_RECOVERY
        else:
            self.state = ExpertState.TRACK
        twist = tuple(float(value) for value in observation.twist)
        desired_twist = tuple(float(value) for value in observation.desired_twist)
        desired_acceleration = tuple(float(value) for value in observation.desired_acceleration)
        k = self.profile.stiffness_baseline
        force = [0.0] * 6
        approach_tcp = observation.frame_semantics.base_vector_to_tcp(observation.frame_semantics.approach_normal_base)
        reaction_tcp = observation.frame_semantics.base_vector_to_tcp(observation.frame_semantics.reaction_normal_base)
        if self.state == ExpertState.CONTACT_ACQUIRE:
            normal_command = min(2.0, target)
            force[:3] = [normal_command * value for value in approach_tcp]
        elif self.state == ExpertState.LOW_CONTACT_RECOVERY:
            normal_command = min(3.0, target + 1.0)
            force[:3] = [normal_command * value for value in approach_tcp]
        elif self.state == ExpertState.HIGH_FORCE_RELIEF:
            relief = min(3.0, observation.normal_load_n - target)
            force[:3] = [relief * value for value in reaction_tcp]
        elif self.state == ExpertState.STUCK_RECOVERY:
            force[0] = 1.0 if desired_twist[0] >= 0.0 else -1.0
            force[1] = 1.0 if desired_twist[1] >= 0.0 else -1.0
        elif self.state == ExpertState.TRACK:
            # This is a new state-dependent trajectory-reference policy.  Its
            # label is the guarded pre-filter command below.  It does not use
            # the forbidden clean-room ``K*pose_error + D*twist`` formula.
            # Any impedance/PD term is controller-side auxiliary semantics,
            # never an authoritative imitation-label definition.
            normal_load_error_n = observation.target_load_n - observation.normal_load_n
            normal_force_command_along_approach_normal_n = max(-3.0, min(3.0, 0.8 * normal_load_error_n))
            # Only x/y are tangential in the canonical TCP action frame;
            # rotational wrench channels are never treated as tangential.
            for index in range(2):
                reference_tracking_term = 0.5 * (desired_twist[index] - twist[index])
                bounded_acceleration_term = 0.05 * desired_acceleration[index]
                force[index] = max(-1.5, min(1.5, reference_tracking_term + bounded_acceleration_term))
            force[:3] = [
                force[index] + normal_force_command_along_approach_normal_n * approach_tcp[index]
                for index in range(3)
            ]
        elif self.state in {ExpertState.COMPLETE, ExpertState.FAILED_RETRACT}:
            force = [0.0] * 6
        # A state transition is a new guarded command phase.  Do not let the
        # prior acquire sign smear a HIGH_FORCE_RELIEF reaction command (or
        # vice versa) into the opposite semantic direction.
        previous_for_guard = None if self.state != previous_state else self._previous_action
        action = guard_action(TacDiffusionAction(tuple(force), k, self.profile.frame_id), previous=previous_for_guard, dt_s=dt_s, profile=self.profile)
        self._previous_action = action
        return ExpertDecision(self.state, action, action.raw_f_df, derive_damping(action.stiffness, self.profile), observation.frame_semantics, observation.target_load_n - observation.normal_load_n)
