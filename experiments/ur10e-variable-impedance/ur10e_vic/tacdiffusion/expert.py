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


def _finite_xyz(values: Sequence[float] | float, name: str) -> tuple[float, float, float]:
    if isinstance(values, (int, float)):
        result = (float(values),) * 3
    else:
        result = tuple(float(value) for value in values[:3])
    if len(result) != 3 or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain three finite values")
    return result


def _norm_xyz(values: Sequence[float] | float, name: str) -> float:
    if isinstance(values, (int, float)):
        value = float(values)
        if not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        return abs(value)
    vector = _finite_xyz(values, name)
    return math.sqrt(sum(value * value for value in vector))


@dataclass(frozen=True)
class FixedKExpertV1:
    """Fixed impedance primitive: Kxyz=600 N/m and Krot=30 Nm/rad."""

    schema_version: str = "ur10e_fixed_k_expert/v1"
    mode: str = "fixed_k"
    translational_stiffness_n_m: float = 600.0
    rotational_stiffness_nm_rad: float = 30.0
    model_output_dimension: int = 6

    def __post_init__(self) -> None:
        if self.schema_version != "ur10e_fixed_k_expert/v1" or self.mode != "fixed_k":
            raise ValueError("unsupported FixedKExpertV1 identity")
        if self.translational_stiffness_n_m != 600.0 or self.rotational_stiffness_nm_rad != 30.0:
            raise ValueError("FixedKExpertV1 stiffness is frozen at 600/30")
        if self.model_output_dimension != 6:
            raise ValueError("FixedKExpertV1 model output must be 6D")

    @property
    def stiffness_6d(self) -> tuple[float, ...]:
        return (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)

    def stiffness(self, **_: object) -> tuple[float, ...]:
        return self.stiffness_6d

    def damping(self, *, profile: ActionProfile = ActionProfile()) -> tuple[float, ...]:
        return derive_damping(self.stiffness_6d, profile)

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "model_output_dimension": self.model_output_dimension,
            "stiffness_6d": list(self.stiffness_6d),
            "active": False,
            "shadow_only": True,
        }


