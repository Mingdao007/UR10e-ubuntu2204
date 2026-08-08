"""Deterministic SI, frame, synchronization, and internal-wrench helpers."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence

import numpy as np

from .contracts import CONTROL_RATE_HZ, RAW_WRENCH_RATE_HZ


HOST_BATCH_WATCHDOG_S = 0.080


def _control_grid_gap_ticks(previous_s: float, current_s: float) -> int:
    """Return the positive 500 Hz tick gap without inventing missing rows."""

    delta = float(current_s) - float(previous_s)
    if not math.isfinite(delta) or delta <= 0.0:
        raise ValueError("control timestamps must be strictly monotonic")
    gap = int(round(delta * CONTROL_RATE_HZ))
    if gap < 1 or not math.isclose(
        delta * CONTROL_RATE_HZ,
        float(gap),
        rel_tol=0.05,
        abs_tol=CONTROL_RATE_HZ * 1e-9,
    ):
        raise ValueError("control timestamps must lie on a 500 Hz grid")
    return gap


def _array(values: Iterable[float], shape: tuple[int, ...], name: str) -> np.ndarray:
    result = np.asarray(tuple(values), dtype=float)
    if result.shape != shape:
        raise ValueError(f"{name} must have shape {shape}")
    if not np.isfinite(result).all():
        raise ValueError(f"{name} must be finite")
    return result


def convert_wrench_to_si(
    raw_wrench: Sequence[float],
    *,
    force_scale_to_newtons: float,
    torque_scale_to_newton_metres: float,
) -> tuple[float, ...]:
    """Apply explicit vendor/unit scales and return ``[N, N*m]`` values."""

    wrench = _array(raw_wrench, (6,), "raw_wrench")
    scales = (force_scale_to_newtons, torque_scale_to_newton_metres)
    if not all(math.isfinite(value) and value > 0.0 for value in scales):
        raise ValueError("SI conversion scales must be finite and positive")
    result = np.concatenate(
        (
            wrench[:3] * force_scale_to_newtons,
            wrench[3:] * torque_scale_to_newton_metres,
        )
    )
    return tuple(float(value) for value in result)


def transform_wrench_to_target_frame(
    wrench_source_si: Sequence[float],
    *,
    rotation_target_from_source: Sequence[Sequence[float]],
    target_origin_to_source_origin_target_m: Sequence[float],
) -> tuple[float, ...]:
    """Transform a wrench into a target frame using an explicit origin offset.

    ``rotation_target_from_source`` maps source-frame vectors into the target
    frame.  ``target_origin_to_source_origin_target_m`` points from the target
    origin to the source origin and is expressed in the target frame.  Hence
    ``moment_target = R moment_source + p x force_target``.
    """

    wrench = _array(wrench_source_si, (6,), "wrench_source_si")
    rotation = np.asarray(rotation_target_from_source, dtype=float)
    if rotation.shape != (3, 3) or not np.isfinite(rotation).all():
        raise ValueError("rotation_target_from_source must be a finite 3x3 matrix")
    if not np.allclose(rotation.T @ rotation, np.eye(3), rtol=0.0, atol=1e-9):
        raise ValueError("rotation_target_from_source must be orthonormal")
    if not math.isclose(float(np.linalg.det(rotation)), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("rotation_target_from_source must be a proper rotation")
    offset = _array(
        target_origin_to_source_origin_target_m,
        (3,),
        "target_origin_to_source_origin_target_m",
    )
    force_target = rotation @ wrench[:3]
    moment_target = rotation @ wrench[3:] + np.cross(offset, force_target)
    return tuple(float(value) for value in np.concatenate((force_target, moment_target)))


@dataclass(frozen=True)
class CanonicalWrenchSample:
    sequence: int
    timestamp_s: float
    wrench_tcp_si: tuple[float, ...] | Sequence[float]
    frame_id: str
    calibration_sha256: str
    device_time_s: float | None = None
    host_visible_time_s: float | None = None
    batch_id: int = 0
    sample_index: int | None = None

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("wrench sequence must be non-negative")
        if not math.isfinite(self.timestamp_s) or self.timestamp_s < 0.0:
            raise ValueError("wrench timestamp must be finite and non-negative")
        wrench = _array(self.wrench_tcp_si, (6,), "wrench_tcp_si")
        if not self.frame_id.strip():
            raise ValueError("wrench frame_id must be non-empty")
        if len(self.calibration_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.calibration_sha256
        ):
            raise ValueError("calibration_sha256 must be a lowercase SHA-256")
        device_time = self.timestamp_s if self.device_time_s is None else float(self.device_time_s)
        if not math.isfinite(device_time) or device_time < 0.0:
            raise ValueError("wrench device time must be finite and non-negative")
        if self.host_visible_time_s is not None:
            host_visible = float(self.host_visible_time_s)
            if not math.isfinite(host_visible) or host_visible < 0.0:
                raise ValueError(
                    "wrench host-visible time must be finite and non-negative"
                )
            object.__setattr__(self, "host_visible_time_s", host_visible)
        if self.batch_id < 0:
            raise ValueError("wrench batch id must be non-negative")
        sample_index = self.sequence if self.sample_index is None else int(self.sample_index)
        if sample_index < 0:
            raise ValueError("wrench sample index must be non-negative")
        object.__setattr__(self, "device_time_s", device_time)
        object.__setattr__(self, "sample_index", sample_index)
        object.__setattr__(self, "wrench_tcp_si", tuple(float(value) for value in wrench))

    @property
    def has_dual_clock(self) -> bool:
        """Whether this row carries an explicit host-visible arrival clock."""

        return self.host_visible_time_s is not None


@dataclass(frozen=True)
class CausalWrenchAlignment:
    control_timestamp_s: float
    source_timestamp_s: float
    source_sequence: int
    wrench_tcp_si: tuple[float, ...]
    age_s: float
    source_device_time_s: float | None = None
    source_host_visible_time_s: float | None = None
    source_batch_id: int | None = None
    source_sample_index: int | None = None
    external_hold: bool = False
    external_held_ticks: int = 0
    device_age_samples: int = 0
    host_age_s: float | None = None

    @property
    def external_device_time_s(self) -> float | None:
        return self.source_device_time_s

    @property
    def external_host_visible_time_s(self) -> float | None:
        return self.source_host_visible_time_s

    @property
    def external_batch_id(self) -> int | None:
        return self.source_batch_id

    @property
    def external_sample_index(self) -> int | None:
        return self.source_sample_index


def causal_sync_wrench_1khz_to_control_500hz(
    raw_samples: Sequence[CanonicalWrenchSample],
    control_timestamps_s: Sequence[float],
    *,
    expected_frame_id: str,
    expected_calibration_sha256: str,
    max_sample_age_s: float | None = None,
    max_host_age_s: float | None = None,
) -> tuple[CausalWrenchAlignment, ...]:
    """Causally select the newest already-arrived 1 kHz sample per 500 Hz tick.

    New v3 rows use ``host_visible_time_s`` for causality and retain
    ``device_time_s`` only for ordering/lineage.  A TCP receive batch therefore
    gives every contained sample the same host-visible timestamp, and a 500 Hz
    tick may explicitly hold the newest selected sample.  Legacy rows without
    an arrival clock retain the v2 timestamp semantics for backward reads; no
    arrival clock is fabricated for a dual-clock stream.
    """

    if not raw_samples or not control_timestamps_s:
        raise ValueError("raw_samples and control_timestamps_s must be non-empty")
    if not expected_frame_id.strip():
        raise ValueError("expected_frame_id must be non-empty")
    samples = tuple(raw_samples)
    dual_clock = any(sample.host_visible_time_s is not None for sample in samples)
    if dual_clock and any(sample.host_visible_time_s is None for sample in samples):
        raise ValueError("dual-clock wrench stream requires host-visible time on every sample")
    legacy_max_age = 2.0 / RAW_WRENCH_RATE_HZ if max_sample_age_s is None else float(max_sample_age_s)
    if not math.isfinite(legacy_max_age) or legacy_max_age <= 0.0:
        raise ValueError("max_sample_age_s must be finite and positive")
    if max_host_age_s is None:
        host_max_age = HOST_BATCH_WATCHDOG_S if dual_clock else legacy_max_age
    else:
        host_max_age = float(max_host_age_s)
    if not math.isfinite(host_max_age) or host_max_age <= 0.0:
        raise ValueError("max_host_age_s must be finite and positive")
    for index, sample in enumerate(samples):
        if not isinstance(sample, CanonicalWrenchSample):
            raise ValueError("raw_samples must contain CanonicalWrenchSample values")
        if sample.frame_id != expected_frame_id:
            raise ValueError("raw wrench frame mismatch")
        if sample.calibration_sha256 != expected_calibration_sha256:
            raise ValueError("raw wrench calibration mismatch")
        if index and sample.sequence <= samples[index - 1].sequence:
            raise ValueError("raw wrench sequences must be strictly monotonic")
        if index and sample.sample_index <= samples[index - 1].sample_index:
            raise ValueError("raw wrench sample indices must be strictly monotonic")
        if index and sample.device_time_s < samples[index - 1].device_time_s - 1e-12:
            raise ValueError("raw wrench device time must be non-decreasing")
        if index and dual_clock:
            previous = samples[index - 1]
            if sample.host_visible_time_s < previous.host_visible_time_s - 1e-12:
                raise ValueError("raw wrench host-visible time must be non-decreasing")
            if (
                sample.batch_id == previous.batch_id
                and not math.isclose(
                    sample.host_visible_time_s,
                    previous.host_visible_time_s,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
            ):
                raise ValueError("samples in one TCP batch must share arrival time")
        if index and not dual_clock and sample.timestamp_s - samples[index - 1].timestamp_s > 2.0 / RAW_WRENCH_RATE_HZ + 1e-12:
            raise ValueError("raw wrench stream contains a gap larger than two 1 kHz periods")
    ticks = tuple(float(value) for value in control_timestamps_s)
    if not all(math.isfinite(value) and value >= 0.0 for value in ticks):
        raise ValueError("control timestamps must be finite and non-negative")
    tick_gaps = [1]
    for index in range(1, len(ticks)):
        tick_gaps.append(_control_grid_gap_ticks(ticks[index - 1], ticks[index]))

    result: list[CausalWrenchAlignment] = []
    sample_index = -1
    previous_selected_sample_index: int | None = None
    held_ticks = 0
    for tick_index, tick in enumerate(ticks):
        def host_time(sample: CanonicalWrenchSample) -> float:
            # This fallback is intentionally restricted to the legacy all-v2
            # path.  New rows must carry an explicit host-visible clock.
            return sample.timestamp_s if sample.host_visible_time_s is None else sample.host_visible_time_s

        while (
            sample_index + 1 < len(samples)
            and host_time(samples[sample_index + 1]) <= tick
        ):
            sample_index += 1
        if sample_index < 0:
            raise ValueError("no causally available wrench sample for control tick")
        sample = samples[sample_index]
        host_age = tick - host_time(sample)
        if host_age > host_max_age + 1e-12:
            raise ValueError("causally available wrench sample is stale")
        selected_index = sample.sample_index
        external_hold = selected_index == previous_selected_sample_index
        held_ticks = held_ticks + tick_gaps[tick_index] if external_hold else 0
        # A hold is a control-side diagnostic.  It does not infer a future
        # device sample or interpolate between device timestamps.
        device_age_samples = held_ticks * (RAW_WRENCH_RATE_HZ // CONTROL_RATE_HZ)
        result.append(
            CausalWrenchAlignment(
                control_timestamp_s=tick,
                source_timestamp_s=sample.timestamp_s,
                source_sequence=sample.sequence,
                wrench_tcp_si=sample.wrench_tcp_si,
                age_s=host_age,
                source_device_time_s=sample.device_time_s,
                source_host_visible_time_s=sample.host_visible_time_s,
                source_batch_id=sample.batch_id,
                source_sample_index=selected_index,
                external_hold=external_hold,
                external_held_ticks=held_ticks,
                device_age_samples=device_age_samples,
                host_age_s=host_age,
            )
        )
        previous_selected_sample_index = selected_index
    return tuple(result)


@dataclass(frozen=True)
class InternalWrenchEstimate:
    wrench_tcp_si: tuple[float, ...]
    joint_torque_residual_nm: tuple[float, ...]
    residual_norm_nm: float
    actual_current_shadow_wrench_tcp_si: tuple[float, ...] | None
    actual_current_shadow_delta_norm: float | None
    source_tick_offset: int = -1


def _solve_wrench(
    jacobian: np.ndarray,
    joint_wrench_torque: np.ndarray,
    damping: float,
) -> tuple[np.ndarray, np.ndarray]:
    operator = jacobian.T
    regularized = operator.T @ operator + (damping * damping) * np.eye(6)
    wrench = np.linalg.solve(regularized, operator.T @ joint_wrench_torque)
    residual = operator @ wrench - joint_wrench_torque
    if not np.isfinite(wrench).all() or not np.isfinite(residual).all():
        raise ValueError("internal-wrench reconstruction produced non-finite output")
    return wrench, residual


def reconstruct_internal_wrench_from_previous_command(
    *,
    previous_applied_no_gravity_joint_torque_nm: Sequence[float],
    previous_jacobian_tcp: Sequence[Sequence[float]],
    previous_coriolis_joint_torque_nm: Sequence[float],
    previous_joint_damping_torque_nm: Sequence[float],
    jacobian_frame_id: str,
    canonical_tcp_frame_id: str,
    damping: float = 1e-6,
    actual_current_as_torque_nm_shadow: Sequence[float] | None = None,
) -> InternalWrenchEstimate:
    """Reconstruct the previous applied wrench without using the F/T sensor.

    The no-gravity command contract is
    ``tau = J.T @ wrench + coriolis - joint_damping``.  Therefore the wrench
    contribution is ``tau - coriolis + joint_damping``.  An optional
    ``actual_current_as_torque`` value is reconstructed separately and only
    reported as a shadow cross-check; it cannot alter the primary estimate.
    """

    if jacobian_frame_id != canonical_tcp_frame_id or not canonical_tcp_frame_id.strip():
        raise ValueError("Jacobian and observation must use the same canonical TCP frame")
    if not math.isfinite(damping) or damping <= 0.0:
        raise ValueError("damping must be finite and positive")
    applied = _array(
        previous_applied_no_gravity_joint_torque_nm,
        (6,),
        "previous_applied_no_gravity_joint_torque_nm",
    )
    coriolis = _array(
        previous_coriolis_joint_torque_nm,
        (6,),
        "previous_coriolis_joint_torque_nm",
    )
    joint_damping = _array(
        previous_joint_damping_torque_nm,
        (6,),
        "previous_joint_damping_torque_nm",
    )
    jacobian = np.asarray(previous_jacobian_tcp, dtype=float)
    if jacobian.shape != (6, 6) or not np.isfinite(jacobian).all():
        raise ValueError("previous_jacobian_tcp must be a finite 6x6 matrix")
    wrench, residual = _solve_wrench(
        jacobian,
        applied - coriolis + joint_damping,
        damping,
    )

    shadow_wrench: tuple[float, ...] | None = None
    shadow_delta: float | None = None
    if actual_current_as_torque_nm_shadow is not None:
        actual = _array(
            actual_current_as_torque_nm_shadow,
            (6,),
            "actual_current_as_torque_nm_shadow",
        )
        shadow, _ = _solve_wrench(
            jacobian,
            actual - coriolis + joint_damping,
            damping,
        )
        shadow_wrench = tuple(float(value) for value in shadow)
        shadow_delta = float(np.linalg.norm(shadow - wrench))

    return InternalWrenchEstimate(
        wrench_tcp_si=tuple(float(value) for value in wrench),
        joint_torque_residual_nm=tuple(float(value) for value in residual),
        residual_norm_nm=float(np.linalg.norm(residual)),
        actual_current_shadow_wrench_tcp_si=shadow_wrench,
        actual_current_shadow_delta_norm=shadow_delta,
    )
