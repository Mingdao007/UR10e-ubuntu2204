"""Offline backend contracts; this module has no network or upload capability."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Sequence

from .contracts import BackendCommand, ImpedanceObservation, ImpedanceProposal
from .math3d import damped_least_squares


DIRECT_TORQUE_MIN_VERSION = (5, 25, 2)
OFFICIAL_API_EVIDENCE = {
    "direct_torque": "https://www.universal-robots.com/manuals/EN/HTML/SW5_25/Content/prod-scriptmanual/all_scripts/direct_torque.htm",
    "get_coriolis_and_centrifugal_torques": "https://www.universal-robots.com/manuals/EN/HTML/SW5_25/Content/prod-scriptmanual/all_scripts/get_coriolis_and_centrifugal_torques.htm",
    "get_jacobian": "https://www.universal-robots.com/manuals/EN/HTML/SW5_25/Content/prod-scriptmanual/all_scripts/get_jacobian.htm",
    "release_notes": "https://www.universal-robots.com/articles/ur/release-notes/release-note-software-version-525x/",
    "wrench_trans": "https://www.universal-robots.com/manuals/EN/HTML/SW5_25/Content/prod-scriptmanual/all_scripts/4ModuleUrmath.htm",
}
SURROGATE_CLAIM_BOUNDARY = (
    "velocity_admittance_surrogate_only; not torque impedance; "
    "not DBIL hardware reproduction"
)
BASELINE_TRANSLATIONAL_STIFFNESS = (600.0, 600.0, 600.0)
DIRECT_TORQUE_BASELINE_K = (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
DIRECT_TORQUE_K_MIN = (25.0, 25.0, 25.0, 0.5, 0.5, 0.5)
DIRECT_TORQUE_K_MAX = (1000.0, 1000.0, 1000.0, 60.0, 60.0, 60.0)
DIRECT_TORQUE_K_SLEW = (400.0, 400.0, 400.0, 20.0, 20.0, 20.0)
DIRECT_TORQUE_VIRTUAL_MASS = (2.0, 2.0, 2.0, 0.2, 0.2, 0.2)
DIRECT_TORQUE_FDF_ABS_MAX = (20.0, 20.0, 20.0, 2.0, 2.0, 2.0)
DIRECT_TORQUE_FDF_FORCE_NORM_MAX = 20.0
DIRECT_TORQUE_FDF_TORQUE_NORM_MAX = 2.0
DIRECT_TORQUE_MODEL_PERIOD_US = (2_000, 5_000, 10_000, 20_000)
DIRECT_TORQUE_CONTROL_PERIOD_US = 2_000
DIRECT_TORQUE_MODEL_STALE_PERIODS = 2
DIRECT_TORQUE_MODEL_MODES = (0, 1, 2)
DIRECT_TORQUE_MODEL_ACTIVE_ALLOWED = False
DIRECT_TORQUE_FRAME_TOKEN = 5_252_001
DIRECT_TORQUE_ORIENTATION_SLEW_RAD_S = 0.05
# The keys below are retained under the legacy packet oracle only.  Mainline
# packet construction uses the 12D action contract and validates all six K
# components dynamically; orientation K is not fixed there.
MAINLINE_DIRECT_TORQUE_CONTRACT = {
    "action_dimension": 12,
    "stiffness_components": 6,
    "stiffness_units": "N/m,Nm/rad",
    "damping_units": "N s/m,Nm s/rad",
    "orientation_stiffness_policy": "dynamic_bounded_slew",
}
DIRECT_TORQUE_FILTER_ALPHA = 0.9
DIRECT_TORQUE_FILTER_BETA = 0.3
DIRECT_TORQUE_MANIFEST_LIMITS = {
    "damping_max": [200.0, 200.0, 200.0, 30.0, 30.0, 30.0],
    "damping_sqrt_tolerance": 0.05,
    "equilibrium_translation_slew_max_m_s": 0.05,
    "equilibrium_orientation_slew_max_rad_s": DIRECT_TORQUE_ORIENTATION_SLEW_RAD_S,
    "feedforward_abs_max": list(DIRECT_TORQUE_FDF_ABS_MAX),
    "feedforward_force_norm_max_n": DIRECT_TORQUE_FDF_FORCE_NORM_MAX,
    "feedforward_torque_norm_max_nm": DIRECT_TORQUE_FDF_TORQUE_NORM_MAX,
    "filter_alpha": DIRECT_TORQUE_FILTER_ALPHA,
    "filter_beta": DIRECT_TORQUE_FILTER_BETA,
    "force_norm_abs_max_n": 50.0,
    "joint_damping": [1.5, 1.5, 1.2, 0.3, 0.3, 0.2],
    "joint_position_rad_min": [0.4, -2.0, -3.0, -0.8, 1.3, -1.5],
    "joint_position_rad_max": [0.9, -1.2, -2.2, -0.2, 1.8, -0.7],
    "joint_speed_abs_max_rad_s": 1.0,
    "new_run_release_force_norm_max_n": 2.0,
    "new_run_release_pose_error_max_m": 0.002,
    "new_run_release_torque_norm_max_nm": 0.2,
    "model_period_us_allowed": list(DIRECT_TORQUE_MODEL_PERIOD_US),
    "model_stale_periods": DIRECT_TORQUE_MODEL_STALE_PERIODS,
    "orientation_damping_fixed": [4.898979486, 4.898979486, 4.898979486],
    "orientation_stiffness_fixed": [30.0, 30.0, 30.0],
    "pose_error_abs_max": [0.05, 0.05, 0.05, 0.35, 0.35, 0.35],
    "stiffness_baseline": list(DIRECT_TORQUE_BASELINE_K),
    "stiffness_max": list(DIRECT_TORQUE_K_MAX),
    "stiffness_min": list(DIRECT_TORQUE_K_MIN),
    "stiffness_slew_max_per_s": list(DIRECT_TORQUE_K_SLEW),
    "tcp_cage_xyz_max_m": [0.55, 0.24, 0.12],
    "tcp_cage_xyz_min_m": [0.3, 0.0, -0.02],
    "torque_abs_max_nm": [20.0, 20.0, 20.0, 8.0, 8.0, 8.0],
    "torque_norm_abs_max_nm": 3.0,
    "virtual_mass": list(DIRECT_TORQUE_VIRTUAL_MASS),
}


def validate_mainline_stiffness(stiffness: Sequence[float]) -> tuple[float, ...]:
    """Validate the production six-axis K vector without a fixed orientation."""

    values = tuple(float(value) for value in stiffness)
    if len(values) != 6 or not all(math.isfinite(value) for value in values):
        raise ValueError("mainline stiffness must contain six finite values")
    for index, value in enumerate(values):
        if not DIRECT_TORQUE_K_MIN[index] <= value <= DIRECT_TORQUE_K_MAX[index]:
            raise ValueError("mainline stiffness out of bounds")
    return values


@dataclass(frozen=True)
class Step5bTwistInput:
    """A fresh command emitted by the already-validated Step5b scaffold."""

    sequence: int
    generated_at_s: float
    twist_base: tuple[float, ...] | Sequence[float]
    scaffold_sha256: str

    def __post_init__(self) -> None:
        twist = tuple(float(value) for value in self.twist_base)
        if self.sequence < 0:
            raise ValueError("Step5b sequence must be non-negative")
        if not math.isfinite(self.generated_at_s) or self.generated_at_s < 0.0:
            raise ValueError("Step5b timestamp must be finite and non-negative")
        if len(twist) != 6 or not all(math.isfinite(value) for value in twist):
            raise ValueError("Step5b twist must contain six finite values")
        if not re.fullmatch(r"[0-9a-f]{64}", self.scaffold_sha256):
            raise ValueError("Step5b scaffold hash must be a lowercase SHA-256")
        object.__setattr__(self, "twist_base", twist)


@dataclass(frozen=True)
class DirectTorquePacket:
    """Host-side representation used only to test controller packet gates."""

    sequence_before: int
    sequence_after: int
    heartbeat: int
    lease_id: int
    mode: int
    equilibrium_pose: tuple[float, ...] | Sequence[float]
    stiffness: tuple[float, ...] | Sequence[float]
    damping: tuple[float, ...] | Sequence[float]
    raw_feedforward_wrench: tuple[float, ...] | Sequence[float] = (0.0,) * 6
    model_sequence_before: int = 0
    model_sequence_after: int = 0
    model_period_us: int = 0
    model_timestamp_us: int = 0
    model_mode: int = 0
    home_ack_identity: int = 0
    home_consume_identity: int = 0
    episode_identity: int = 0
    wrench_frame_token: int = 0

    def __post_init__(self) -> None:
        for name in ("equilibrium_pose", "stiffness", "damping"):
            values = tuple(float(value) for value in getattr(self, name))
            if len(values) != 6 or not all(math.isfinite(value) for value in values):
                raise ValueError(f"direct-torque {name} must contain six finite values")
            object.__setattr__(self, name, values)
        feedforward = tuple(float(value) for value in self.raw_feedforward_wrench)
        if len(feedforward) != 6:
            raise ValueError("raw feed-forward wrench must contain six values")
        object.__setattr__(self, "raw_feedforward_wrench", feedforward)
        if self.model_timestamp_us < 0:
            raise ValueError("model timestamp must be non-negative")
        if self.home_ack_identity < 0 or self.home_consume_identity < 0:
            raise ValueError("home identity values must be non-negative")
        if self.episode_identity < 0:
            raise ValueError("episode identity must be non-negative")


@dataclass(frozen=True)
class DirectTorqueGuardState:
    last_sequence: int = 0
    lease_id: int = 0
    last_equilibrium_pose: tuple[float, ...] | None = None
    initial_orientation: tuple[float, ...] | None = None
    last_stiffness: tuple[float, ...] | None = None
    last_model_sequence: int = 0
    last_model_period_us: int = 0
    last_model_timestamp_us: int = 0
    last_model_mode: int = 0
    model_age_ticks: int = 0
    last_raw_feedforward_wrench: tuple[float, ...] = (0.0,) * 6
    filtered_feedforward_wrench: tuple[float, ...] = (0.0,) * 6
    filtered_feedforward_velocity: tuple[float, ...] = (0.0,) * 6
    applied_feedforward_wrench: tuple[float, ...] = (0.0,) * 6


@dataclass(frozen=True)
class DirectTorqueGuardDecision:
    accepted: bool
    reason: str
    next_state: DirectTorqueGuardState


def advance_force_domain_filter(
    raw_feedforward_wrench: Sequence[float],
    filtered_feedforward_wrench: Sequence[float],
    filtered_feedforward_velocity: Sequence[float],
    *,
    dt_s: float = 0.002,
    alpha: float = DIRECT_TORQUE_FILTER_ALPHA,
    beta: float = DIRECT_TORQUE_FILTER_BETA,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Semi-implicit Euler oracle for Fdd=alpha*(beta*(Fdf-Fff)-Fd)."""

    vectors = (
        tuple(float(value) for value in raw_feedforward_wrench),
        tuple(float(value) for value in filtered_feedforward_wrench),
        tuple(float(value) for value in filtered_feedforward_velocity),
    )
    if any(len(values) != 6 for values in vectors):
        raise ValueError("force-domain filter vectors must contain six values")
    if not all(math.isfinite(value) for values in vectors for value in values):
        raise ValueError("force-domain filter vectors must be finite")
    if not math.isfinite(dt_s) or dt_s <= 0.0:
        raise ValueError("force-domain filter dt must be positive")
    if (
        not math.isfinite(alpha)
        or not math.isfinite(beta)
        or alpha <= 0.0
        or beta <= 0.0
    ):
        raise ValueError("force-domain filter alpha/beta must be positive")
    raw, filtered, velocity = vectors
    next_filtered: list[float] = []
    next_velocity: list[float] = []
    for axis in range(6):
        acceleration = alpha * (
            beta * (raw[axis] - filtered[axis]) - velocity[axis]
        )
        axis_velocity = velocity[axis] + acceleration * dt_s
        axis_filtered = filtered[axis] + axis_velocity * dt_s
        if not math.isfinite(axis_velocity) or not math.isfinite(axis_filtered):
            raise ValueError("force-domain filter produced non-finite state")
        next_velocity.append(axis_velocity)
        next_filtered.append(axis_filtered)
    return tuple(next_filtered), tuple(next_velocity)


