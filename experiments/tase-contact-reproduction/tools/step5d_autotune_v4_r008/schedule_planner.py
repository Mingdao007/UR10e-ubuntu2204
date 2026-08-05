"""Plan B3 FAR/NEAR contact-search speeds from sealed geometry calibration.

Unknown / untrusted contact → refuse speed-opt (calibrate first).
Known Δz → distance-primary NEAR band before expected touch; v_far capped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from .b3_identity import compute_b3_campaign_fingerprint
from .contact_search_schedule import (
    FORCE_FUSE_REASON,
    F_FAR_N,
    MAINLINE_FINGERPRINT,
    MAX_TRAVEL_M,
    PLANNED_CONFIRM_FORCE_NORM_N,
    PLANNED_CONFIRM_HOLD_S,
    PLANNED_CONFIRM_NORMAL_N,
    PLANNED_FAR_ACCEL_MAX_M_S2,
    PLANNED_NEAR_ACCEL_M_S2,
    PLANNED_V_FAR_MAX_M_S,
    PLANNED_V_NEAR_MAX_M_S,
    PROGRAM_B3,
    SCHEDULE_SCHEMA,
    ContactSearchSchedule,
    ContactSearchScheduleError,
    validate_planned_schedule,
)
from .geometry_calibration import (
    PROVENANCE_V3_PRIOR_UNTRUSTED,
    GeometryCalibration,
    GeometryCalibrationError,
)

NEAR_MARGIN_M = 0.0025
MIN_D_NEAR_START_TRAVEL_M = 0.002
V_NEAR_DEFAULT_M_S = 0.0005
V_FAR_MULTIPLIER = 4.0
WAVE1_V_FAR_REF_M_S = 0.0008
WAVE1_FAR_ACCEL_REF_M_S2 = 0.05
SPEED_EFFICIENCY = 0.8  # Wave1 reissue/ramp tax
PLANNED_VERSION = "b3-geometry-planned-wave3-far-cap-v1"
WAVE2_V_FAR_M_S = 0.0016  # Wave-2 observation FAR (archived)
WAVE3_V_FAR_M_S = 0.002  # planner hard cap (PLANNED_V_FAR_MAX_M_S)
MAX_FORCE_FUSE_N = 50.0
DEFAULT_TIMEOUT_S = 90.0


class SchedulePlannerError(ValueError):
    """Geometry → schedule planning refused or unsafe."""


@dataclass(frozen=True)
class PlannedContactSearch:
    """Planner output: validated schedule + identity + time estimate."""

    schedule: ContactSearchSchedule
    campaign_fingerprint: str
    delta_z_m: float
    d_near_start_travel_m: float
    near_margin_m: float
    estimated_search_s: float
    document: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schedule": self.schedule.as_dict(),
            "campaign_fingerprint": self.campaign_fingerprint,
            "delta_z_m": self.delta_z_m,
            "d_near_start_travel_m": self.d_near_start_travel_m,
            "near_margin_m": self.near_margin_m,
            "estimated_search_s": self.estimated_search_s,
            "parent_campaign_fingerprint": MAINLINE_FINGERPRINT,
            "notes": {
                "return_home_is_not_second_search": (
                    "TP return_home (+5mm rise then descend to home) is not a "
                    "second contact search"
                ),
                "host_abs_normal_n": 60.0,
                "speed_efficiency_assumed": SPEED_EFFICIENCY,
            },
        }


def _far_accel_for_speed(v_far_m_s: float) -> float:
    """Scale FAR accel down when v_far rises above the Wave-1 reference."""

    if v_far_m_s <= WAVE1_V_FAR_REF_M_S:
        return WAVE1_FAR_ACCEL_REF_M_S2
    scaled = WAVE1_FAR_ACCEL_REF_M_S2 * (WAVE1_V_FAR_REF_M_S / v_far_m_s)
    return min(PLANNED_FAR_ACCEL_MAX_M_S2, max(0.01, scaled))


def plan_contact_search_schedule(
    calibration: GeometryCalibration,
    *,
    v_near_m_s: float = V_NEAR_DEFAULT_M_S,
    near_margin_m: float = NEAR_MARGIN_M,
    max_travel_m: float = MAX_TRAVEL_M,
    v_far_cap_m_s: float = WAVE3_V_FAR_M_S,
) -> PlannedContactSearch:
    """Map sealed calibration Δz → FAR/NEAR schedule (speed-opt path)."""

    if not calibration.allows_speed_opt:
        if calibration.provenance == PROVENANCE_V3_PRIOR_UNTRUSTED:
            raise SchedulePlannerError(
                "v3_prior_untrusted cannot drive speed-opt; re-seal contact "
                "via observation/calibration first"
            )
        if not calibration.has_contact:
            raise SchedulePlannerError(
                "sealed contact_pose_m_rad missing; run calibration / "
                "observation before speed-opt planning"
            )
        raise SchedulePlannerError(
            f"provenance {calibration.provenance!r} does not allow speed-opt"
        )

    assert calibration.delta_z_m is not None
    delta_z = float(calibration.delta_z_m)
    if near_margin_m <= 0.0 or not math.isfinite(near_margin_m):
        raise SchedulePlannerError("near_margin_m must be positive and finite")
    if delta_z <= near_margin_m:
        raise SchedulePlannerError(
            f"delta_z_m ({delta_z}) must be > near_margin_m ({near_margin_m})"
        )
    if delta_z >= max_travel_m:
        raise SchedulePlannerError(
            f"delta_z_m ({delta_z}) must be < max_travel_m ({max_travel_m})"
        )

    if v_near_m_s <= 0.0 or v_near_m_s > PLANNED_V_NEAR_MAX_M_S:
        raise SchedulePlannerError(
            f"v_near_m_s must be in (0, {PLANNED_V_NEAR_MAX_M_S}]"
        )

    d_near_start = round(max(MIN_D_NEAR_START_TRAVEL_M, delta_z - near_margin_m), 9)
    if d_near_start >= delta_z:
        raise SchedulePlannerError(
            "d_near_start_travel_m must leave a positive near band before contact"
        )
    d_near_m = round(max_travel_m - d_near_start, 9)
    v_far = round(min(v_far_cap_m_s, V_FAR_MULTIPLIER * v_near_m_s), 9)
    if v_far <= v_near_m_s:
        raise SchedulePlannerError("computed v_far_m_s must exceed v_near_m_s")
    far_accel = round(_far_accel_for_speed(v_far), 9)

    d_far = d_near_start
    d_near_band = round(delta_z - d_near_start, 9)
    v_far_eff = SPEED_EFFICIENCY * v_far
    v_near_eff = SPEED_EFFICIENCY * v_near_m_s
    estimated = round(
        d_far / v_far_eff + d_near_band / v_near_eff + PLANNED_CONFIRM_HOLD_S,
        6,
    )

    document: dict[str, Any] = {
        "schema": SCHEDULE_SCHEMA,
        "version": PLANNED_VERSION,
        "program": PROGRAM_B3,
        "parent_campaign_fingerprint": MAINLINE_FINGERPRINT,
        "max_travel_m": max_travel_m,
        "timeout_s": DEFAULT_TIMEOUT_S,
        "v_far_m_s": v_far,
        "v_near_m_s": v_near_m_s,
        "far_acceleration_m_s2": far_accel,
        "near_acceleration_m_s2": PLANNED_NEAR_ACCEL_M_S2,
        "d_near_m": d_near_m,
        "F_far_n": F_FAR_N,
        "force_fuse_n": MAX_FORCE_FUSE_N,
        "force_fuse_reason": FORCE_FUSE_REASON,
        "confirm_normal_n": PLANNED_CONFIRM_NORMAL_N,
        "confirm_force_norm_n": PLANNED_CONFIRM_FORCE_NORM_N,
        "confirm_hold_s": PLANNED_CONFIRM_HOLD_S,
        "notes": {
            "wave": (
                "B3 Wave 3 — geometry-planned FAR at planner cap "
                f"(v_far={v_far}), NEAR frozen at Wave1"
            ),
            "d_near_start_travel_m": d_near_start,
            "delta_z_m": delta_z,
            "near_margin_m": near_margin_m,
            "calibration_seal_sha256": calibration.seal_sha256,
            "calibration_provenance": calibration.provenance,
            "v_far_policy": (
                f"capped at {v_far_cap_m_s} (Wave3 default {WAVE3_V_FAR_M_S}; "
                f"Wave2 archive {WAVE2_V_FAR_M_S})"
            ),
            "host_abs_normal_n_frozen": 60.0,
            "mainline_forbidden": "never resume into live_20260803_1113_stage_d / 1db4f9bf",
        },
    }
    try:
        schedule = validate_planned_schedule(document)
    except ContactSearchScheduleError as exc:
        raise SchedulePlannerError(str(exc)) from exc

    fingerprint = compute_b3_campaign_fingerprint(
        parent_fingerprint=MAINLINE_FINGERPRINT,
        schedule=schedule,
    )
    return PlannedContactSearch(
        schedule=schedule,
        campaign_fingerprint=fingerprint,
        delta_z_m=round(delta_z, 9),
        d_near_start_travel_m=d_near_start,
        near_margin_m=near_margin_m,
        estimated_search_s=estimated,
        document=document,
    )


def plan_demo_from_delta_z(
    delta_z_m: float,
    *,
    provenance: str = "operator_sealed",
    v_far_cap_m_s: float = WAVE3_V_FAR_M_S,
) -> PlannedContactSearch:
    """Demo helper: seal a synthetic contact at home_z - delta_z (not disk-sealed)."""

    from step5d_autotune_v4_r004.home_profile import FIXED_HOME_POSE

    from .geometry_calibration import (
        PROVENANCE_OPERATOR_SEALED,
        build_geometry_calibration,
    )

    if provenance != PROVENANCE_OPERATOR_SEALED:
        raise SchedulePlannerError("demo helper only seals operator_sealed synthetics")
    if delta_z_m <= 0.0 or not math.isfinite(delta_z_m):
        raise SchedulePlannerError("delta_z_m must be positive and finite")
    delta = round(float(delta_z_m), 12)
    contact = (
        FIXED_HOME_POSE[0],
        FIXED_HOME_POSE[1],
        round(FIXED_HOME_POSE[2] - delta, 12),
        FIXED_HOME_POSE[3],
        FIXED_HOME_POSE[4],
        FIXED_HOME_POSE[5],
    )
    try:
        calibration = build_geometry_calibration(
            contact_pose_m_rad=contact,
            provenance=PROVENANCE_OPERATOR_SEALED,
            notes={
                "demo": True,
                "purpose": "formula demonstration only; not a sealed bench calibration",
            },
        )
    except GeometryCalibrationError as exc:
        raise SchedulePlannerError(str(exc)) from exc
    return plan_contact_search_schedule(
        calibration, v_far_cap_m_s=v_far_cap_m_s
    )


__all__ = [
    "NEAR_MARGIN_M",
    "PLANNED_VERSION",
    "PlannedContactSearch",
    "SchedulePlannerError",
    "SPEED_EFFICIENCY",
    "V_NEAR_DEFAULT_M_S",
    "WAVE2_V_FAR_M_S",
    "WAVE3_V_FAR_M_S",
    "plan_contact_search_schedule",
    "plan_demo_from_delta_z",
]
