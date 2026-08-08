"""Hash-bound r006 active envelope over the frozen V3/r034 execution profile."""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from step5d_autotune_v4_r004.contracts import V3_R034_SAFETY_ENVELOPE
from step5d_autotune_v4_r004.motion_profile import R004_MOTION_PROFILE, V4MotionProfile
from step5d_autotune_v4_r004.path_reference import (
    PATH_AMPLITUDE_M,
    PATH_DURATION_S,
    PATH_OMEGA_RAD_S,
    PATH_STAGE_ID,
    path_reference_binding,
    step5_path_reference,
)

from .contracts import ROOT, sha256_bytes


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _finite_positive(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{role} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{role} must be finite and positive")
    return number


def _parent_profile_payload() -> Mapping[str, Any]:
    return {
        "v3_control_profile_id": R004_MOTION_PROFILE.v3_control_profile_id,
        "execution_profile_id": R004_MOTION_PROFILE.execution_profile_id,
        "xy_path_speed_m_s": R004_MOTION_PROFILE.xy_path_speed_m_s,
        "total_linear_cap_m_s": R004_MOTION_PROFILE.total_linear_cap_m_s,
        "normal_linear_cap_m_s": R004_MOTION_PROFILE.normal_linear_cap_m_s,
        "angular_cap_rad_s": R004_MOTION_PROFILE.angular_cap_rad_s,
        "qdot_cap_rad_s": R004_MOTION_PROFILE.qdot_cap_rad_s,
        "host_slew_rad_s2": R004_MOTION_PROFILE.host_slew_rad_s2,
        "tp_acceleration_rad_s2": R004_MOTION_PROFILE.tp_acceleration_rad_s2,
        "normal_update_rate_rad_s": R004_MOTION_PROFILE.normal_update_rate_rad_s,
        "cadence_hz": R004_MOTION_PROFILE.cadence_hz,
    }


FROZEN_V3_R034_PROFILE_SHA256 = sha256_bytes(_canonical(_parent_profile_payload()))
R005_CONTRACT_PATH = ROOT / "config/step5d/autotune_v4_r005.json"
FROZEN_R005_CONTRACT_SHA256 = sha256_bytes(R005_CONTRACT_PATH.read_bytes())


@dataclass(frozen=True)
class ActiveMotionEnvelopeV2:
    """V4-only active PATH caps; the V3 ExecutionProfile remains untouched."""

    schema: str
    version: str
    source: str
    parent_execution_profile_id: str
    frozen_v3_profile_sha256: str
    parent_r005_contract_sha256: str
    xy_path_speed_m_s: float
    total_linear_cap_m_s: float
    normal_linear_cap_m_s: float
    angular_cap_rad_s: float
    qdot_cap_rad_s: float
    host_qdot_slew_rad_s2: float
    tp_speedj_acceleration_rad_s2: float
    normal_update_rate_rad_s: float
    cadence_hz: float = 500.0
    path_omega_rad_s: float = PATH_OMEGA_RAD_S
    path_duration_s: float = PATH_DURATION_S
    path_amplitude_m: float = PATH_AMPLITUDE_M
    active_lease_s: float = 0.080

    def __post_init__(self) -> None:
        if self.schema != "step5d.autotune-v4/r006-active-motion-envelope-v2":
            raise ValueError("r006 active envelope schema differs")
        if self.version != "v3-r034-parent-v2-doubled-active-path":
            raise ValueError("r006 active envelope version differs")
        if self.source != "frozen_v3_r034_r005_profile_adapter":
            raise ValueError("r006 active envelope source differs")
        if self.parent_execution_profile_id != R004_MOTION_PROFILE.execution_profile_id:
            raise ValueError("r006 active envelope parent profile differs")
        if self.frozen_v3_profile_sha256 != FROZEN_V3_R034_PROFILE_SHA256:
            raise ValueError("r006 frozen V3 profile binding differs")
        expected = {
            "xy_path_speed_m_s": 0.008,
            "total_linear_cap_m_s": 2.0,
            "normal_linear_cap_m_s": 2.0,
            "angular_cap_rad_s": 0.5,
            "qdot_cap_rad_s": 5.0,
            "host_qdot_slew_rad_s2": 5.0,
            "tp_speedj_acceleration_rad_s2": 40.0,
            "normal_update_rate_rad_s": 200.0,
            "cadence_hz": 500.0,
            "path_omega_rad_s": 0.1,
            "path_duration_s": 60.0,
            "path_amplitude_m": 0.015,
            "active_lease_s": 0.080,
        }
        for name, wanted in expected.items():
            actual = _finite_positive(getattr(self, name), name)
            if not math.isclose(actual, wanted, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(f"r006 active envelope {name} differs")
            object.__setattr__(self, name, actual)

    @classmethod
    def from_frozen_v3_r005(cls) -> "ActiveMotionEnvelopeV2":
        return cls(
            schema="step5d.autotune-v4/r006-active-motion-envelope-v2",
            version="v3-r034-parent-v2-doubled-active-path",
            source="frozen_v3_r034_r005_profile_adapter",
            parent_execution_profile_id=R004_MOTION_PROFILE.execution_profile_id,
            frozen_v3_profile_sha256=FROZEN_V3_R034_PROFILE_SHA256,
            parent_r005_contract_sha256=FROZEN_R005_CONTRACT_SHA256,
            xy_path_speed_m_s=R004_MOTION_PROFILE.xy_path_speed_m_s * 2.0,
            total_linear_cap_m_s=R004_MOTION_PROFILE.total_linear_cap_m_s * 2.0,
            normal_linear_cap_m_s=R004_MOTION_PROFILE.normal_linear_cap_m_s * 2.0,
            angular_cap_rad_s=R004_MOTION_PROFILE.angular_cap_rad_s * 2.0,
            qdot_cap_rad_s=R004_MOTION_PROFILE.qdot_cap_rad_s * 2.0,
            host_qdot_slew_rad_s2=R004_MOTION_PROFILE.host_slew_rad_s2 * 2.0,
            tp_speedj_acceleration_rad_s2=R004_MOTION_PROFILE.tp_acceleration_rad_s2 * 2.0,
            normal_update_rate_rad_s=R004_MOTION_PROFILE.normal_update_rate_rad_s * 2.0,
        )

    @property
    def profile_id(self) -> str:
        return "r006-active-motion-envelope-v2"

    @property
    def profile_sha256(self) -> str:
        return sha256_bytes(_canonical(self.as_dict))

    @property
    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "source": self.source,
            "parent_execution_profile_id": self.parent_execution_profile_id,
            "frozen_v3_profile_sha256": self.frozen_v3_profile_sha256,
            "parent_r005_contract_sha256": self.parent_r005_contract_sha256,
            "active_path_caps": {
                "xy_tangential_m_s": self.xy_path_speed_m_s,
                "total_linear_m_s": self.total_linear_cap_m_s,
                "normal_linear_m_s": self.normal_linear_cap_m_s,
                "angular_rad_s": self.angular_cap_rad_s,
                "qdot_rad_s": self.qdot_cap_rad_s,
                "host_qdot_slew_rad_s2": self.host_qdot_slew_rad_s2,
                "tp_speedj_accel_rad_s2": self.tp_speedj_acceleration_rad_s2,
                "normal_vector_update_rad_s": self.normal_update_rate_rad_s,
            },
            "unchanged": {
                "path_omega_rad_s": self.path_omega_rad_s,
                "path_duration_s": self.path_duration_s,
                "path_amplitude_m": self.path_amplitude_m,
                "contact_search_speed_m_s": 0.0002,
                "contact_search_acceleration_m_s2": 0.005,
                "active_lease_s": self.active_lease_s,
                "cadence_hz": self.cadence_hz,
                "minimum_rate_hz": 460.0,
                "force_hard_guard_abs_normal_n": 60.0,
                "force_hard_guard_norm_n": 100.0,
                "torque_hard_guard_norm_nm": 3.0,
                "home_rise_descent_speed_m_s": 0.04,
                "home_rise_descent_acceleration_m_s2": 0.06,
                "home_xy_transfer_speed_m_s": 0.09,
                "home_xy_transfer_acceleration_m_s2": 0.135,
            },
            "path_reference_binding": dict(path_reference_binding()),
        }

    @property
    def execution_profile(self):
        """Return the exact frozen V3 object; never a doubled copy."""

        return R004_MOTION_PROFILE.execution_profile

    @property
    def mature_profile(self) -> V4MotionProfile:
        """Typed V4 view consumed by the calibrated r004 writer seam.

        The underlying V3 ``ExecutionProfile`` is the same immutable object;
        only the active PATH envelope is widened.  Home/contact/guard
        primitives continue to read their frozen parent values.
        """

        # ``V4MotionProfile.__post_init__`` is correctly an r004 invariant: it
        # accepts only the frozen r004 numbers.  Reusing that constructor as an
        # r006 gate would make a versioned cap adapter impossible.  Clone the
        # already validated typed object, then replace only the fields that the
        # independently validated r006 envelope owns.  The shared execution
        # profile and every Home/contact/geometry field remain identical.
        profile = copy.copy(R004_MOTION_PROFILE)
        replacements = {
            "xy_path_speed_m_s": self.xy_path_speed_m_s,
            "total_linear_cap_m_s": self.total_linear_cap_m_s,
            "normal_linear_cap_m_s": self.normal_linear_cap_m_s,
            "angular_cap_rad_s": self.angular_cap_rad_s,
            "qdot_cap_rad_s": self.qdot_cap_rad_s,
            "host_slew_rad_s2": self.host_qdot_slew_rad_s2,
            "tp_acceleration_rad_s2": self.tp_speedj_acceleration_rad_s2,
            "normal_update_rate_rad_s": self.normal_update_rate_rad_s,
            "cadence_hz": self.cadence_hz,
        }
        for name, value in replacements.items():
            object.__setattr__(profile, name, float(value))
        if not isinstance(profile, V4MotionProfile) or profile.execution_profile is not self.execution_profile:
            raise ValueError("r006 mature profile lost its typed frozen V3 parent")
        return profile


ACTIVE_MOTION_ENVELOPE_V2 = ActiveMotionEnvelopeV2.from_frozen_v3_r005()
PATH_SNAPSHOT_BINDING = dict(path_reference_binding())


def r006_path_reference(origin_xy_m: tuple[float, float], elapsed_s: float) -> Mapping[str, Any]:
    """Consume the reviewed V3 path snapshot through the r006 hash-bound seam."""

    value = step5_path_reference(PATH_STAGE_ID, origin_xy_m, elapsed_s)
    if value.get("stage_id") != PATH_STAGE_ID:
        raise ValueError("r006 path snapshot stage identity differs")
    return value


def r006_runtime_path_reference(
    stage_id: str,
    origin_xy_m: tuple[float, float],
    elapsed_s: float,
) -> Mapping[str, Any]:
    """Typed callable consumed by the mature calibrated runtime."""

    if stage_id != PATH_STAGE_ID:
        raise ValueError("r006 runtime path stage differs from the bound snapshot")
    if dict(path_reference_binding()) != PATH_SNAPSHOT_BINDING:
        raise ValueError("r006 runtime path snapshot changed after admission")
    return r006_path_reference(origin_xy_m, elapsed_s)


__all__ = [
    "ACTIVE_MOTION_ENVELOPE_V2",
    "ActiveMotionEnvelopeV2",
    "FROZEN_R005_CONTRACT_SHA256",
    "FROZEN_V3_R034_PROFILE_SHA256",
    "PATH_SNAPSHOT_BINDING",
    "V3_R034_SAFETY_ENVELOPE",
    "r006_path_reference",
    "r006_runtime_path_reference",
]
