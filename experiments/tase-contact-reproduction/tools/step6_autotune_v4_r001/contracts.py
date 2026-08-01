"""Locked, immutable Step6 Figure-Eight r001 offline contracts."""

from __future__ import annotations

import math
import re
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from .errors import ContractInvariantError, InvalidPathTimeError


SAFE_FRAME_SOURCE: Final[str] = "config/step6_eight_safe_frame.json"
SAFE_FRAME_SHA256: Final[str] = "25b03cb256c95a3a1f387168703314acc616e3539d2ba84de8d12f4bfe9dd232"
LINEAGE: Final[str] = "step6_strict_rnn_autotune_v4"
PROFILE: Final[str] = "autotune_v4.step6_figure8_paper"
REVISION: Final[str] = "r001"
PROGRAM_NAME: Final[str] = "step6_strict_rnn_autotune_v4_r001"
HUMAN_LABEL: Final[str] = "Autotune V4 / Step6 Figure-Eight (Paper 60s)"
HOME_CARTESIAN_POSE: Final[tuple[float, ...]] = (
    0.462055182,
    0.177882596,
    0.033000000,
    3.120752062,
    0.0,
    0.068626833,
)
THEORETICAL_PEAK_SPEED_M_S: Final[float] = math.hypot(0.004, 0.002)
FORCE_MAE_V2_VERSION: Final[str] = "force_mae_v2"
FORCE_MAE_V2_SPEC_ID: Final[str] = "force_mae_v2"
FORCE_MAE_V1_COMPAT_SHADOW_ID: Final[str] = "force_mae_v1_compat_shadow"
FORCE_MAE_V1_COMPAT_SHADOW_ROLE: Final[str] = "audit_only"
FORCE_MAE_V2_AGGREGATION_ORDER: Final[str] = "sample_abs_then_bin_mean_then_equal_bin_mean"
FORCE_MAE_V2_SAMPLE_ERROR_OPERATION: Final[str] = "abs(filtered_normal_n - target_force_n)"
FORCE_MAE_V2_WITHIN_BIN_REDUCTION: Final[str] = "arithmetic_mean"
FORCE_MAE_V2_ACROSS_BIN_REDUCTION: Final[str] = "equal_weight_arithmetic_mean"
FORCE_MAE_V1_COMPAT_SHADOW_AGGREGATION_ORDER: Final[str] = "bin_mean_then_abs_then_equal_bin_mean"
FORCE_MAE_V1_COMPAT_SHADOW_ERROR_OPERATION: Final[str] = "abs(bin_mean_filtered_normal_n - target_force_n)"


