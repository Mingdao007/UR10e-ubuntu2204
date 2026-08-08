#!/usr/bin/env python3
"""Build deterministic numeric proof for the formal V4 handoff contract."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

ROOT = Path(__file__).resolve().parents[1]
VIC_ROOT = ROOT.parent / "ur10e-variable-impedance"
if str(VIC_ROOT) not in sys.path:
    sys.path.insert(0, str(VIC_ROOT))

from ur10e_vic.tacdiffusion.formal_contact_acquisition import (
    ACQUISITION_ACCELERATION_M_S2,
    ACQUISITION_BRAKING_DISTANCE_M,
    ACQUISITION_CONTROL_PERIOD_S,
    ACQUISITION_DECELERATION_M_S2,
    ACQUISITION_DECELERATION_START_DISTANCE_M,
    ACQUISITION_DISCRETE_STOP_BOUND_M,
    ACQUISITION_FORCE_GUARD_N,
    ACQUISITION_HEARTBEAT_TIMEOUT_TICKS,
    ACQUISITION_LATCH_LOAD_N,
    ACQUISITION_LATCH_SAMPLES,
    ACQUISITION_MAX_DISTANCE_M,
    ACQUISITION_SENSOR_DELIVERY_WATCHDOG_S,
    ACQUISITION_SPEED_M_S,
    ACQUISITION_PREPARE_TIMEOUT_TICKS,
    ACQUISITION_STATIONARY_TCP_SPEED_LIMIT_M_S,
    ACQUISITION_TORQUE_GUARD_NM,
    FIXED_TRACKING_STIFFNESS,
    MAX_HANDOFF_MISMATCH_M,
)


OUTPUT = ROOT / "config" / "tacdiffusion_formal_v4_numeric_sanity.json"


def _atomic_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def build() -> dict[str, object]:
    nominal_duration_s = 50.0
    nominal_distance_m = ACQUISITION_SPEED_M_S * nominal_duration_s
    acceleration_time_s = ACQUISITION_SPEED_M_S / ACQUISITION_ACCELERATION_M_S2
    deceleration_time_s = ACQUISITION_SPEED_M_S / ACQUISITION_DECELERATION_M_S2
    acceleration_distance_m = (
        0.5 * ACQUISITION_ACCELERATION_M_S2 * acceleration_time_s**2
    )
    deceleration_distance_m = (
        0.5 * ACQUISITION_SPEED_M_S * deceleration_time_s
    )
    cruise_distance_m = (
        ACQUISITION_MAX_DISTANCE_M
        - acceleration_distance_m
        - deceleration_distance_m
        - ACQUISITION_DISCRETE_STOP_BOUND_M
    )
    profile_distance_m = (
        acceleration_distance_m + cruise_distance_m + deceleration_distance_m
    )
    braking_distance_m = ACQUISITION_BRAKING_DISTANCE_M
    discrete_stop_bound_m = ACQUISITION_DISCRETE_STOP_BOUND_M
    deceleration_start_distance_m = ACQUISITION_DECELERATION_START_DISTANCE_M
    commanded_stop_distance_m = deceleration_start_distance_m + braking_distance_m
    physical_worst_case_distance_m = commanded_stop_distance_m + discrete_stop_bound_m
    if not math.isclose(braking_distance_m, deceleration_distance_m, abs_tol=1.0e-15):
        raise RuntimeError("numeric braking-distance proof does not match profile")
    if physical_worst_case_distance_m > ACQUISITION_MAX_DISTANCE_M + 1.0e-15:
        raise RuntimeError("numeric commanded stop exceeds the 25 mm maximum")
    one_mm_force_n = FIXED_TRACKING_STIFFNESS[0] * 0.001
    maximum_handoff_force_mismatch_n = (
        FIXED_TRACKING_STIFFNESS[0] * MAX_HANDOFF_MISMATCH_M
    )
    payload: dict[str, object] = {
        "schema_version": "ur10e_tacdiffusion_formal_v4_numeric_sanity/v1",
        "lineage": "tacdiffusion_formal_v4",
        "offline_only": True,
        "distance_proof": {
            "speed_m_s": ACQUISITION_SPEED_M_S,
            "duration_s": nominal_duration_s,
            "distance_m": nominal_distance_m,
            "distance_mm": nominal_distance_m * 1000.0,
            "assertion": "0.0005 m/s * 50 s = 0.025 m = 25 mm",
            "exact_25mm": math.isclose(
                nominal_distance_m, ACQUISITION_MAX_DISTANCE_M, abs_tol=1.0e-15
            ),
        },
        "bounded_profile": {
            "acceleration_m_s2": ACQUISITION_ACCELERATION_M_S2,
            "deceleration_m_s2": ACQUISITION_DECELERATION_M_S2,
            "acceleration_time_s": acceleration_time_s,
            "deceleration_time_s": deceleration_time_s,
            "acceleration_distance_m": acceleration_distance_m,
            "deceleration_distance_m": deceleration_distance_m,
            "braking_formula": "v^2/(2a) = 0.0005^2/(2*0.01) = 0.0000125 m = 12.5 micrometres",
            "braking_formula_matches_profile": math.isclose(
                braking_distance_m, deceleration_distance_m, abs_tol=1.0e-15
            ),
            "cruise_distance_m": cruise_distance_m,
            "profile_distance_m": profile_distance_m,
            "maximum_search_distance_m": ACQUISITION_MAX_DISTANCE_M,
            "profile_does_not_overshoot": profile_distance_m
            <= ACQUISITION_MAX_DISTANCE_M + 1.0e-15,
            "discrete_stop_bound_m": discrete_stop_bound_m,
            "braking_distance_m_v2_over_2a": braking_distance_m,
            "deceleration_start_distance_m": deceleration_start_distance_m,
            "commanded_stop_distance_m": commanded_stop_distance_m,
            "physical_worst_case_distance_m": physical_worst_case_distance_m,
            "bounded_overshoot_m": max(
                0.0, physical_worst_case_distance_m - ACQUISITION_MAX_DISTANCE_M
            ),
            "stop_fits_inside_maximum": physical_worst_case_distance_m
            <= ACQUISITION_MAX_DISTANCE_M + 1.0e-15,
            "maximum_bounded_distance_m": physical_worst_case_distance_m,
        },
        "fixed_k_mapping": {
            "stiffness_6d": list(FIXED_TRACKING_STIFFNESS),
            "one_mm_error_m": 0.001,
            "one_mm_force_n": one_mm_force_n,
            "maximum_handoff_mismatch_m": MAX_HANDOFF_MISMATCH_M,
            "maximum_handoff_force_mismatch_n": maximum_handoff_force_mismatch_n,
            "initial_desired_pose_is_fresh_actual": True,
            "initial_feedforward_wrench_6d": [0.0] * 6,
        },
        "guards": {
            "force_limit_n": ACQUISITION_FORCE_GUARD_N,
            "torque_limit_nm": ACQUISITION_TORQUE_GUARD_NM,
            "latch_load_n": ACQUISITION_LATCH_LOAD_N,
            "latch_consecutive_native_samples": ACQUISITION_LATCH_SAMPLES,
            "native_sensor_rate_hz": 1000,
            "sensor_delivery_watchdog_s": ACQUISITION_SENSOR_DELIVERY_WATCHDOG_S,
            "sensor_delivery_semantics": (
                "latest_native_batch_delivery_age_only_not_per_frame_host_arrival"
            ),
            "heartbeat_timeout_s": ACQUISITION_HEARTBEAT_TIMEOUT_TICKS
            * ACQUISITION_CONTROL_PERIOD_S,
            "heartbeat_timeout_ticks": ACQUISITION_HEARTBEAT_TIMEOUT_TICKS,
            "prepare_timeout_s": ACQUISITION_PREPARE_TIMEOUT_TICKS
            * ACQUISITION_CONTROL_PERIOD_S,
            "prepare_timeout_ticks": ACQUISITION_PREPARE_TIMEOUT_TICKS,
            "authority": "kunwei_kwr75_tcp_raw_stream_v1",
        },
        "transition_invariants": {
            "search_to_stopping": "host_latch_1N_for_50_consecutive_native_samples",
            "search_stop_distance": (
                "decelerate_at_25mm_minus_12.5um_minus_one_control_tick"
            ),
            "stopping_to_dwell": "velocity_command_zero_and_fresh_kinematic_sample",
            "stationary_tcp_speed_limit_m_s": ACQUISITION_STATIONARY_TCP_SPEED_LIMIT_M_S,
            "stationary_threshold_below_search_speed": (
                ACQUISITION_STATIONARY_TCP_SPEED_LIMIT_M_S
                < ACQUISITION_SPEED_M_S
            ),
            "dwell_to_handoff": "fresh_stationary_dwell_complete",
            "handoff_to_direct_torque": "successful_identity_bound_handoff_only",
            "fault_policy": "fail_closed_no_automatic_return",
            "formal_training_window": "TRACK_plus_STATE_TORQUE_only",
            "acquisition_training": False,
        },
    }
    _atomic_write(OUTPUT, payload)
    return {"ok": True, "output": str(OUTPUT), "payload": payload}


if __name__ == "__main__":
    print(json.dumps(build(), indent=2, sort_keys=True))