def monotonic_feedforward_decay(
    filtered_feedforward_wrench: Sequence[float], *, factor: float = 0.5
) -> tuple[float, ...]:
    """Component-wise fail-closed decay; each absolute value can only decrease."""

    values = tuple(float(value) for value in filtered_feedforward_wrench)
    if len(values) != 6 or not all(math.isfinite(value) for value in values):
        raise ValueError("feed-forward decay requires six finite values")
    if not math.isfinite(factor) or factor < 0.0 or factor >= 1.0:
        raise ValueError("feed-forward decay factor must be in [0, 1)")
    return tuple(value * factor for value in values)


def select_applied_feedforward_wrench(
    model_mode: int,
    filtered_feedforward_wrench: Sequence[float],
    *,
    active_allowed: bool = DIRECT_TORQUE_MODEL_ACTIVE_ALLOWED,
) -> tuple[float, ...]:
    """Mirror the capability boundary: shadow and disabled apply exact zeros."""

    filtered = tuple(float(value) for value in filtered_feedforward_wrench)
    if len(filtered) != 6 or not all(math.isfinite(value) for value in filtered):
        raise ValueError("applied feed-forward selection requires six finite values")
    if model_mode not in DIRECT_TORQUE_MODEL_MODES:
        raise ValueError("unknown direct-torque model mode")
    if model_mode == 2:
        if not active_allowed:
            raise PermissionError("model-active feed-forward is not authorized")
        return filtered
    return (0.0,) * 6