def require_finite_float(value: object, label: str) -> float:
    """Return a finite float without accepting bool or silently normalizing bad data."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractInvariantError(f"{label} must be a real number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ContractInvariantError(f"{label} must be finite, got {value!r}")
    return number


@dataclass(frozen=True, slots=True)
class ForceMaeSpec:
    """The locked force objective and compatibility-shadow evidence contract."""

    version: str = FORCE_MAE_V2_VERSION
    mae_spec_id: str = FORCE_MAE_V2_SPEC_ID
    target_force_n: float = 5.0
    window_start_s: float = 5.0
    window_end_s: float = 60.0
    bin_width_s: float = 0.1
    bin_count: int = 550
    requires_complete_coverage: bool = True
    requires_distinct_sample_identities: bool = True
    v1_shadow_id: str = FORCE_MAE_V1_COMPAT_SHADOW_ID
    v1_shadow_role: str = FORCE_MAE_V1_COMPAT_SHADOW_ROLE
    v2_aggregation_order: str = FORCE_MAE_V2_AGGREGATION_ORDER
    v2_sample_error_operation: str = FORCE_MAE_V2_SAMPLE_ERROR_OPERATION
    v2_within_bin_reduction: str = FORCE_MAE_V2_WITHIN_BIN_REDUCTION
    v2_across_bin_reduction: str = FORCE_MAE_V2_ACROSS_BIN_REDUCTION
    v1_shadow_aggregation_order: str = FORCE_MAE_V1_COMPAT_SHADOW_AGGREGATION_ORDER
    v1_shadow_error_operation: str = FORCE_MAE_V1_COMPAT_SHADOW_ERROR_OPERATION
    semantic_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if self.version != FORCE_MAE_V2_VERSION:
            raise ContractInvariantError("ForceMaeSpec.version must be force_mae_v2")
        if self.mae_spec_id != FORCE_MAE_V2_SPEC_ID:
            raise ContractInvariantError("ForceMaeSpec.mae_spec_id must be force_mae_v2")
        if self.v1_shadow_id != FORCE_MAE_V1_COMPAT_SHADOW_ID:
            raise ContractInvariantError("ForceMaeSpec.v1_shadow_id is not the locked compatibility shadow")
        if self.v1_shadow_role != FORCE_MAE_V1_COMPAT_SHADOW_ROLE:
            raise ContractInvariantError("ForceMaeSpec.v1_shadow_role must be audit_only")
        semantic_strings = {
            "v2_aggregation_order": FORCE_MAE_V2_AGGREGATION_ORDER,
            "v2_sample_error_operation": FORCE_MAE_V2_SAMPLE_ERROR_OPERATION,
            "v2_within_bin_reduction": FORCE_MAE_V2_WITHIN_BIN_REDUCTION,
            "v2_across_bin_reduction": FORCE_MAE_V2_ACROSS_BIN_REDUCTION,
            "v1_shadow_aggregation_order": FORCE_MAE_V1_COMPAT_SHADOW_AGGREGATION_ORDER,
            "v1_shadow_error_operation": FORCE_MAE_V1_COMPAT_SHADOW_ERROR_OPERATION,
        }
        for field_name, expected in semantic_strings.items():
            if getattr(self, field_name) != expected:
                raise ContractInvariantError(
                    f"ForceMaeSpec semantic drift in {field_name}: expected {expected!r}"
                )
        if self.requires_complete_coverage is not True:
            raise ContractInvariantError("ForceMaeSpec requires complete formal coverage")
        if self.requires_distinct_sample_identities is not True:
            raise ContractInvariantError("ForceMaeSpec requires distinct sample identities")
        for label in (
            "target_force_n",
            "window_start_s",
            "window_end_s",
            "bin_width_s",
        ):
            require_finite_float(getattr(self, label), label)
        if self.target_force_n != 5.0:
            raise ContractInvariantError("ForceMaeSpec target must be 5.0 N")
        if self.window_start_s != 5.0 or self.window_end_s != 60.0:
            raise ContractInvariantError("ForceMaeSpec window must be [5.0, 60.0)")
        if self.bin_width_s != 0.1 or self.bin_count != 550:
            raise ContractInvariantError("ForceMaeSpec must use 550 bins of width 0.1 s")
        if isinstance(self.bin_count, bool) or not isinstance(self.bin_count, int):
            raise ContractInvariantError("ForceMaeSpec.bin_count must be an integer")
        object.__setattr__(self, "semantic_fingerprint", _derive_force_mae_fingerprint(self))

    @property
    def target_n(self) -> float:
        """Short typed spelling for the locked normal-force target."""

        return self.target_force_n

    @property
    def requires_distinct_identities(self) -> bool:
        return self.requires_distinct_sample_identities

    @property
    def spec_fingerprint(self) -> str:
        """Stable alias for the semantic fingerprint used by composition seams."""

        return self.semantic_fingerprint


def _canonical_number(value: float | int) -> str:
    """Render contract numbers without locale, repr, or binary hash behavior."""

    decimal = Decimal(str(value)).normalize()
    rendered = format(decimal, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _derive_force_mae_fingerprint(spec: ForceMaeSpec) -> str:
    """Hash an explicitly ordered, versioned ASCII semantic payload."""

    canonical_lines = (
        f"version={spec.version}",
        f"mae_spec_id={spec.mae_spec_id}",
        f"target_force_n={_canonical_number(spec.target_force_n)}",
        f"window_start_s={_canonical_number(spec.window_start_s)}",
        f"window_end_s={_canonical_number(spec.window_end_s)}",
        "window_interval=half_open",
        f"bin_width_s={_canonical_number(spec.bin_width_s)}",
        f"bin_count={spec.bin_count}",
        f"requires_complete_coverage={str(spec.requires_complete_coverage).lower()}",
        f"requires_distinct_sample_identities={str(spec.requires_distinct_sample_identities).lower()}",
        f"v2_aggregation_order={spec.v2_aggregation_order}",
        f"v2_sample_error_operation={spec.v2_sample_error_operation}",
        f"v2_within_bin_reduction={spec.v2_within_bin_reduction}",
        f"v2_across_bin_reduction={spec.v2_across_bin_reduction}",
        f"v1_shadow_id={spec.v1_shadow_id}",
        f"v1_shadow_role={spec.v1_shadow_role}",
        f"v1_shadow_aggregation_order={spec.v1_shadow_aggregation_order}",
        f"v1_shadow_error_operation={spec.v1_shadow_error_operation}",
    )
    canonical_payload = ("\n".join(canonical_lines) + "\n").encode("ascii")
    return hashlib.sha256(canonical_payload).hexdigest()


FORCE_MAE_V2_SPEC: Final[ForceMaeSpec] = ForceMaeSpec()
FORCE_MAE_V2_SPEC_FINGERPRINT: Final[str] = FORCE_MAE_V2_SPEC.semantic_fingerprint


@dataclass(frozen=True, slots=True)
class AutotuneTaskProfile:
    """The complete identity and paper-path contract for this offline scope."""

    lineage: str = LINEAGE
    profile: str = PROFILE
    revision: str = REVISION
    program_name: str = PROGRAM_NAME
    human_label: str = HUMAN_LABEL
    home_cartesian_pose: tuple[float, ...] = HOME_CARTESIAN_POSE
    # Home q is deliberately unavailable in this preparation scope.
    home_q: None = None
    safe_frame_source: str = SAFE_FRAME_SOURCE
    safe_frame_sha256: str = SAFE_FRAME_SHA256
    program_z_delta_m: float = 0.0
    duration_s: float = 60.0
    force_target_n: float = 5.0
    along_amplitude_m: float = 0.04
    along_frequency_rad_s: float = 0.1
    lateral_amplitude_m: float = 0.01
    lateral_frequency_rad_s: float = 0.2
    reference_speed_guard_m_s: float = 0.0045
    tangential_command_cap_m_s: float = 0.010
    theoretical_peak_speed_m_s: float = THEORETICAL_PEAK_SPEED_M_S
    force_mae_spec: ForceMaeSpec = FORCE_MAE_V2_SPEC
    # Derived from the sole force_mae_spec source; callers cannot inject a second id.
    mae_spec_id: str = field(init=False)
    semantic_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        exact_strings = {
            "lineage": LINEAGE,
            "profile": PROFILE,
            "revision": REVISION,
            "program_name": PROGRAM_NAME,
            "human_label": HUMAN_LABEL,
            "safe_frame_source": SAFE_FRAME_SOURCE,
        }
        for field_name, expected in exact_strings.items():
            if getattr(self, field_name) != expected:
                raise ContractInvariantError(
                    f"locked {field_name} mismatch: expected {expected!r}, "
                    f"got {getattr(self, field_name)!r}"
                )

        if self.home_q is not None:
            raise ContractInvariantError("home_q must remain unavailable in offline preparation")
        if not isinstance(self.home_cartesian_pose, tuple) or len(self.home_cartesian_pose) != 6:
            raise ContractInvariantError("home_cartesian_pose must be an immutable six-value tuple")
        pose = tuple(require_finite_float(value, f"home_cartesian_pose[{index}]") for index, value in enumerate(self.home_cartesian_pose))
        if pose != HOME_CARTESIAN_POSE:
            raise ContractInvariantError(f"locked Home Cartesian pose mismatch: {pose!r}")

        if not isinstance(self.safe_frame_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.safe_frame_sha256) is None:
            raise ContractInvariantError("safe_frame_sha256 must be a lowercase 64-character hex digest")
        if self.safe_frame_sha256 != SAFE_FRAME_SHA256:
            raise ContractInvariantError("safe_frame_sha256 does not match the inherited source contract")
        if self.force_mae_spec != FORCE_MAE_V2_SPEC:
            raise ContractInvariantError("AutotuneTaskProfile is locked to ForceMaeSpec force_mae_v2")
        object.__setattr__(self, "mae_spec_id", self.force_mae_spec.mae_spec_id)
        object.__setattr__(self, "semantic_fingerprint", self.force_mae_spec.semantic_fingerprint)

        finite_fields = (
            "program_z_delta_m",
            "duration_s",
            "force_target_n",
            "along_amplitude_m",
            "along_frequency_rad_s",
            "lateral_amplitude_m",
            "lateral_frequency_rad_s",
            "reference_speed_guard_m_s",
            "tangential_command_cap_m_s",
            "theoretical_peak_speed_m_s",
        )
        for field_name in finite_fields:
            require_finite_float(getattr(self, field_name), field_name)

        locked_floats = {
            "program_z_delta_m": 0.0,
            "duration_s": 60.0,
            "force_target_n": 5.0,
            "along_amplitude_m": 0.04,
            "along_frequency_rad_s": 0.1,
            "lateral_amplitude_m": 0.01,
            "lateral_frequency_rad_s": 0.2,
            "reference_speed_guard_m_s": 0.0045,
            "tangential_command_cap_m_s": 0.010,
        }
        for field_name, expected in locked_floats.items():
            if getattr(self, field_name) != expected:
                raise ContractInvariantError(
                    f"locked {field_name} mismatch: expected {expected!r}, "
                    f"got {getattr(self, field_name)!r}"
                )
        if not math.isclose(
            self.theoretical_peak_speed_m_s,
            THEORETICAL_PEAK_SPEED_M_S,
            rel_tol=0.0,
            abs_tol=1e-18,
        ):
            raise ContractInvariantError("theoretical peak speed does not match the locked derivatives")
        if self.theoretical_peak_speed_m_s > self.reference_speed_guard_m_s:
            raise ContractInvariantError("theoretical peak speed exceeds the reference speed guard")
        if self.reference_speed_guard_m_s > self.tangential_command_cap_m_s:
            raise ContractInvariantError("reference speed guard exceeds the tangential command cap")
        if self.force_target_n != self.force_mae_spec.target_force_n:
            raise ContractInvariantError("profile force target must be supplied by the locked MAE spec")

    @property
    def spec_fingerprint(self) -> str:
        return self.semantic_fingerprint

    @property
    def mae_spec_fingerprint(self) -> str:
        return self.semantic_fingerprint

# This is a strict public compatibility alias: construction, dataclass typing,
# frozen invariants, and the single locked instance are all the same object.
Step6R001Contract = AutotuneTaskProfile


STEP6_R001_PROFILE: Final[AutotuneTaskProfile] = AutotuneTaskProfile()
# Compatibility aliases deliberately point at the same immutable profile.
STEP6_R001_CONTRACT: Final[AutotuneTaskProfile] = STEP6_R001_PROFILE
STEP6_IDENTITY: Final[AutotuneTaskProfile] = STEP6_R001_PROFILE


def validate_path_time(path_time_s: object) -> float:
    """Validate a Step6 program time in the closed [0, 60] interval."""

    try:
        value = require_finite_float(path_time_s, "path_time_s")
    except ContractInvariantError as exc:
        raise InvalidPathTimeError(str(exc)) from exc
    if value < 0.0 or value > STEP6_R001_PROFILE.duration_s:
        raise InvalidPathTimeError(
            f"path_time_s must be in [0.0, {STEP6_R001_PROFILE.duration_s}], got {value!r}"
        )
    return value
