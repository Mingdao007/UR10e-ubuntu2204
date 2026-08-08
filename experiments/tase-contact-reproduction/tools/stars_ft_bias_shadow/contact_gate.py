"""Fail-closed contact/static gate for the offline STARS FT bias shadow.

This module deliberately has no live-control dependency.  A caller must pass
the values derived from a validated trace row; missing evidence is never
converted to a safe-looking zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Any, Mapping, Sequence


BIAS_VECTOR_NAMES = ("fx_n", "fy_n", "fz_n", "mx_nm", "my_nm", "mz_nm")
BIAS_ESTIMATE_FIELDS = tuple(f"bias_est_{name}" for name in BIAS_VECTOR_NAMES)
BIAS_RATE_ESTIMATE_FIELDS = tuple(
    f"bias_rate_est_{name}_per_s" for name in BIAS_VECTOR_NAMES
)
LOAD_FIELDS = ("force_norm_n", "normal_load_n", "torque_norm_nm")


class GateMode(str, Enum):
    UPDATE = "UPDATE"
    FREEZE = "FREEZE"
    PREDICT_ONLY = "PREDICT_ONLY"


@dataclass(frozen=True)
class GateDecision:
    """Typed gate result with a stable reason and validity signal."""

    mode: GateMode
    bias_estimation_contact_mask: bool
    bias_contact_reason: str
    update_allowed: bool
    input_valid: bool = False
    invalid_fields: tuple[str, ...] = ()

    @property
    def reason_code(self) -> str:
        """Stable machine-readable alias retained beside the legacy name."""

        return self.bias_contact_reason


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _exact_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _valid_wrench6(value: Any) -> tuple[float, float, float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 6:
        return None
    converted: list[float] = []
    for item in value:
        number = _finite_float(item)
        if number is None:
            return None
        converted.append(number)
    return tuple(converted)  # type: ignore[return-value]


def _valid_speed(value: Any) -> float | None:
    number = _finite_float(value)
    if number is None or number < 0.0:
        return None
    return number


def _valid_pose_if_present(sample: Mapping[str, Any]) -> bool:
    """Require the exact finite pose used to derive the speed evidence."""

    if "tcp_pose_m_rad" not in sample:
        return False
    pose = sample.get("tcp_pose_m_rad")
    if not isinstance(pose, (list, tuple)) or len(pose) != 6:
        return False
    return all(_finite_float(item) is not None for item in pose)


def _decision(
    mode: GateMode,
    reason: str,
    *,
    input_valid: bool,
    contact: bool = False,
    invalid_fields: Sequence[str] = (),
) -> GateDecision:
    return GateDecision(
        mode=mode,
        bias_estimation_contact_mask=contact,
        bias_contact_reason=reason,
        update_allowed=mode is GateMode.UPDATE,
        input_valid=input_valid,
        invalid_fields=tuple(invalid_fields),
    )


def decide_gate(
    sample: Mapping[str, Any],
    *,
    gates: Mapping[str, Any],
    tcp_speed_m_s: float | None = None,
    tcp_omega_rad_s: float | None = None,
    dt_s: float | None = None,
) -> GateDecision:
    """Return a fail-closed update/freeze/predict decision.

    ``UPDATE`` is intentionally difficult to reach: all required evidence,
    including a positive credited contiguous interval, must be valid.  The
    threshold comparisons are inclusive, so equality is not below a limit.
    """

    normal_threshold = _finite_float(gates.get("normal_load_threshold_n"))
    force_threshold = _finite_float(gates.get("force_norm_threshold_n"))
    torque_threshold = _finite_float(gates.get("torque_norm_threshold_nm"))
    linear_threshold = _finite_float(gates.get("tcp_linear_max_m_s"))
    angular_threshold = _finite_float(gates.get("tcp_angular_max_rad_s"))
    # Config validation guarantees these are valid.  If a caller bypasses it,
    # fail closed instead of inventing thresholds.
    if any(
        value is None or value <= 0.0
        for value in (
            normal_threshold,
            force_threshold,
            torque_threshold,
            linear_threshold,
            angular_threshold,
        )
    ):
        return _decision(
            GateMode.PREDICT_ONLY,
            "gate_config_invalid",
            input_valid=False,
            invalid_fields=("gates",),
        )

    update_states = set(gates.get("update_tp_states", ()))
    known_states = set(gates.get("known_tp_states", ()))
    freeze_states = set(gates.get("freeze_tp_states", ()))
    search_states = set(gates.get("search_tp_states", ()))
    if not update_states or not known_states:
        return _decision(
            GateMode.PREDICT_ONLY,
            "gate_config_invalid",
            input_valid=False,
            invalid_fields=("tp_states",),
        )

    tp_state = _exact_int(sample.get("tp_state"))
    if tp_state is None:
        return _decision(
            GateMode.PREDICT_ONLY,
            "tp_state_invalid",
            input_valid=False,
            invalid_fields=("tp_state",),
        )
    if tp_state not in known_states:
        return _decision(
            GateMode.PREDICT_ONLY,
            "unknown_tp_state",
            input_valid=False,
            invalid_fields=("tp_state",),
        )

    # A known path/contact state is explicit evidence for freezing, even if a
    # secondary sensor field is missing.  It still cannot update anything.
    if tp_state in freeze_states:
        return _decision(
            GateMode.FREEZE,
            "tp_state_path_or_contact",
            input_valid=False,
            contact=True,
            invalid_fields=(),
        )

    if sample.get("sensor_fresh") is not True:
        reason = "sensor_stale" if sample.get("sensor_fresh") is False else "sensor_fresh_invalid"
        return _decision(
            GateMode.PREDICT_ONLY,
            reason,
            input_valid=False,
            invalid_fields=("sensor_fresh",),
        )

    invalid_loads: list[str] = []
    for name in LOAD_FIELDS:
        value = _finite_float(sample.get(name))
        # ``normal_load_n`` is a signed channel in the historical trace.  Its
        # magnitude is the gate quantity; force and torque norms are required
        # to remain finite non-negative norms.
        if value is None or (name != "normal_load_n" and value < 0.0):
            invalid_loads.append(name)
    if invalid_loads:
        return _decision(
            GateMode.PREDICT_ONLY,
            "load_metric_invalid",
            input_valid=False,
            invalid_fields=tuple(invalid_loads),
        )

    contact_invalid = tuple(
        field
        for field in ("contact", "contact_detected")
        if field in sample and not isinstance(sample.get(field), bool)
    )
    if contact_invalid:
        return _decision(
            GateMode.PREDICT_ONLY,
            "contact_flag_invalid",
            input_valid=False,
            invalid_fields=contact_invalid,
        )

    # A valid explicit contact/high-load signal freezes even when another
    # update-only field is absent.  The evidence is not promoted to
    # ``input_valid`` because it cannot qualify an UPDATE row.
    if sample.get("contact") is True or sample.get("contact_detected") is True:
        return _decision(
            GateMode.FREEZE,
            "contact_detected",
            input_valid=False,
            contact=True,
        )
    if abs(float(sample["normal_load_n"])) >= normal_threshold:
        return _decision(
            GateMode.FREEZE,
            "normal_load_threshold",
            input_valid=False,
            contact=True,
        )
    if float(sample["force_norm_n"]) >= force_threshold:
        return _decision(
            GateMode.FREEZE,
            "force_norm_threshold",
            input_valid=False,
            contact=True,
        )
    if float(sample["torque_norm_nm"]) >= torque_threshold:
        return _decision(
            GateMode.FREEZE,
            "torque_norm_threshold",
            input_valid=False,
            contact=True,
        )

    wrench = _valid_wrench6(sample.get("wrench"))
    if wrench is None:
        return _decision(
            GateMode.PREDICT_ONLY,
            "wrench_invalid",
            input_valid=False,
            invalid_fields=("wrench",),
        )

    if not _valid_pose_if_present(sample):
        return _decision(
            GateMode.PREDICT_ONLY,
            "pose_invalid",
            input_valid=False,
            invalid_fields=("tcp_pose_m_rad",),
        )

    linear_speed = _valid_speed(tcp_speed_m_s)
    angular_speed = _valid_speed(tcp_omega_rad_s)
    if linear_speed is None or angular_speed is None:
        invalid = []
        if linear_speed is None:
            invalid.append("tcp_speed_m_s")
        if angular_speed is None:
            invalid.append("tcp_omega_rad_s")
        return _decision(
            GateMode.PREDICT_ONLY,
            "pose_speed_invalid",
            input_valid=False,
            invalid_fields=invalid,
        )

    moving = linear_speed >= linear_threshold or angular_speed >= angular_threshold
    if tp_state in search_states and moving:
        return _decision(
            GateMode.FREEZE,
            "search_motion",
            input_valid=True,
            contact=True,
        )

    if tp_state not in update_states:
        return _decision(
            GateMode.PREDICT_ONLY,
            "tp_state_not_update_eligible" if tp_state not in search_states else "search_state",
            input_valid=True,
        )

    # No row, first row, or a large gap can ever earn update time.
    credited_dt = _finite_float(dt_s)
    if credited_dt is None or credited_dt <= 0.0:
        return _decision(
            GateMode.PREDICT_ONLY,
            "dt_invalid",
            input_valid=False,
            invalid_fields=("dt_credited_s",),
        )

    if moving:
        return _decision(
            GateMode.PREDICT_ONLY,
            "motion_threshold",
            input_valid=True,
        )

    return _decision(GateMode.UPDATE, "free_static", input_valid=True)


def residual_wrench_from_sample(
    sample: Mapping[str, Any],
) -> tuple[float, float, float, float, float, float] | None:
    """Return the logged residual only when it is an exact finite wrench6."""

    return _valid_wrench6(sample.get("wrench"))


__all__ = [
    "BIAS_ESTIMATE_FIELDS",
    "BIAS_RATE_ESTIMATE_FIELDS",
    "GateDecision",
    "GateMode",
    "decide_gate",
    "residual_wrench_from_sample",
]
