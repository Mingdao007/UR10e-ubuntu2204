"""Allocation-bounded geometric tube guard for Step5d Stage25."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import math
from typing import Sequence

from .stage_adapters import ControllerProgress, ControllerProgressPhase


HARD_TUBE_RADIUS_M = 0.030
HARD_TUBE_PROGRESS_MAX_AGE_NS = 2_000_000
HARD_TUBE_BASE_RATE_HZ = 500
HARD_TUBE_EVALUATION_DIVISOR = 5
HARD_TUBE_EVALUATION_HZ = (
    HARD_TUBE_BASE_RATE_HZ / HARD_TUBE_EVALUATION_DIVISOR
)


class HardTubeReason(IntEnum):
    TUBE_INACTIVE = 0
    TUBE_STAGE_UNKNOWN = 1
    TUBE_INPUT_MISSING = 2
    TUBE_INPUT_NONFINITE = 3
    TUBE_REFERENCE_MISMATCH = 4
    TUBE_PROGRESS_STALE = 5
    TUBE_PROGRESS_NONSEQUENTIAL = 6
    TUBE_ACTUAL_BREACH = 7
    TUBE_OK = 8
    TUBE_BETWEEN_SAMPLES = 9


@dataclass(slots=True)
class HardTubeResult:
    stop: bool = True
    reason: HardTubeReason = HardTubeReason.TUBE_INPUT_MISSING
    actual_distance_m: float = math.nan
    remaining_margin_m: float = math.nan


class HardTubeGuard:
    """Fail-closed 30 mm sphere swept along the frozen Stage25 centerline."""

    __slots__ = (
        "reference_sha256",
        "radius_m",
        "evaluation_divisor",
        "evaluation_hz",
        "result",
        "last_controller_tick_seq",
        "last_controller_timestamp_ns",
        "last_evaluation_tick_seq",
    )

    def __init__(
        self,
        *,
        reference_sha256: str,
        radius_m: float = HARD_TUBE_RADIUS_M,
        evaluation_divisor: int = HARD_TUBE_EVALUATION_DIVISOR,
        result: HardTubeResult | None = None,
    ) -> None:
        if len(reference_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in reference_sha256
        ):
            raise ValueError("reference_sha256 must be a lowercase SHA256")
        if not math.isfinite(radius_m) or radius_m <= 0.0:
            raise ValueError("radius_m must be finite and positive")
        if (
            isinstance(evaluation_divisor, bool)
            or not isinstance(evaluation_divisor, int)
            or evaluation_divisor < 1
        ):
            raise ValueError("evaluation_divisor must be a positive integer")
        self.reference_sha256 = reference_sha256
        self.radius_m = float(radius_m)
        self.evaluation_divisor = evaluation_divisor
        self.evaluation_hz = HARD_TUBE_BASE_RATE_HZ / evaluation_divisor
        self.result = result if result is not None else HardTubeResult()
        self.last_controller_tick_seq = 0
        self.last_controller_timestamp_ns = 0
        self.last_evaluation_tick_seq = 0

    def reset(self) -> None:
        self.last_controller_tick_seq = 0
        self.last_controller_timestamp_ns = 0
        self.last_evaluation_tick_seq = 0
        self.result.stop = True
        self.result.reason = HardTubeReason.TUBE_INPUT_MISSING
        self.result.actual_distance_m = math.nan
        self.result.remaining_margin_m = math.nan

    def tick(
        self,
        *,
        progress: ControllerProgress | None,
        tcp_base: Sequence[float] | None,
    ) -> HardTubeResult:
        out = self.result
        out.stop = True
        if progress is None:
            out.actual_distance_m = math.nan
            out.remaining_margin_m = math.nan
            out.reason = HardTubeReason.TUBE_INPUT_MISSING
            return out
        if progress.phase is ControllerProgressPhase.UNKNOWN:
            out.actual_distance_m = math.nan
            out.remaining_margin_m = math.nan
            out.reason = HardTubeReason.TUBE_STAGE_UNKNOWN
            return out
        if progress.phase is ControllerProgressPhase.INACTIVE:
            out.actual_distance_m = math.nan
            out.remaining_margin_m = math.nan
            out.stop = False
            out.reason = HardTubeReason.TUBE_INACTIVE
            return out
        if progress.phase is not ControllerProgressPhase.ACTIVE_STAGE25:
            out.actual_distance_m = math.nan
            out.remaining_margin_m = math.nan
            out.reason = HardTubeReason.TUBE_STAGE_UNKNOWN
            return out
        if tcp_base is None or len(tcp_base) < 3:
            out.actual_distance_m = math.nan
            out.remaining_margin_m = math.nan
            out.reason = HardTubeReason.TUBE_INPUT_MISSING
            return out
        if progress.reference_sha256 != self.reference_sha256:
            out.actual_distance_m = math.nan
            out.remaining_margin_m = math.nan
            out.reason = HardTubeReason.TUBE_REFERENCE_MISMATCH
            return out
        if (
            progress.age_ns < 0
            or progress.age_ns > HARD_TUBE_PROGRESS_MAX_AGE_NS
        ):
            out.actual_distance_m = math.nan
            out.remaining_margin_m = math.nan
            out.reason = HardTubeReason.TUBE_PROGRESS_STALE
            return out
        if (
            not progress.monotonic
            or progress.controller_tick_seq <= 0
            or (
                self.last_controller_tick_seq != 0
                and progress.controller_tick_seq
                <= self.last_controller_tick_seq
            )
            or progress.controller_timestamp_ns
            <= self.last_controller_timestamp_ns
        ):
            out.actual_distance_m = math.nan
            out.remaining_margin_m = math.nan
            out.reason = HardTubeReason.TUBE_PROGRESS_NONSEQUENTIAL
            return out
        self.last_controller_tick_seq = progress.controller_tick_seq
        self.last_controller_timestamp_ns = progress.controller_timestamp_ns
        tcp_x, tcp_y, tcp_z = (float(tcp_base[index]) for index in range(3))
        center = (
            progress.center_x_m,
            progress.center_y_m,
            progress.center_z_m,
        )
        if not all(
            math.isfinite(value)
            for value in (tcp_x, tcp_y, tcp_z, *center)
        ):
            out.actual_distance_m = math.nan
            out.remaining_margin_m = math.nan
            out.reason = HardTubeReason.TUBE_INPUT_NONFINITE
            return out
        if (
            self.last_evaluation_tick_seq != 0
            and progress.controller_tick_seq - self.last_evaluation_tick_seq
            < self.evaluation_divisor
        ):
            out.stop = False
            out.reason = HardTubeReason.TUBE_BETWEEN_SAMPLES
            return out
        dx = tcp_x - center[0]
        dy = tcp_y - center[1]
        dz = tcp_z - center[2]
        distance = math.sqrt(dx * dx + dy * dy + dz * dz)
        self.last_evaluation_tick_seq = progress.controller_tick_seq
        out.actual_distance_m = distance
        out.remaining_margin_m = self.radius_m - distance
        if distance > self.radius_m:
            out.reason = HardTubeReason.TUBE_ACTUAL_BREACH
            return out
        out.stop = False
        out.reason = HardTubeReason.TUBE_OK
        return out


__all__ = [
    "HARD_TUBE_BASE_RATE_HZ",
    "HARD_TUBE_EVALUATION_DIVISOR",
    "HARD_TUBE_EVALUATION_HZ",
    "HARD_TUBE_PROGRESS_MAX_AGE_NS",
    "HARD_TUBE_RADIUS_M",
    "HardTubeGuard",
    "HardTubeReason",
    "HardTubeResult",
]
