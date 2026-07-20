#!/usr/bin/env python3
"""Deterministic Step5d Direct Torque command core.

This module is deliberately free of network, Dashboard, RTDE, serial, upload,
and motion side effects.  The 50 Hz TacDiffusion fixture is diagnostic-only;
no fixture value is reachable from the serialized controller command.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import struct
import sys
from typing import Callable, Sequence


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
)


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
    )
    doubles = (
        *packet.equilibrium_pose,
        *packet.stiffness,
        *packet.damping,
        *packet.raw_feedforward_wrench,
    )
    return struct.pack("!8i24d", *integers, *doubles)


class Step5dDirectTorqueCore:
    def __init__(self, *, lease_id: int, shadow: FixtureShadowRunner) -> None:
        if lease_id <= 0:
            raise ValueError("lease_id must be positive")
        self.lease_id = lease_id
        self.shadow = shadow
        self._last_tick = -1
        self._equilibrium_pose: tuple[float, ...] | None = None
        self._outer_state = Step5dOuterLoopState()

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
        reference = step5_path_reference(
            STAGE_ID,
            (sample.tcp_pose_base[0], sample.tcp_pose_base[1]),
            sample.elapsed_s,
        )
        outer = compute_step5d_outer_loop(
            OUTER_CONFIG,
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
                control_reaction_normal_base=sample.control_reaction_normal_base,
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
        packet = DirectTorquePacket(
            sequence_before=sequence,
            sequence_after=sequence,
            heartbeat=sequence,
            lease_id=self.lease_id,
            mode=1,
            equilibrium_pose=equilibrium,
            stiffness=FIXED_STIFFNESS,
            damping=FIXED_DAMPING,
            raw_feedforward_wrench=(0.0,) * 6,
            model_sequence_before=model_sequence,
            model_sequence_after=model_sequence,
            model_period_us=20_000,
            model_mode=1,
            wrench_frame_token=DIRECT_TORQUE_FRAME_TOKEN,
        )
        diagnostic = self.shadow.tick(sample)
        result = CommandResult(
            packet=packet,
            command_bytes=encode_controller_packet(packet),
            equilibrium_pose=equilibrium,
            outer_state=outer.next_state,
            shadow=diagnostic,
        )
        self._last_tick = sample.tick
        self._equilibrium_pose = equilibrium
        self._outer_state = outer.next_state
        return result
