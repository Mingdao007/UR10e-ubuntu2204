#!/usr/bin/env python3
"""P0 v9 bridge wrapper with strict qualification and no-contact guards."""

from __future__ import annotations

import math
import time
from typing import Any

import numpy as np

import kunwei_rtde_bridge as base
from step5d_p0_v9_control_core import (
    P0_V9_ACTUAL_NORMAL_SPEED_DWELL_S,
    P0_V9_ACTUAL_NORMAL_SPEED_STOP_M_S,
    P0_V9_NORMAL_DISPLACEMENT_STOP_M,
)


QUALIFIED_FIELD = "_step5d_p0_v9_qualified_s"
QUALIFIED_BOOL_FIELD = "_step5d_p0_v9_qualified"
QUALIFICATION_REASON_FIELD = "_step5d_p0_v9_qualification_reason"
_base_compute_bridge_values = base.compute_bridge_values


def update_qualified_time(
    state: base.BridgeState,
    *,
    qualified: bool,
    now_s: float,
) -> float:
    prior = getattr(state, "p0_v9_last_qualified_time_s", None)
    if not qualified:
        state.p0_v9_qualified_s = 0.0
        state.p0_v9_last_qualified_time_s = None
        return 0.0
    if prior is None:
        state.p0_v9_qualified_s = 0.0
    else:
        state.p0_v9_qualified_s = float(
            getattr(state, "p0_v9_qualified_s", 0.0)
        ) + max(0.0, now_s - float(prior))
    state.p0_v9_last_qualified_time_s = now_s
    return float(state.p0_v9_qualified_s)


def _qualification(values: dict[str, float], latest_output: dict[str, Any] | None) -> tuple[bool, str]:
    checks = (
        (latest_output is not None and math.isclose(float(latest_output.get("output_double_register_35", math.nan)), 25.0, abs_tol=0.05), "stage_not_25"),
        (math.isclose(float(values.get("_step5d_stage25_echo_layout_tag", math.nan)), 524.0, abs_tol=1e-3), "echo_layout_not_524"),
        (float(values.get("_step5d_stage25_echo_cmd_valid", 0.0)) >= 0.5, "echo_cmd_invalid"),
        (float(values.get("_step5d_stage25_echo_consumed", 0.0)) >= 0.5, "tp_not_consumed"),
        (float(values.get("_step5d_p0_rnn_accepted", 0.0)) >= 0.5, "host_not_accepted"),
        (float(values.get("_step5d_p0_safe_hold_active", 1.0)) < 0.5, "safe_hold_active"),
        (float(values.get("_step5d_dls_shadow_normal_direction_class_difference", 1.0)) < 0.5, "dls_normal_direction_mismatch"),
    )
    for passed, reason in checks:
        if not passed:
            return False, reason
    return True, "qualified"


def _apply_no_contact_runtime_guards(
    state: base.BridgeState,
    latest_output: dict[str, Any] | None,
    values: dict[str, float],
    dt_s: float,
) -> str | None:
    if latest_output is None or state.step5d_p0_v9_approach_normal is None:
        return None
    pose = np.asarray(latest_output.get("actual_TCP_pose", ()), dtype=float)
    speed = np.asarray(latest_output.get("actual_TCP_speed", ()), dtype=float)
    normal = np.asarray(state.step5d_p0_v9_approach_normal, dtype=float)
    if pose.shape != (6,) or speed.shape != (6,) or not np.all(np.isfinite(pose)) or not np.all(np.isfinite(speed)):
        return "no_contact_actual_state_invalid"
    actual_normal_speed = float(np.dot(speed[:3], normal))
    values["_step5d_p0_v9_actual_normal_speed_m_s"] = actual_normal_speed
    if abs(actual_normal_speed) > P0_V9_ACTUAL_NORMAL_SPEED_STOP_M_S:
        state.p0_v9_normal_speed_violation_s = float(
            getattr(state, "p0_v9_normal_speed_violation_s", 0.0)
        ) + max(0.0, min(float(dt_s), 0.010))
    else:
        state.p0_v9_normal_speed_violation_s = 0.0
    values["_step5d_p0_v9_actual_normal_speed_violation_s"] = float(
        state.p0_v9_normal_speed_violation_s
    )
    anchor = state.step5d_p0_v9_anchor_tcp_pose
    if anchor is None:
        return None
    normal_displacement = float(np.dot(pose[:3] - np.asarray(anchor[:3]), normal))
    values["_step5d_p0_v9_anchor_normal_displacement_m"] = normal_displacement
    if abs(normal_displacement) > P0_V9_NORMAL_DISPLACEMENT_STOP_M:
        return "no_contact_anchor_normal_displacement_exceeded"
    if state.p0_v9_normal_speed_violation_s >= P0_V9_ACTUAL_NORMAL_SPEED_DWELL_S:
        return "no_contact_actual_normal_speed_dwell_exceeded"
    return None


def compute_bridge_values(
    args: Any,
    latest_zeroed: list[float],
    latest_output: dict[str, Any] | None,
    sensor_ok: float,
    state: base.BridgeState,
    dt_s: float,
) -> dict[str, float]:
    profile = str(getattr(args, "bridge_profile", ""))
    requested_phase_s = float(getattr(args, "step5d_stop_register_canary_s", 0.0))
    if profile != base.STEP5D_NO_CONTACT_P0_V9_STAGE_ID:
        return _base_compute_bridge_values(args, latest_zeroed, latest_output, sensor_ok, state, dt_s)

    args.step5d_stop_register_canary_s = 0.0
    try:
        values = _base_compute_bridge_values(args, latest_zeroed, latest_output, sensor_ok, state, dt_s)
    finally:
        args.step5d_stop_register_canary_s = requested_phase_s

    guard_reason = _apply_no_contact_runtime_guards(state, latest_output, values, dt_s)
    qualified, qualification_reason = _qualification(values, latest_output)
    if guard_reason is not None:
        qualified = False
        qualification_reason = guard_reason
    qualified_s = update_qualified_time(state, qualified=qualified, now_s=time.monotonic())
    values[QUALIFIED_FIELD] = qualified_s
    values[QUALIFIED_BOOL_FIELD] = 1.0 if qualified else 0.0
    values[QUALIFICATION_REASON_FIELD] = qualification_reason

    terminal = guard_reason is not None or (
        requested_phase_s > 0.0 and qualified and qualified_s >= requested_phase_s
    )
    if terminal:
        values.update(
            base.step5d_stage25_register_values(
                (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                layout_tag=base.STEP5D_STAGE25_JOINT_LAYOUT_CODE,
                cmd_valid=1.0,
                path_time_s=qualified_s,
                force_error_n=0.0,
                pose_or_orientation_error=0.0,
            )
        )
        values["stop_request"] = 1.0
        values["_step5d_p0_v9_canary_stop_active"] = 1.0
        values["_step5d_contact_safety_reason"] = (
            guard_reason or f"p0_v9_qualified_{requested_phase_s:g}s_complete"
        )
    return values


def main() -> int:
    for field in (
        QUALIFIED_FIELD,
        QUALIFIED_BOOL_FIELD,
        QUALIFICATION_REASON_FIELD,
        "_step5d_p0_v9_actual_normal_speed_m_s",
        "_step5d_p0_v9_actual_normal_speed_violation_s",
        "_step5d_p0_v9_canary_stop_active",
    ):
        if field not in base.STEP5D_DIAG_FIELDS:
            base.STEP5D_DIAG_FIELDS.append(field)
    base.compute_bridge_values = compute_bridge_values
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
