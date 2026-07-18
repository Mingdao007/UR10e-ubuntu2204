#!/usr/bin/env python3
"""Fail-closed estimation-mode observer for offline UR contact replay.

This module never opens a device, sends a command, or changes controller state.
It converts existing Step5d bridge facts into estimator semantics so adaptive
bias updates cannot absorb contact force.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping


class EstimationMode(str, Enum):
    INVALID = "invalid"
    UNKNOWN = "unknown"
    FREE_STATIC = "free_static"
    FREE_REFERENCE = "free_reference"
    SEARCH = "search"
    IMPACT = "impact"
    CONTACT_TRACK = "contact_track"
    LIFT_OFF = "lift_off"
    REBASELINE = "rebaseline"


@dataclass(frozen=True)
class ModeConfig:
    static_linear_speed_max_m_s: float = 2.0e-4
    static_angular_speed_max_rad_s: float = 1.0e-3
    static_dwell_s: float = 0.25
    impact_dwell_s: float = 0.05
    release_dwell_s: float = 0.25
    contact_normal_enter_n: float = 0.75
    contact_force_enter_n: float = 2.0
    contact_exit_scale: float = 0.70
    stage_tolerance: float = 0.05

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ModeConfig":
        fields = cls.__dataclass_fields__
        unknown = sorted(set(payload) - set(fields))
        if unknown:
            raise ValueError(f"unknown mode config fields: {unknown}")
        values = {name: float(payload[name]) for name in payload}
        config = cls(**values)
        config.validate()
        return config

    def validate(self) -> None:
        positive = {
            "static_linear_speed_max_m_s": self.static_linear_speed_max_m_s,
            "static_angular_speed_max_rad_s": self.static_angular_speed_max_rad_s,
            "static_dwell_s": self.static_dwell_s,
            "impact_dwell_s": self.impact_dwell_s,
            "release_dwell_s": self.release_dwell_s,
            "contact_normal_enter_n": self.contact_normal_enter_n,
            "contact_force_enter_n": self.contact_force_enter_n,
            "stage_tolerance": self.stage_tolerance,
        }
        if any(not math.isfinite(value) or value <= 0.0 for value in positive.values()):
            raise ValueError(f"mode config values must be finite and positive: {positive}")
        if not math.isfinite(self.contact_exit_scale) or not 0.0 < self.contact_exit_scale < 1.0:
            raise ValueError("contact_exit_scale must be strictly between zero and one")


@dataclass(frozen=True)
class ModeObservation:
    t_monotonic_s: float
    dt_s: float
    stage: float
    bridge_profile: str
    baseline_ready: bool
    sensor_fresh: bool
    sample_finite: bool
    source_valid: bool
    normal_load_n: float
    force_norm_n: float
    source_contact_mask: bool
    control_contact_window: bool
    normal_acquired: bool
    force_settle_ready: bool
    contact_safety_state: float
    cmd_valid: bool
    linear_speed_m_s: float
    angular_speed_rad_s: float
    phase_s: float


@dataclass(frozen=True)
class ModeDecision:
    mode: EstimationMode
    contact_mask: int
    update_allowed: int
    confidence: str
    transition_id: int
    freeze_reason: str
    phase_s: float | None


def _finite_float(value: Any, default: float = math.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _truthy(value: Any) -> bool:
    return _finite_float(value, 0.0) > 0.5


def _norm(values: list[float]) -> float:
    return math.sqrt(sum(value * value for value in values)) if all(math.isfinite(v) for v in values) else math.nan


class Step5dV3Adapter:
    """Decode Step5d-v3 bridge and Kunwei rows into one typed observation."""

    SENSOR_WRENCH_FIELDS = (
        "fx_n_zeroed",
        "fy_n_zeroed",
        "fz_n_zeroed",
        "mx_nm_zeroed",
        "my_nm_zeroed",
        "mz_nm_zeroed",
    )

    def __init__(
        self,
        *,
        bridge_profile: str,
        mode_config: ModeConfig,
        max_sensor_age_s: float,
    ) -> None:
        mode_config.validate()
        if not bridge_profile:
            raise ValueError("bridge_profile is required")
        if not math.isfinite(max_sensor_age_s) or max_sensor_age_s <= 0.0:
            raise ValueError("max_sensor_age_s must be finite and positive")
        self.bridge_profile = bridge_profile
        self.mode_config = mode_config
        self.max_sensor_age_s = float(max_sensor_age_s)

    @classmethod
    def from_metadata(
        cls,
        metadata: Mapping[str, Any],
        *,
        base_config: ModeConfig,
        max_sensor_age_s: float,
    ) -> "Step5dV3Adapter":
        args = metadata.get("args", {})
        if not isinstance(args, Mapping):
            raise ValueError("metadata.args must be an object")
        profile = str(args.get("bridge_profile") or args.get("step4e_version") or "")
        if not profile:
            raise ValueError("metadata does not declare a bridge profile")
        normal_threshold = _finite_float(
            args.get("bias_contact_normal_threshold_n"),
            base_config.contact_normal_enter_n,
        )
        force_threshold = _finite_float(
            args.get("bias_contact_force_norm_threshold_n"),
            base_config.contact_force_enter_n,
        )
        config = replace(
            base_config,
            contact_normal_enter_n=normal_threshold,
            contact_force_enter_n=force_threshold,
        )
        return cls(
            bridge_profile=profile,
            mode_config=config,
            max_sensor_age_s=max_sensor_age_s,
        )

    def observation(
        self,
        bridge_row: Mapping[str, Any],
        sensor_row: Mapping[str, Any],
        *,
        previous_bridge_t_s: float | None,
    ) -> ModeObservation:
        bridge_t = _finite_float(bridge_row.get("t_monotonic_s"))
        sensor_t = _finite_float(sensor_row.get("t_monotonic_s"))
        dt_s = 0.0 if previous_bridge_t_s is None else bridge_t - previous_bridge_t_s
        sensor_age_s = bridge_t - sensor_t
        wrench = [_finite_float(sensor_row.get(name)) for name in self.SENSOR_WRENCH_FIELDS]
        sample_finite = all(math.isfinite(value) for value in wrench)
        sensor_fresh = (
            math.isfinite(sensor_age_s)
            and sensor_age_s >= -1.0e-12
            and sensor_age_s <= self.max_sensor_age_s
        )
        baseline_ready = _truthy(bridge_row.get("baseline_ready"))
        bridge_zero = _finite_float(
            bridge_row.get("zero_event_id", bridge_row.get("baseline_epoch"))
        )
        sensor_zero = _finite_float(sensor_row.get("zero_event_id"))
        source_valid = (
            math.isfinite(bridge_t)
            and math.isfinite(sensor_t)
            and (previous_bridge_t_s is None or (math.isfinite(dt_s) and dt_s > 0.0))
            and baseline_ready
            and math.isfinite(bridge_zero)
            and math.isfinite(sensor_zero)
            and int(bridge_zero) == int(sensor_zero)
        )
        linear = [_finite_float(bridge_row.get(f"ur_actual_TCP_speed_{idx}")) for idx in range(3)]
        angular = [_finite_float(bridge_row.get(f"ur_actual_TCP_speed_{idx}")) for idx in range(3, 6)]
        bridge_mask = _truthy(bridge_row.get("bias_estimation_contact_mask"))
        sensor_mask = _truthy(sensor_row.get("bias_estimation_contact_mask"))
        normal_load = _finite_float(
            bridge_row.get("normal_force_n"),
            _finite_float(sensor_row.get("normal_force_n")),
        )
        force_norm = _finite_float(
            bridge_row.get("force_norm_n"),
            _finite_float(sensor_row.get("force_norm_n")),
        )
        return ModeObservation(
            t_monotonic_s=bridge_t,
            dt_s=dt_s,
            stage=_finite_float(bridge_row.get("ur_output_double_register_35")),
            bridge_profile=self.bridge_profile,
            baseline_ready=baseline_ready,
            sensor_fresh=sensor_fresh,
            sample_finite=sample_finite,
            source_valid=source_valid,
            normal_load_n=normal_load,
            force_norm_n=force_norm,
            source_contact_mask=bridge_mask or sensor_mask,
            control_contact_window=_truthy(bridge_row.get("control_contact_window")),
            normal_acquired=_truthy(bridge_row.get("_step4e_normal_acquired")),
            force_settle_ready=_truthy(bridge_row.get("_step5d_force_settle_ready")),
            contact_safety_state=_finite_float(bridge_row.get("_step5d_contact_safety_state"), 0.0),
            cmd_valid=_truthy(bridge_row.get("step4e_cmd_valid")),
            linear_speed_m_s=_norm(linear),
            angular_speed_rad_s=_norm(angular),
            phase_s=_finite_float(
                bridge_row.get("_step4e_path_time_s"),
                _finite_float(bridge_row.get("step4e_progress_m")),
            ),
        )


class ContactModeObserver:
    """Stateful fail-closed observer used by the offline bias estimator."""

    def __init__(self, config: ModeConfig) -> None:
        config.validate()
        self.config = config
        self.mode = EstimationMode.UNKNOWN
        self.transition_id = 0
        self.static_elapsed_s = 0.0
        self.impact_elapsed_s = 0.0
        self.release_elapsed_s = 0.0
        self.contact_latched = False

    def _stage_is(self, stage: float, expected: float) -> bool:
        return math.isfinite(stage) and abs(stage - expected) < self.config.stage_tolerance

    def _emit(
        self,
        mode: EstimationMode,
        *,
        contact_mask: int,
        update_allowed: int,
        confidence: str,
        freeze_reason: str,
        phase_s: float,
    ) -> ModeDecision:
        if mode != self.mode:
            self.transition_id += 1
            self.mode = mode
        return ModeDecision(
            mode=mode,
            contact_mask=int(contact_mask),
            update_allowed=int(update_allowed),
            confidence=confidence,
            transition_id=self.transition_id,
            freeze_reason=freeze_reason,
            phase_s=phase_s if math.isfinite(phase_s) else None,
        )

    def decide(self, observation: ModeObservation) -> ModeDecision:
        dt_s = observation.dt_s if math.isfinite(observation.dt_s) and observation.dt_s >= 0.0 else 0.0
        valid = (
            observation.source_valid
            and observation.baseline_ready
            and observation.sensor_fresh
            and observation.sample_finite
            and math.isfinite(observation.normal_load_n)
            and math.isfinite(observation.force_norm_n)
            and math.isfinite(observation.linear_speed_m_s)
            and math.isfinite(observation.angular_speed_rad_s)
        )
        if not valid:
            self.static_elapsed_s = 0.0
            self.release_elapsed_s = 0.0
            return self._emit(
                EstimationMode.INVALID,
                contact_mask=1,
                update_allowed=0,
                confidence="low",
                freeze_reason="invalid_or_stale_source",
                phase_s=observation.phase_s,
            )

        force_enter = (
            abs(observation.normal_load_n) >= self.config.contact_normal_enter_n
            or observation.force_norm_n >= self.config.contact_force_enter_n
        )
        force_clear = (
            abs(observation.normal_load_n)
            < self.config.contact_normal_enter_n * self.config.contact_exit_scale
            and observation.force_norm_n
            < self.config.contact_force_enter_n * self.config.contact_exit_scale
        )
        explicit_contact = (
            observation.source_contact_mask
            or observation.control_contact_window
            or observation.normal_acquired
            or observation.force_settle_ready
            or observation.contact_safety_state > 0.5
            or force_enter
        )
        lift_stage = self._stage_is(observation.stage, 25.10)
        search_stage = any(
            self._stage_is(observation.stage, stage)
            for stage in (24.0, 24.2, 25.05, 25.2, 25.3)
        )
        track_stage = self._stage_is(observation.stage, 25.0)

        if lift_stage:
            self.contact_latched = True
            self.static_elapsed_s = 0.0
            self.release_elapsed_s = self.release_elapsed_s + dt_s if force_clear else 0.0
            if self.release_elapsed_s >= self.config.release_dwell_s:
                self.contact_latched = False
                self.release_elapsed_s = 0.0
                return self._emit(
                    EstimationMode.REBASELINE,
                    contact_mask=1,
                    update_allowed=0,
                    confidence="high",
                    freeze_reason="release_dwell_complete_rebaseline",
                    phase_s=observation.phase_s,
                )
            return self._emit(
                EstimationMode.LIFT_OFF,
                contact_mask=1,
                update_allowed=0,
                confidence="high",
                freeze_reason="controller_detach_or_release_dwell",
                phase_s=observation.phase_s,
            )

        if explicit_contact and not self.contact_latched:
            self.contact_latched = True
            self.impact_elapsed_s = 0.0
            self.release_elapsed_s = 0.0
        if self.contact_latched:
            self.static_elapsed_s = 0.0
            if explicit_contact:
                self.release_elapsed_s = 0.0
                self.impact_elapsed_s += dt_s
                if self.impact_elapsed_s < self.config.impact_dwell_s and self.mode != EstimationMode.CONTACT_TRACK:
                    return self._emit(
                        EstimationMode.IMPACT,
                        contact_mask=1,
                        update_allowed=0,
                        confidence="high" if observation.source_contact_mask or observation.normal_acquired else "medium",
                        freeze_reason="contact_onset_impact_dwell",
                        phase_s=observation.phase_s,
                    )
                return self._emit(
                    EstimationMode.CONTACT_TRACK,
                    contact_mask=1,
                    update_allowed=0,
                    confidence="high" if track_stage or observation.normal_acquired else "medium",
                    freeze_reason="contact_measurement_update_forbidden",
                    phase_s=observation.phase_s,
                )
            self.release_elapsed_s = self.release_elapsed_s + dt_s if force_clear else 0.0
            if self.release_elapsed_s < self.config.release_dwell_s:
                return self._emit(
                    EstimationMode.LIFT_OFF,
                    contact_mask=1,
                    update_allowed=0,
                    confidence="medium",
                    freeze_reason="post_contact_release_dwell",
                    phase_s=observation.phase_s,
                )
            self.contact_latched = False
            self.release_elapsed_s = 0.0
            return self._emit(
                EstimationMode.REBASELINE,
                contact_mask=1,
                update_allowed=0,
                confidence="high",
                freeze_reason="release_dwell_complete_rebaseline",
                phase_s=observation.phase_s,
            )

        if search_stage:
            self.static_elapsed_s = 0.0
            return self._emit(
                EstimationMode.SEARCH,
                contact_mask=1,
                update_allowed=0,
                confidence="high",
                freeze_reason="controller_search_or_acquire_stage",
                phase_s=observation.phase_s,
            )
        if track_stage:
            self.static_elapsed_s = 0.0
            return self._emit(
                EstimationMode.UNKNOWN,
                contact_mask=1,
                update_allowed=0,
                confidence="low",
                freeze_reason="stage25_without_confirmed_contact",
                phase_s=observation.phase_s,
            )

        static_candidate = (
            not observation.cmd_valid
            and observation.linear_speed_m_s <= self.config.static_linear_speed_max_m_s
            and observation.angular_speed_rad_s <= self.config.static_angular_speed_max_rad_s
            and force_clear
        )
        self.static_elapsed_s = self.static_elapsed_s + dt_s if static_candidate else 0.0
        if static_candidate and self.static_elapsed_s >= self.config.static_dwell_s:
            return self._emit(
                EstimationMode.FREE_STATIC,
                contact_mask=0,
                update_allowed=1,
                confidence="high",
                freeze_reason="",
                phase_s=observation.phase_s,
            )
        if "no_contact" in observation.bridge_profile:
            return self._emit(
                EstimationMode.FREE_REFERENCE,
                contact_mask=0,
                update_allowed=0,
                confidence="high",
                freeze_reason="dynamic_reference_only" if not static_candidate else "static_dwell_incomplete",
                phase_s=observation.phase_s,
            )
        return self._emit(
            EstimationMode.UNKNOWN,
            contact_mask=1,
            update_allowed=0,
            confidence="low",
            freeze_reason="no_high_confidence_free_or_contact_state",
            phase_s=observation.phase_s,
        )