def validate_direct_torque_packet(
    packet: DirectTorquePacket,
    state: DirectTorqueGuardState,
    *,
    dt_s: float = 0.002,
    release_ready: bool,
    runtime_guard_ok: bool,
) -> DirectTorqueGuardDecision:
    """Python oracle for the offline URScript gate state machine.

    This does not generate torque.  Its purpose is to make frozen/mixed packet,
    slew, release-reset, and lease semantics executable in unit tests before
    URSim is available.
    """

    def reject(reason: str) -> DirectTorqueGuardDecision:
        return DirectTorqueGuardDecision(
            False,
            reason,
            replace(
                state,
                filtered_feedforward_wrench=monotonic_feedforward_decay(
                    state.filtered_feedforward_wrench
                ),
                filtered_feedforward_velocity=(0.0,) * 6,
                applied_feedforward_wrench=monotonic_feedforward_decay(
                    state.applied_feedforward_wrench
                ),
            ),
        )

    if not math.isfinite(dt_s) or dt_s <= 0.0:
        raise ValueError("direct-torque oracle dt must be positive")
    if packet.mode != 1:
        return reject("mode_not_armed")
    if (
        packet.sequence_before != packet.sequence_after
        or packet.heartbeat != packet.sequence_after
    ):
        return reject("mixed_or_uncommitted_packet")
    sequence = packet.sequence_after
    if sequence <= 0 or sequence != state.last_sequence + 1:
        return reject("frozen_gap_or_regressed_sequence")
    if packet.lease_id <= 0 or (
        state.lease_id != 0 and packet.lease_id != state.lease_id
    ):
        return reject("exclusive_lease_mismatch")
    if not runtime_guard_ok:
        return reject("runtime_guard_failed")

    raw_feedforward = tuple(packet.raw_feedforward_wrench)
    if packet.model_mode not in DIRECT_TORQUE_MODEL_MODES:
        return reject("model_mode_invalid")
    if packet.model_mode == 2 and not DIRECT_TORQUE_MODEL_ACTIVE_ALLOWED:
        return reject("model_active_not_authorized")
    if packet.model_mode == 0:
        if (
            packet.model_sequence_before != 0
            or packet.model_sequence_after != 0
            or packet.model_period_us != 0
            or packet.model_timestamp_us != 0
            or not all(
                math.isfinite(value) and abs(value) <= 1e-12
                for value in raw_feedforward
            )
        ):
            return reject("disabled_model_packet_not_zero")
        next_model_sequence = 0
        next_model_period_us = 0
        next_model_timestamp_us = 0
        next_model_mode = 0
        next_model_age_ticks = 0
        next_raw_feedforward = (0.0,) * 6
        next_filtered_feedforward = monotonic_feedforward_decay(
            state.filtered_feedforward_wrench
        )
        next_filtered_velocity = (0.0,) * 6
    else:
        if packet.model_sequence_before != packet.model_sequence_after:
            return reject("mixed_or_uncommitted_model_packet")
        model_sequence = packet.model_sequence_after
        if packet.wrench_frame_token != DIRECT_TORQUE_FRAME_TOKEN:
            return reject("wrench_frame_contract_mismatch")
        if packet.model_period_us not in DIRECT_TORQUE_MODEL_PERIOD_US:
            return reject("model_period_not_allowed")
        if not all(math.isfinite(value) for value in raw_feedforward):
            return reject("nonfinite_feedforward_wrench")
        if any(
            abs(raw_feedforward[index]) > DIRECT_TORQUE_FDF_ABS_MAX[index]
            for index in range(6)
        ):
            return reject("feedforward_component_out_of_bounds")
        if (
            math.sqrt(sum(value * value for value in raw_feedforward[:3]))
            > DIRECT_TORQUE_FDF_FORCE_NORM_MAX
        ):
            return reject("feedforward_force_norm_out_of_bounds")
        if (
            math.sqrt(sum(value * value for value in raw_feedforward[3:]))
            > DIRECT_TORQUE_FDF_TORQUE_NORM_MAX
        ):
            return reject("feedforward_torque_norm_out_of_bounds")

        first_model_packet = state.last_model_sequence == 0
        new_model_packet = (
            first_model_packet or model_sequence == state.last_model_sequence + 1
        )
        held_model_packet = model_sequence == state.last_model_sequence
        if model_sequence <= 0 or not (new_model_packet or held_model_packet):
            return reject("model_sequence_gap_or_regression")
        if held_model_packet:
            if (
                packet.model_period_us != state.last_model_period_us
                or packet.model_mode != state.last_model_mode
                or packet.model_timestamp_us != state.last_model_timestamp_us
                or any(
                    abs(
                        raw_feedforward[index]
                        - state.last_raw_feedforward_wrench[index]
                    )
                    > 1e-12
                    for index in range(6)
                )
                or state.last_stiffness is None
                or any(abs(packet.stiffness[index] - state.last_stiffness[index]) > 1e-12 for index in range(6))
            ):
                return reject("model_payload_changed_without_sequence_commit")
            next_model_age_ticks = state.model_age_ticks + 1
        else:
            next_model_age_ticks = 0
            if packet.model_timestamp_us < state.last_model_timestamp_us:
                return reject("model_timestamp_regression")
        if (
            next_model_age_ticks * DIRECT_TORQUE_CONTROL_PERIOD_US
            > DIRECT_TORQUE_MODEL_STALE_PERIODS * packet.model_period_us
        ):
            return reject("model_stale_over_two_periods")
        (
            next_filtered_feedforward,
            next_filtered_velocity,
        ) = advance_force_domain_filter(
            raw_feedforward,
            state.filtered_feedforward_wrench,
            state.filtered_feedforward_velocity,
            dt_s=dt_s,
        )
        next_model_sequence = model_sequence
        next_model_period_us = packet.model_period_us
        next_model_timestamp_us = packet.model_timestamp_us
        next_model_mode = packet.model_mode
        next_raw_feedforward = raw_feedforward
    for index, (stiffness, damping) in enumerate(
        zip(packet.stiffness, packet.damping)
    ):
        if not DIRECT_TORQUE_K_MIN[index] <= stiffness <= DIRECT_TORQUE_K_MAX[index]:
            return reject("stiffness_out_of_bounds")
        expected_damping = 2.0 * math.sqrt(
            stiffness * DIRECT_TORQUE_VIRTUAL_MASS[index]
        )
        if abs(damping - expected_damping) > 0.05:
            return reject("damping_sqrt_relation_mismatch")
    if any(abs(packet.stiffness[index] - 30.0) > 1e-6 for index in range(3, 6)):
        return reject("orientation_stiffness_not_fixed")

    first_packet = state.last_sequence == 0
    if first_packet:
        if not release_ready:
            return reject("new_run_release_gate_failed")
        if any(
            abs(packet.stiffness[index] - DIRECT_TORQUE_BASELINE_K[index]) > 1e-6
            for index in range(6)
        ):
            return reject("new_run_must_restore_baseline_stiffness")
        initial_orientation = tuple(packet.equilibrium_pose[3:6])
    else:
        assert state.last_stiffness is not None
        assert state.last_equilibrium_pose is not None
        assert state.initial_orientation is not None
        initial_orientation = state.initial_orientation
        for index in range(6):
            decrease = state.last_stiffness[index] - packet.stiffness[index]
            if decrease < -1e-6 or decrease > DIRECT_TORQUE_K_SLEW[index] * dt_s + 1e-6:
                return reject("stiffness_increase_or_slew_violation")
        if any(
            abs(packet.equilibrium_pose[index] - state.last_equilibrium_pose[index])
            > 0.05 * dt_s + 1e-6
            for index in range(3)
        ):
            return reject("equilibrium_translation_slew_violation")
        orientation_step = math.sqrt(
            sum(
                (
                    packet.equilibrium_pose[index + 3]
                    - state.last_equilibrium_pose[index + 3]
                )
                ** 2
                for index in range(3)
            )
        )
        if orientation_step > DIRECT_TORQUE_ORIENTATION_SLEW_RAD_S * dt_s + 1e-6:
            return reject("equilibrium_orientation_slew_violation")

    next_state = DirectTorqueGuardState(
        last_sequence=sequence,
        lease_id=packet.lease_id,
        last_equilibrium_pose=tuple(packet.equilibrium_pose),
        initial_orientation=initial_orientation,
        last_stiffness=tuple(packet.stiffness),
        last_model_sequence=next_model_sequence,
        last_model_period_us=next_model_period_us,
        last_model_timestamp_us=next_model_timestamp_us,
        last_model_mode=next_model_mode,
        model_age_ticks=next_model_age_ticks,
        last_raw_feedforward_wrench=next_raw_feedforward,
        filtered_feedforward_wrench=next_filtered_feedforward,
        filtered_feedforward_velocity=next_filtered_velocity,
        applied_feedforward_wrench=select_applied_feedforward_wrench(
            next_model_mode, next_filtered_feedforward
        ),
    )
    return DirectTorqueGuardDecision(True, "accepted", next_state)


