#!/usr/bin/env python3
"""P0 v9 bridge wrapper with strict qualification and no-contact guards."""

from __future__ import annotations

import math
import time
from typing import Any

import kunwei_rtde_bridge as base


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
    )
    for passed, reason in checks:
        if not passed:
            return False, reason
    return True, "qualified"


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

    qualified, qualification_reason = _qualification(values, latest_output)
    qualified_s = update_qualified_time(state, qualified=qualified, now_s=time.monotonic())
    values[QUALIFIED_FIELD] = qualified_s
    values[QUALIFIED_BOOL_FIELD] = 1.0 if qualified else 0.0
    values[QUALIFICATION_REASON_FIELD] = qualification_reason

    terminal = requested_phase_s > 0.0 and qualified and qualified_s >= requested_phase_s
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
        values["_step5d_contact_safety_reason"] = f"p0_v9_qualified_{requested_phase_s:g}s_complete"
    return values


def main() -> int:
    for field in (
        QUALIFIED_FIELD,
        QUALIFIED_BOOL_FIELD,
        QUALIFICATION_REASON_FIELD,
        "_step5d_p0_v9_canary_stop_active",
    ):
        if field not in base.STEP5D_DIAG_FIELDS:
            base.STEP5D_DIAG_FIELDS.append(field)
    base.compute_bridge_values = compute_bridge_values
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
