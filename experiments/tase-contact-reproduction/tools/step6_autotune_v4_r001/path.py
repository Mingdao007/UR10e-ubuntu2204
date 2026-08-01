"""Standalone analytical Step6 Figure-Eight r001 path reference."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .contracts import (
    STEP6_R001_PROFILE,
    AutotuneTaskProfile,
    require_finite_float,
    validate_path_time,
)
from .errors import ContractInvariantError
from .frame import DEFAULT_SAFE_FRAME_PATH, InheritedXYFrame, load_inherited_xy_frame


def _xy_pair(value: object, label: str) -> tuple[float, float]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise ContractInvariantError(f"{label} must be an immutable two-value tuple")
    return tuple(require_finite_float(item, f"{label}[{index}]") for index, item in enumerate(value))  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class Step6PathSample:
    """One immutable analytical sample in both local and base XY coordinates."""

    path_time_s: float
    phase_rad: float
    along_m: float
    lateral_m: float
    along_derivative_m_s: float
    lateral_derivative_m_s: float
    reference_speed_m_s: float
    desired_base_xy_m: tuple[float, float]
    analytical_base_xy_velocity_m_s: tuple[float, float]

    def __post_init__(self) -> None:
        validate_path_time(self.path_time_s)
        for label in (
            "phase_rad",
            "along_m",
            "lateral_m",
            "along_derivative_m_s",
            "lateral_derivative_m_s",
            "reference_speed_m_s",
        ):
            require_finite_float(getattr(self, label), label)
        if self.reference_speed_m_s < 0.0:
            raise ContractInvariantError("reference_speed_m_s must be non-negative")
        expected_local_speed = math.hypot(self.along_derivative_m_s, self.lateral_derivative_m_s)
        if not math.isclose(
            self.reference_speed_m_s,
            expected_local_speed,
            rel_tol=0.0,
            abs_tol=1e-15,
        ):
            raise ContractInvariantError(
                "reference_speed_m_s must equal the local analytical derivative magnitude"
            )
        _xy_pair(self.desired_base_xy_m, "desired_base_xy_m")
        base_velocity = _xy_pair(
            self.analytical_base_xy_velocity_m_s,
            "analytical_base_xy_velocity_m_s",
        )
        if not math.isclose(
            math.hypot(*base_velocity),
            self.reference_speed_m_s,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ContractInvariantError(
                "orthonormal XY basis transform must preserve reference-speed magnitude"
            )

    @property
    def base_xy_m(self) -> tuple[float, float]:
        """Compatibility spelling for the desired base XY output."""

        return self.desired_base_xy_m

    @property
    def base_velocity_xy_m_s(self) -> tuple[float, float]:
        """Compatibility spelling for the analytical base XY velocity."""

        return self.analytical_base_xy_velocity_m_s

    @property
    def local_along_m(self) -> float:
        return self.along_m

    @property
    def local_lateral_m(self) -> float:
        return self.lateral_m

    @property
    def local_along_derivative_m_s(self) -> float:
        return self.along_derivative_m_s

    @property
    def local_lateral_derivative_m_s(self) -> float:
        return self.lateral_derivative_m_s


@dataclass(frozen=True, slots=True)
class PathReference:
    """Immutable analytical path bound to the inherited r001 XY frame."""

    frame: InheritedXYFrame
    contract: AutotuneTaskProfile = STEP6_R001_PROFILE

    def __post_init__(self) -> None:
        if not isinstance(self.frame, InheritedXYFrame):
            raise ContractInvariantError("PathReference.frame must be an InheritedXYFrame")
        if self.contract != STEP6_R001_PROFILE:
            raise ContractInvariantError("PathReference is locked to the Step6 r001 contract")
        if self.frame.source_sha256 != self.contract.safe_frame_sha256:
            raise ContractInvariantError(
                "PathReference frame SHA-256 does not match the inherited Step6 r001 source"
            )

    @classmethod
    def from_inherited_source(cls, path: Path = DEFAULT_SAFE_FRAME_PATH) -> "PathReference":
        return cls(load_inherited_xy_frame(path))

    def evaluate(self, path_time_s: object) -> Step6PathSample:
        """Evaluate without clamping or wrapping; valid endpoints are inclusive."""

        t = validate_path_time(path_time_s)
        phase = self.contract.along_frequency_rad_s * t
        along = self.contract.along_amplitude_m * math.sin(phase)
        lateral = self.contract.lateral_amplitude_m * math.sin(self.contract.lateral_frequency_rad_s * t)
        along_derivative = self.contract.along_amplitude_m * self.contract.along_frequency_rad_s * math.cos(phase)
        lateral_derivative = (
            self.contract.lateral_amplitude_m
            * self.contract.lateral_frequency_rad_s
            * math.cos(self.contract.lateral_frequency_rad_s * t)
        )
        reference_speed = math.hypot(along_derivative, lateral_derivative)
        base_xy = (
            self.frame.origin_xy_m[0]
            + along * self.frame.u_along_xy[0]
            + lateral * self.frame.p_lateral_xy[0],
            self.frame.origin_xy_m[1]
            + along * self.frame.u_along_xy[1]
            + lateral * self.frame.p_lateral_xy[1],
        )
        base_velocity_xy = (
            along_derivative * self.frame.u_along_xy[0]
            + lateral_derivative * self.frame.p_lateral_xy[0],
            along_derivative * self.frame.u_along_xy[1]
            + lateral_derivative * self.frame.p_lateral_xy[1],
        )
        return Step6PathSample(
            path_time_s=t,
            phase_rad=phase,
            along_m=along,
            lateral_m=lateral,
            along_derivative_m_s=along_derivative,
            lateral_derivative_m_s=lateral_derivative,
            reference_speed_m_s=reference_speed,
            desired_base_xy_m=base_xy,
            analytical_base_xy_velocity_m_s=base_velocity_xy,
        )

    @property
    def profile(self) -> AutotuneTaskProfile:
        """Explicit typed profile injection seam, retained beside ``contract``."""

        return self.contract

    @property
    def theoretical_peak_speed_m_s(self) -> float:
        return self.contract.theoretical_peak_speed_m_s

    @property
    def reference_speed_guard_m_s(self) -> float:
        return self.contract.reference_speed_guard_m_s

    @property
    def tangential_command_cap_m_s(self) -> float:
        return self.contract.tangential_command_cap_m_s

    @property
    def program_z_delta_m(self) -> float:
        return self.contract.program_z_delta_m


STEP6_R001_PATH_REFERENCE_FACTORY: Final[str] = "PathReference.from_inherited_source"