def parse_polyscope_version(value: str) -> tuple[int, int, int]:
    match = re.search(r"(?:^|\D)(\d+)\.(\d+)(?:\.(\d+))?", value)
    if not match:
        raise ValueError(f"cannot parse PolyScope version: {value!r}")
    return tuple(int(part or 0) for part in match.groups())


def direct_torque_supported(value: str) -> bool:
    return parse_polyscope_version(value) >= DIRECT_TORQUE_MIN_VERSION


def require_direct_torque_version(value: str) -> None:
    if not direct_torque_supported(value):
        raise RuntimeError(
            "Direct Torque V2 backend requires verified PolyScope 5.25.2 or newer; "
            f"received {value!r}"
        )


class VelocityAdmittanceSurrogate:
    """Bounded 5.11-compatible DLS qdot path, explicitly not true impedance."""

    def __init__(
        self,
        *,
        dls_damping: float = 0.05,
        qdot_limit_rad_s: float = 0.05,
        translation_twist_limit_m_s: float = 0.01,
        rotation_twist_limit_rad_s: float = 0.03,
        step5b_scaffold_sha256: str | None = None,
        step5b_activation_ready: bool = False,
        step5b_freshness_s: float = 0.01,
    ) -> None:
        if min(
            dls_damping,
            qdot_limit_rad_s,
            translation_twist_limit_m_s,
            rotation_twist_limit_rad_s,
        ) <= 0.0:
            raise ValueError("surrogate backend limits must be positive")
        self.dls_damping = dls_damping
        self.qdot_limit_rad_s = qdot_limit_rad_s
        self.twist_limits = (
            translation_twist_limit_m_s,
            translation_twist_limit_m_s,
            translation_twist_limit_m_s,
            rotation_twist_limit_rad_s,
            rotation_twist_limit_rad_s,
            rotation_twist_limit_rad_s,
        )
        if step5b_scaffold_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", step5b_scaffold_sha256
        ):
            raise ValueError("configured Step5b scaffold hash must be SHA-256")
        if not math.isfinite(step5b_freshness_s) or step5b_freshness_s <= 0.0:
            raise ValueError("Step5b freshness must be positive")
        self.step5b_scaffold_sha256 = step5b_scaffold_sha256
        self.step5b_activation_ready = bool(step5b_activation_ready)
        self.step5b_freshness_s = step5b_freshness_s

    def command(
        self,
        observation: ImpedanceObservation,
        proposal: ImpedanceProposal,
        *,
        step5b_input: Step5bTwistInput | None = None,
    ) -> BackendCommand:
        # A valid proposal is not necessarily command-capable.  In particular,
        # every DBIL proposal in this experiment is permanently shadow-only.
        # Keep this check at the backend boundary as a second line of defence
        # even when a capability-separated mux is used upstream.
        if proposal.shadow_only:
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "shadow_proposal_not_command_capable"),),
            )
        if not proposal.valid:
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "invalid_impedance_proposal"),),
            )
        if self.step5b_scaffold_sha256 is None:
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "step5b_scaffold_binding_not_configured"),),
            )
        if not self.step5b_activation_ready:
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "step5b_scaffold_activation_not_accepted"),),
            )
        if step5b_input is None:
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "step5b_scaffold_not_bound"),),
            )
        step5b_age_s = observation.timestamp_s - step5b_input.generated_at_s
        if (
            step5b_input.scaffold_sha256 != self.step5b_scaffold_sha256
            or step5b_input.sequence != observation.sequence
            or step5b_age_s < 0.0
            or step5b_age_s > self.step5b_freshness_s
        ):
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "step5b_scaffold_binding_or_freshness_mismatch"),),
            )
        if any(
            abs(step5b_input.twist_base[index]) > self.twist_limits[index]
            for index in range(6)
        ):
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "step5b_twist_out_of_bounds"),),
            )
        if observation.jacobian_base is None:
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "missing_calibrated_jacobian"),),
            )
        force = observation.wrench[:3]
        force_norm = math.sqrt(sum(value * value for value in force))
        if force_norm <= 1e-9:
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "missing_reaction_normal"),),
            )
        reaction_normal = tuple(value / force_norm for value in force)
        baseline_linear = step5b_input.twist_base[:3]
        normal_velocity = sum(
            baseline_linear[index] * reaction_normal[index] for index in range(3)
        )
        effective_k = sum(
            reaction_normal[index] ** 2 * proposal.stiffness[index]
            for index in range(3)
        )
        baseline_k = sum(
            reaction_normal[index] ** 2
            * BASELINE_TRANSLATIONAL_STIFFNESS[index]
            for index in range(3)
        )
        # This dimensionless <=1 modulation changes only the normal component;
        # the validated Step5b tangential and orientation commands stay intact.
        compliance_scale = math.sqrt(max(0.0, min(1.0, effective_k / baseline_k)))
        desired_twist = [
            baseline_linear[index]
            + (compliance_scale - 1.0)
            * normal_velocity
            * reaction_normal[index]
            for index in range(3)
        ]
        desired_twist.extend(step5b_input.twist_base[3:])
        qdot = damped_least_squares(
            observation.jacobian_base, desired_twist, self.dls_damping
        )
        bounded_qdot = tuple(
            max(-self.qdot_limit_rad_s, min(self.qdot_limit_rad_s, value))
            for value in qdot
        )
        if not all(math.isfinite(value) for value in bounded_qdot):
            return BackendCommand(
                backend="velocity_admittance_surrogate",
                backend_fidelity="surrogate",
                mode="stop",
                shadow_only=True,
                claim_boundary=SURROGATE_CLAIM_BOUNDARY,
                diagnostics=(("reason", "nonfinite_qdot"),),
            )
        return BackendCommand(
            backend="velocity_admittance_surrogate",
            backend_fidelity="surrogate",
            mode="command",
            qdot_rad_s=bounded_qdot,
            shadow_only=False,
            claim_boundary=SURROGATE_CLAIM_BOUNDARY,
            diagnostics=(
                ("controller_compatibility", "PolyScope_5.11_velocity_path"),
                ("control_source", "hash_bound_validated_Step5b_twist"),
                ("step5b_scaffold_sha256", step5b_input.scaffold_sha256),
                ("normal_modulation", "dimensionless_sqrt_K_ratio_le_1"),
                ("policy_source", proposal.source),
            ),
        )