@dataclass(frozen=True)
class FormalMotionFeedforwardV1:
    """Bounded tangential authority for a moving formal reference.

    The receiver already applies ``-D * actual_twist``.  Adding
    ``D * desired_twist`` here completes velocity-error damping without a new
    wire field.  A smooth, norm-bounded breakaway term supplies enough
    authority to cross the controller's residual low-speed deadband while the
    existing 50 N / 4 Nm action guard and slew limits remain authoritative.
    """

    schema_version: str = "ur10e_formal_motion_feedforward/v1"
    profile_id: str = "formal_motion_feedforward_v1"
    translational_virtual_mass_kg: float = 2.0
    damping_ratio: float = 1.0
    tangential_breakaway_force_n: float = 2.0
    breakaway_smoothing_speed_m_s: float = 0.0005

    def __post_init__(self) -> None:
        expected = {
            "translational_virtual_mass_kg": 2.0,
            "damping_ratio": 1.0,
            "tangential_breakaway_force_n": 2.0,
            "breakaway_smoothing_speed_m_s": 0.0005,
        }
        if self.schema_version != "ur10e_formal_motion_feedforward/v1":
            raise ValueError("unsupported formal motion feedforward schema")
        if self.profile_id != "formal_motion_feedforward_v1":
            raise ValueError("unsupported formal motion feedforward profile")
        for name, value in expected.items():
            if float(getattr(self, name)) != value:
                raise ValueError(f"formal motion feedforward {name} is frozen")

    def tangential_force_base(
        self,
        desired_twist_base: Sequence[float],
        stiffness_6d: Sequence[float],
        *,
        surface_normal_base: Sequence[float] = (0.0, 0.0, 1.0),
    ) -> tuple[float, float, float]:
        twist = tuple(float(value) for value in desired_twist_base)
        stiffness = tuple(float(value) for value in stiffness_6d)
        normal = tuple(float(value) for value in surface_normal_base)
        if len(twist) != 6 or len(stiffness) != 6 or len(normal) != 3:
            raise ValueError("formal motion feedforward vector shape is invalid")
        if not all(math.isfinite(value) for value in (*twist, *stiffness, *normal)):
            raise ValueError("formal motion feedforward vectors must be finite")
        normal_norm = math.sqrt(sum(value * value for value in normal))
        if abs(normal_norm - 1.0) > 1.0e-9:
            raise ValueError("surface normal must be unit length")
        if max(stiffness[:3]) - min(stiffness[:3]) > 1.0e-9:
            raise ValueError("formal translational stiffness must be isotropic")
        velocity = twist[:3]
        normal_velocity = sum(value * axis for value, axis in zip(velocity, normal))
        tangent = tuple(
            velocity[index] - normal_velocity * normal[index] for index in range(3)
        )
        speed = math.sqrt(sum(value * value for value in tangent))
        damping = 2.0 * self.damping_ratio * math.sqrt(
            self.translational_virtual_mass_kg * stiffness[0]
        )
        if speed <= 1.0e-12:
            breakaway = (0.0, 0.0, 0.0)
        else:
            magnitude = self.tangential_breakaway_force_n * math.tanh(
                speed / self.breakaway_smoothing_speed_m_s
            )
            breakaway = tuple(magnitude * value / speed for value in tangent)
        force = tuple(damping * tangent[index] + breakaway[index] for index in range(3))
        if math.sqrt(sum(value * value for value in force)) > 3.0 + 1.0e-9:
            raise ValueError("formal tangential feedforward exceeded its 3 N envelope")
        return force

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "translational_virtual_mass_kg": self.translational_virtual_mass_kg,
            "damping_ratio": self.damping_ratio,
            "tangential_breakaway_force_n": self.tangential_breakaway_force_n,
            "breakaway_smoothing_speed_m_s": self.breakaway_smoothing_speed_m_s,
            "velocity_error_damping": True,
            "frame_id": "base",
        }


@dataclass(frozen=True)
class FormalHostPoseTrackingFeedforwardV1:
    """Host translational pose-error FF layered on FormalMotionFeedforwardV1.

    The receiver already applies ``K * (p_des - p_act)``.  Live free-space
    evidence showed that authority (~2–4 N including the 2 N tangential
    breakaway) remains inside the residual low-speed deadband.  This host term
    adds bounded position-error force on the wire so measured TCP excursion can
    clear the formal tracking gate without softening thresholds or raising the
    frozen on-controller K.
    """

    schema_version: str = "ur10e_formal_host_pose_feedforward/v1"
    profile_id: str = "formal_host_pose_feedforward_v1"
    position_tracking_stiffness_n_per_m: float = 12000.0
    position_tracking_force_cap_n: float = 25.0
    frame_id: str = "base"

    def __post_init__(self) -> None:
        if self.schema_version != "ur10e_formal_host_pose_feedforward/v1":
            raise ValueError("unsupported host pose feedforward schema")
        if self.profile_id != "formal_host_pose_feedforward_v1":
            raise ValueError("unsupported host pose feedforward profile")
        if float(self.position_tracking_stiffness_n_per_m) != 12000.0:
            raise ValueError("host pose feedforward stiffness is frozen")
        if float(self.position_tracking_force_cap_n) != 25.0:
            raise ValueError("host pose feedforward force cap is frozen")
        if self.frame_id != "base":
            raise ValueError("host pose feedforward frame_id must be base")

    def position_force_base(
        self,
        desired_pose_base: Sequence[float],
        actual_pose_base: Sequence[float],
        *,
        surface_normal_base: Sequence[float] = (0.0, 0.0, 1.0),
        project_off_normal: bool = False,
    ) -> tuple[float, float, float]:
        desired = tuple(float(value) for value in desired_pose_base)
        actual = tuple(float(value) for value in actual_pose_base)
        normal = tuple(float(value) for value in surface_normal_base)
        if len(desired) != 6 or len(actual) != 6 or len(normal) != 3:
            raise ValueError("host pose feedforward vector shape is invalid")
        if not all(math.isfinite(value) for value in (*desired, *actual, *normal)):
            raise ValueError("host pose feedforward vectors must be finite")
        normal_norm = math.sqrt(sum(value * value for value in normal))
        if abs(normal_norm - 1.0) > 1.0e-9:
            raise ValueError("surface normal must be unit length")
        error = tuple(desired[index] - actual[index] for index in range(3))
        force = tuple(
            self.position_tracking_stiffness_n_per_m * error[index]
            for index in range(3)
        )
        if project_off_normal:
            normal_force = sum(value * axis for value, axis in zip(force, normal))
            force = tuple(
                force[index] - normal_force * normal[index] for index in range(3)
            )
        norm = math.sqrt(sum(value * value for value in force))
        if norm > self.position_tracking_force_cap_n + 1.0e-12:
            scale = self.position_tracking_force_cap_n / norm
            force = tuple(scale * value for value in force)
        return force

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "position_tracking_stiffness_n_per_m": self.position_tracking_stiffness_n_per_m,
            "position_tracking_force_cap_n": self.position_tracking_force_cap_n,
            "frame_id": self.frame_id,
            "layers_on": "formal_motion_feedforward_v1",
        }


