#!/usr/bin/env python3
"""P0 v8-only bridge entrypoint with a consumption-qualified canary clock."""

from __future__ import annotations

import math
import time
from typing import Any

import kunwei_rtde_bridge as base


QUALIFIED_FIELD = "_step5d_p0_v8_qualified_consumed_s"
_base_compute_bridge_values = base.compute_bridge_values


def update_qualified_consumed_time(
    state: base.BridgeState,
    *,
    consumed: bool,
    now_s: float,
) -> float:
    """Track one uninterrupted monotonic interval of TP-consumed ticks."""

    prior = getattr(state, "p0_v8_last_consumed_time_s", None)
    if not consumed:
        state.p0_v8_qualified_consumed_s = 0.0
        state.p0_v8_last_consumed_time_s = None
        return 0.0
    if prior is None:
        state.p0_v8_qualified_consumed_s = 0.0
    else:
        state.p0_v8_qualified_consumed_s = float(
            getattr(state, "p0_v8_qualified_consumed_s", 0.0)
        ) + max(0.0, now_s - float(prior))
    state.p0_v8_last_consumed_time_s = now_s
    return float(state.p0_v8_qualified_consumed_s)


def compute_bridge_values(
    args: Any,
    latest_zeroed: list[float],
    latest_output: dict[str, Any] | None,
    sensor_ok: float,
    state: base.BridgeState,
    dt_s: float,
) -> dict[str, float]:
    """Disable the legacy raw-stage timer and apply the P0-qualified timer."""

    profile = str(getattr(args, "bridge_profile", ""))
    requested_phase_s = float(getattr(args, "step5d_stop_register_canary_s", 0.0))
    if profile != base.STEP5D_NO_CONTACT_P0_V8_STAGE_ID:
        return _base_compute_bridge_values(
            args, latest_zeroed, latest_output, sensor_ok, state, dt_s
        )

    args.step5d_stop_register_canary_s = 0.0
    try:
        values = _base_compute_bridge_values(
            args, latest_zeroed, latest_output, sensor_ok, state, dt_s
        )
    finally:
        args.step5d_stop_register_canary_s = requested_phase_s

    values["_step5d_p0_v8_canary_phase_s"] = requested_phase_s
    values["_step5d_p0_v8_canary_stop_active"] = 0.0
    line_stage_active = bool(
        latest_output is not None
        and math.isclose(
            float(latest_output.get("output_double_register_35", math.nan)),
            25.0,
            abs_tol=0.05,
        )
    )
    qualified_s = update_qualified_consumed_time(
        state,
        consumed=(
            line_stage_active
            and float(values.get("_step5d_stage25_echo_consumed", 0.0)) >= 0.5
        ),
        now_s=time.monotonic(),
    )
    values[QUALIFIED_FIELD] = qualified_s
    if requested_phase_s > 0.0 and line_stage_active and qualified_s >= requested_phase_s:
        values.update(
            base.step5d_stage25_register_values(
                (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                layout_tag=base.STEP5D_STAGE25_JOINT_LAYOUT_CODE,
                cmd_valid=1.0,
                path_time_s=qualified_s,
                force_error_n=float(values.get("step4e_force_error_n", 0.0)),
                pose_or_orientation_error=float(
                    values.get("step4e_orientation_error_rad", 0.0)
                ),
            )
        )
        values["stop_request"] = 1.0
        values["_step5d_p0_v8_canary_stop_active"] = 1.0
        values["_step5d_contact_safety_reason"] = (
            f"p0_v8_canary_{requested_phase_s:g}s_complete"
        )
    return values


def main() -> int:
    if QUALIFIED_FIELD not in base.STEP5D_DIAG_FIELDS:
        base.STEP5D_DIAG_FIELDS.append(QUALIFIED_FIELD)
    base.compute_bridge_values = compute_bridge_values
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