@dataclass(frozen=True)
class VelocityMuxResult:
    """One capability-mux result, including the route used for audit."""

    mode: str
    qdot_rad_s: tuple[float, ...] | None
    route: str


@dataclass(frozen=True)
class ShadowCommandMuxResult:
    command: tuple[float, ...]
    command_valid: bool
    route: str


class CapabilitySeparatedShadowMux:
    """Generic evidence mux that cannot route a shadow proposal to output."""

    def select(
        self,
        proposal: ImpedanceProposal | None,
        baseline_command: Sequence[float],
        baseline_valid: bool,
    ) -> ShadowCommandMuxResult:
        command = tuple(float(value) for value in baseline_command)
        if len(command) != 6 or not all(math.isfinite(value) for value in command):
            raise ValueError("baseline command must contain six finite values")
        if proposal is None:
            return ShadowCommandMuxResult(
                command, bool(baseline_valid), "baseline_no_proposal"
            )
        if proposal.shadow_only:
            return ShadowCommandMuxResult(
                command, bool(baseline_valid), "baseline_shadow_bypass"
            )
        return ShadowCommandMuxResult((0.0,) * 6, False, "active_capability_denied")


class CapabilitySeparatedVelocityMux:
    """Keep shadow computation physically unable to modify the output path.

    The established baseline is the only output when no active proposal is
    selected or when a proposal is marked ``shadow_only``.  A non-shadow
    proposal requires an explicit capability at construction time; backend
    rejection then produces stop rather than silently returning to baseline.
    """

    def __init__(
        self,
        backend: VelocityAdmittanceSurrogate,
        *,
        active_capability: bool = False,
    ) -> None:
        self.backend = backend
        self.active_capability = bool(active_capability)

    @staticmethod
    def _baseline(values: Sequence[float]) -> tuple[float, ...]:
        result = tuple(float(value) for value in values)
        if len(result) != 6 or not all(math.isfinite(value) for value in result):
            raise ValueError("baseline qdot must contain six finite values")
        return result

    def select(
        self,
        observation: ImpedanceObservation,
        proposal: ImpedanceProposal | None,
        baseline_qdot_rad_s: Sequence[float],
        *,
        step5b_input: Step5bTwistInput | None = None,
    ) -> VelocityMuxResult:
        baseline = self._baseline(baseline_qdot_rad_s)
        if proposal is None:
            return VelocityMuxResult("command", baseline, "baseline_no_proposal")
        if proposal.shadow_only:
            return VelocityMuxResult("command", baseline, "baseline_shadow_bypass")
        if not self.active_capability:
            return VelocityMuxResult("stop", None, "active_capability_denied")
        command = self.backend.command(
            observation, proposal, step5b_input=step5b_input
        )
        if command.mode != "command":
            return VelocityMuxResult("stop", None, "backend_rejected")
        if command.shadow_only or command.qdot_rad_s is None:
            raise RuntimeError("backend violated the active-command capability contract")
        return VelocityMuxResult("command", command.qdot_rad_s, "active_backend")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_step5b_scaffold_binding(
    binding_path: Path, repository_root: Path
) -> dict[str, Any]:
    """Validate stable pure-core/replay/acceptance bindings, never the big bridge."""

    payload = json.loads(binding_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported Step5b scaffold binding schema")
    bindings = (
        ("source_path_from_repository_root", "source_sha256"),
        ("replay_validator_path_from_repository_root", "replay_validator_sha256"),
        ("acceptance_path_from_repository_root", "acceptance_sha256"),
    )
    for path_key, hash_key in bindings:
        relative = Path(str(payload.get(path_key, "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Step5b binding paths must be repository-relative")
        actual_path = repository_root / relative
        if not actual_path.is_file() or _sha256(actual_path) != payload.get(hash_key):
            raise ValueError(f"Step5b binding drifted: {path_key}")
    acceptance = json.loads(
        (repository_root / payload["acceptance_path_from_repository_root"]).read_text(
            encoding="utf-8"
        )
    )
    if bool(payload.get("activation_ready")) != bool(acceptance.get("accepted")):
        raise ValueError("Step5b activation readiness must mirror acceptance artifact")
    if payload.get("activation_ready"):
        raise ValueError(
            "this offline experiment may not self-promote Step5b live acceptance"
        )
    return payload


def load_direct_torque_bundle(layout_path: Path, template_path: Path) -> dict[str, Any]:
    """Validate that the offline template is exactly bound to its RTDE layout."""

    payload = json.loads(layout_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") not in {3, 4}:
        raise ValueError("unsupported direct-torque layout schema")
    if payload.get("default_mode") != "disabled" or payload.get("upload_authorized"):
        raise ValueError("offline direct-torque bundle must default to disabled/no-upload")
    if payload.get("controller_verified") or payload.get("model_active_authorized"):
        raise ValueError("offline Direct Torque V2 bundle must remain unverified/inactive")
    if payload.get("minimum_polyscope") != "5.25.2":
        raise ValueError("Direct Torque V2 layout must retain the 5.25.2 version gate")
    if payload.get("direct_torque_api") != "V2" or payload.get("control_loop_hz") != 500:
        raise ValueError("Direct Torque V2 layout must retain its 500 Hz design target")
    if payload.get("official_api_evidence") != OFFICIAL_API_EVIDENCE:
        raise ValueError("official PolyScope 5.25 API evidence binding drifted")
    expected_hash = payload.get("template_sha256")
    actual_hash = _sha256(template_path)
    if expected_hash != actual_hash:
        raise ValueError("URScript template hash does not match RTDE manifest")
    template = template_path.read_text(encoding="utf-8")
    required_offline_capability_tokens = (
        "local model_active_allowed = False",
        "local applied_feedforward = vic_zero_six()",
        "vic_safe_exit_tick(last_applied_feedforward)",
        "wrench_trans(tcp_rotation_base, wrench_tcp)",
        "# Intentionally no invocation.",
    )
    if any(token not in template for token in required_offline_capability_tokens):
        raise ValueError("URScript offline/model-active capability boundary drifted")

    registers = payload.get("registers", {})
    input_double = registers.get("input_double", {})
    flattened = [index for indices in input_double.values() for index in indices]
    if sorted(flattened) != list(range(24, 48)) or len(flattened) != len(set(flattened)):
        raise ValueError("input doubles must bind exactly registers 24..47 once")
    input_integer = registers.get("input_integer", {})
    if input_integer != {
        "mode": 24,
        "sequence": 25,
        "heartbeat": 26,
        "exclusive_lease": 27,
        "model_sequence": 28,
        "model_period_us": 29,
        "model_mode": 30,
        "wrench_frame_token": 31,
    }:
        raise ValueError("input integer packet/model/lease binding drifted")
    if registers.get("output_integer") != {
        "state": 24,
        "echo_sequence": 25,
        "fault_code": 26,
        "echo_exclusive_lease": 27,
        "echo_model_sequence": 28,
        "model_fault_code": 29,
        "echo_wrench_frame_token": 30,
    }:
        raise ValueError("output integer state/model/fault binding drifted")
    if registers.get("output_double") != {
        "max_abs_tau_nm": 24,
        "steptime_s": 25,
        "filtered_feedforward_wrench": [26, 27, 28, 29, 30, 31],
    }:
        raise ValueError("output double diagnostic binding drifted")
    for family in registers.values():
        values: list[int] = []
        if isinstance(family, dict):
            for value in family.values():
                values.extend(value if isinstance(value, list) else [value])
        if any(index < 24 or index > 47 for index in values):
            raise ValueError("RTDE registers must remain in the external 24..47 range")
        if len(values) != len(set(values)):
            raise ValueError("registers within one RTDE direction/type must be unique")
    if payload.get("heartbeat_timeout_ticks") != 10:
        raise ValueError("heartbeat timeout must remain 10 controller ticks (20 ms)")
    if payload.get("zero_torque_startup_ticks") != 5:
        raise ValueError("direct torque must retain five zero-wrench startup ticks")
    if payload.get("safe_exit_damping_ticks") != 10:
        raise ValueError("direct torque must retain ten monotonic ramp-down ticks")
    if not payload.get("torque_backend_exclusive"):
        raise ValueError("direct torque bundle requires an exclusive lease")
    if payload.get("sequence_policy") != (
        "coherent before/after read, heartbeat equals sequence, exact +1 advancement; "
        "frozen/gap/mixed packet fails closed"
    ):
        raise ValueError("controller packet sequence policy drifted")
    limits = payload.get("limits", {})
    if limits != DIRECT_TORQUE_MANIFEST_LIMITS:
        raise ValueError(
            "controller-side direct-torque limits drifted from the URScript/oracle contract"
        )
    if payload.get("model_modes") != {"disabled": 0, "shadow": 1, "active": 2}:
        raise ValueError("model mode contract drifted")
    if payload.get("wrench_frame_contract") != {
        "id": "kunwei_sensor_to_tcp_si_v1",
        "token": DIRECT_TORQUE_FRAME_TOKEN,
        "controller_verified": False,
    }:
        raise ValueError("wrench frame token contract drifted")
    if payload.get("model_sequence_policy") != (
        "stable before/after raw F_df read; same sequence may be held for at most "
        "two declared model periods; exact +1 commits a new model sample"
    ):
        raise ValueError("model sequence policy drifted")
    return payload


def torque_formula_reference(
    jacobian: Sequence[Sequence[float]],
    impedance_wrench: Sequence[float],
    coriolis: Sequence[float],
    joint_velocity: Sequence[float],
    joint_damping: Sequence[float],
    feedforward_wrench: Sequence[float] = (0.0,) * 6,
) -> tuple[float, ...]:
    """Oracle for J^T(Fff + K*e - D*xdot) + C - Dq*qdot."""

    if len(jacobian) != 6 or any(len(row) != 6 for row in jacobian):
        raise ValueError("jacobian must be 6x6")
    if not all(
        len(values) == 6
        for values in (
            impedance_wrench,
            coriolis,
            joint_velocity,
            joint_damping,
            feedforward_wrench,
        )
    ):
        raise ValueError("torque formula vectors must have six values")
    result = []
    for joint in range(6):
        value = float(coriolis[joint]) - float(joint_damping[joint]) * float(
            joint_velocity[joint]
        )
        value += sum(
            float(jacobian[axis][joint])
            * (float(feedforward_wrench[axis]) + float(impedance_wrench[axis]))
            for axis in range(6)
        )
        if not math.isfinite(value):
            raise ValueError("torque formula produced non-finite output")
        result.append(value)
    return tuple(result)