@dataclass(frozen=True)
class VariableKExpertV1:
    """Bounded variable-K primitive and deterministic seventh-output label.

    ``raw_stiffness`` is the deterministic expert target for the seventh
    learned model output.  At inference/expansion time the learned seventh
    output is the sole K authority; the observation formula is never run in
    that path.  The formula remains here so training labels and the learned
    controller action have one typed, versioned definition.
    """

    schema_version: str = "ur10e_variable_k_expert/v1"
    mode: str = "variable_k"
    base_n_m: float = 600.0
    error_scale_m: float = 0.010
    error_gain_n_m: float = 200.0
    force_deadband_n: float = 8.0
    force_scale_n: float = 4.0
    force_gain_n_m: float = 400.0
    min_n_m: float = 400.0
    max_n_m: float = 800.0
    rotational_stiffness_nm_rad: float = 30.0
    translational_slew_n_m_s: float = 400.0
    model_output_dimension: int = 7

    def __post_init__(self) -> None:
        if self.schema_version != "ur10e_variable_k_expert/v1" or self.mode != "variable_k":
            raise ValueError("unsupported VariableKExpertV1 identity")
        expected = {
            "base_n_m": 600.0,
            "error_scale_m": 0.010,
            "error_gain_n_m": 200.0,
            "force_deadband_n": 8.0,
            "force_scale_n": 4.0,
            "force_gain_n_m": 400.0,
            "min_n_m": 400.0,
            "max_n_m": 800.0,
            "rotational_stiffness_nm_rad": 30.0,
            "translational_slew_n_m_s": 400.0,
        }
        for name, value in expected.items():
            if float(getattr(self, name)) != value:
                raise ValueError(f"VariableKExpertV1 parameter {name} is frozen")
        if self.model_output_dimension != 7:
            raise ValueError("VariableKExpertV1 model output must be 7D")

    @staticmethod
    def _clip(value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

    def raw_stiffness(self, tracking_error: Sequence[float] | float, kunwei_force: Sequence[float] | float) -> float:
        error_norm = _norm_xyz(tracking_error, "tracking_error")
        force_norm = _norm_xyz(kunwei_force, "kunwei_force")
        error_term = self._clip(error_norm / self.error_scale_m, 0.0, 1.0)
        force_term = self._clip((force_norm - self.force_deadband_n) / self.force_scale_n, 0.0, 1.0)
        return self._clip(
            self.base_n_m + self.error_gain_n_m * error_term - self.force_gain_n_m * force_term,
            self.min_n_m,
            self.max_n_m,
        )

    def seventh_training_label(
        self,
        tracking_error: Sequence[float] | float,
        kunwei_force: Sequence[float] | float,
    ) -> float:
        """Return the bounded seventh model target in N/m.

        This is deliberately a named training-label seam.  It is not called
        by model-output expansion, which consumes the learned seventh value.
        """

        return self.raw_stiffness(tracking_error, kunwei_force)

    def training_label(
        self,
        tracking_error: Sequence[float] | float,
        kunwei_force: Sequence[float] | float,
        *,
        feedforward_wrench_6d: Sequence[float] = (0.0,) * 6,
    ) -> tuple[float, ...]:
        """Compose a typed 7D imitation target ``F_ff[6] + Kxyz``.

        The six force/torque values are supplied by the existing expert label
        path; only the seventh value is generated by this deterministic
        Variable-K formula.
        """

        force_label = tuple(float(value) for value in feedforward_wrench_6d)
        if len(force_label) != 6 or not all(math.isfinite(value) for value in force_label):
            raise ValueError("feedforward_wrench_6d must contain six finite values")
        return force_label + (self.seventh_training_label(tracking_error, kunwei_force),)

    # Explicit alias for dataset builders that name the model target rather
    # than the imitation label.
    model_output_label = training_label

    def stiffness_from_model_output(
        self,
        translational_k_n_m: float,
        *,
        previous_stiffness: Sequence[float] | float | None = None,
        dt_s: float = 1.0 / 500.0,
    ) -> tuple[float, ...]:
        """Expand the learned seventh output into isotropic Kxyz plus Krot.

        Bounding and slew limiting happen after the model output is received.
        No observation is available or consulted here by design.
        """

        if not math.isfinite(dt_s) or dt_s <= 0.0:
            raise ValueError("dt_s must be finite and positive")
        learned = float(translational_k_n_m)
        if not math.isfinite(learned):
            raise ValueError("learned translational K must be finite")
        applied = self._clip(learned, self.min_n_m, self.max_n_m)
        if previous_stiffness is not None:
            previous = _finite_xyz(previous_stiffness, "previous_stiffness")
            if max(previous) - min(previous) > 1e-9:
                raise ValueError("previous translational stiffness must be isotropic")
            maximum_delta = self.translational_slew_n_m_s * dt_s
            applied = self._clip(applied, previous[0] - maximum_delta, previous[0] + maximum_delta)
            applied = self._clip(applied, self.min_n_m, self.max_n_m)
        return (
            applied,
            applied,
            applied,
            self.rotational_stiffness_nm_rad,
            self.rotational_stiffness_nm_rad,
            self.rotational_stiffness_nm_rad,
        )

    def stiffness(
        self,
        tracking_error: Sequence[float] | float,
        kunwei_force: Sequence[float] | float,
        *,
        previous_stiffness: Sequence[float] | float | None = None,
        dt_s: float = 1.0 / 500.0,
    ) -> tuple[float, ...]:
        if not math.isfinite(dt_s) or dt_s <= 0.0:
            raise ValueError("dt_s must be finite and positive")
        return self.stiffness_from_model_output(
            self.seventh_training_label(tracking_error, kunwei_force),
            previous_stiffness=previous_stiffness,
            dt_s=dt_s,
        )

    def damping(
        self,
        tracking_error: Sequence[float] | float,
        kunwei_force: Sequence[float] | float,
        *,
        previous_stiffness: Sequence[float] | float | None = None,
        dt_s: float = 1.0 / 500.0,
        profile: ActionProfile = ActionProfile(),
    ) -> tuple[float, ...]:
        return derive_damping(
            self.stiffness(
                tracking_error,
                kunwei_force,
                previous_stiffness=previous_stiffness,
                dt_s=dt_s,
            ),
            profile,
        )

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "model_output_dimension": self.model_output_dimension,
            "formula": "clip(600 + 200*clip(e/0.010,0,1) - 400*clip((f-8)/4,0,1),400,800)",
            "seventh_output_semantics": "learned_isotropic_translational_K_n_m",
            "seventh_output_training_label": "deterministic_formula_above",
            "rotational_stiffness_nm_rad": 30.0,
            "translational_slew_n_m_s": 400.0,
            "active": False,
            "shadow_only": True,
        }


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
