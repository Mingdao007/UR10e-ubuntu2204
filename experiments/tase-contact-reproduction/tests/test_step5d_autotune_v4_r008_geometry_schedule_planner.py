"""Geometry calibration + contact-search schedule planner unit tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from step5d_autotune_v4_r004.home_profile import FIXED_HOME_POSE
from step5d_autotune_v4_r008.b3_identity import (
    MAINLINE_FINGERPRINT,
    compute_b3_campaign_fingerprint,
)
from step5d_autotune_v4_r008.contact_search_schedule import (
    FAR_SPEED_M_S,
    load_schedule,
    validate_planned_schedule,
    validate_schedule,
)
from step5d_autotune_v4_r008.geometry_calibration import (
    DEFAULT_CALIBRATION_PATH,
    PROVENANCE_OPERATOR_SEALED,
    PROVENANCE_UNSET,
    PROVENANCE_V3_PRIOR_UNTRUSTED,
    PROVENANCE_WAVE1_STATE20_SEALED,
    GeometryCalibrationError,
    build_geometry_calibration,
    load_geometry_calibration,
    validate_geometry_calibration,
)
from step5d_autotune_v4_r008.schedule_planner import (
    NEAR_MARGIN_M,
    WAVE2_V_FAR_M_S,
    SchedulePlannerError,
    plan_contact_search_schedule,
    plan_demo_from_delta_z,
)


ROOT = Path(__file__).resolve().parents[1]


def _contact_at_delta(delta_z_m: float) -> tuple[float, ...]:
    return (
        FIXED_HOME_POSE[0],
        FIXED_HOME_POSE[1],
        FIXED_HOME_POSE[2] - delta_z_m,
        FIXED_HOME_POSE[3],
        FIXED_HOME_POSE[4],
        FIXED_HOME_POSE[5],
    )


def test_default_calibration_on_disk_is_wave1_state20_sealed() -> None:
    calibration = load_geometry_calibration()
    assert calibration.provenance == PROVENANCE_WAVE1_STATE20_SEALED
    assert calibration.has_contact
    assert calibration.allows_speed_opt
    assert calibration.home_pose_m_rad == FIXED_HOME_POSE
    assert calibration.delta_z_m is not None
    assert 0.012 < float(calibration.delta_z_m) < 0.015
    assert DEFAULT_CALIBRATION_PATH.is_file()


def test_missing_contact_rejects_speed_opt() -> None:
    calibration = build_geometry_calibration(provenance=PROVENANCE_UNSET)
    with pytest.raises(SchedulePlannerError, match="sealed contact"):
        plan_contact_search_schedule(calibration)


def test_v3_prior_untrusted_rejects_speed_opt() -> None:
    calibration = build_geometry_calibration(
        contact_pose_m_rad=_contact_at_delta(0.010136481),
        provenance=PROVENANCE_V3_PRIOR_UNTRUSTED,
        notes={"source": "v3 precontact-derived demo"},
    )
    assert calibration.has_contact
    assert not calibration.allows_speed_opt
    with pytest.raises(SchedulePlannerError, match="v3_prior_untrusted"):
        plan_contact_search_schedule(calibration)


def test_delta_z_bounds_reject_too_shallow() -> None:
    calibration = build_geometry_calibration(
        contact_pose_m_rad=_contact_at_delta(0.002),
        provenance=PROVENANCE_OPERATOR_SEALED,
    )
    with pytest.raises(SchedulePlannerError, match="near_margin"):
        plan_contact_search_schedule(calibration)


def test_delta_z_bounds_reject_at_or_beyond_max_travel() -> None:
    calibration = build_geometry_calibration(
        contact_pose_m_rad=_contact_at_delta(0.025),
        provenance=PROVENANCE_OPERATOR_SEALED,
    )
    with pytest.raises(SchedulePlannerError, match="max_travel"):
        plan_contact_search_schedule(calibration)


def test_plan_derives_d_near_start_before_contact() -> None:
    delta_z = 0.0055
    planned = plan_demo_from_delta_z(delta_z)
    expected_start = max(0.002, delta_z - NEAR_MARGIN_M)
    assert planned.d_near_start_travel_m == pytest.approx(expected_start)
    assert planned.schedule.d_near_start_travel_m == pytest.approx(expected_start)
    assert planned.d_near_start_travel_m < delta_z
    assert planned.schedule.v_far_m_s > planned.schedule.v_near_m_s
    assert planned.schedule.v_far_m_s <= 0.006
    assert planned.schedule.force_fuse_n == pytest.approx(50.0)
    # NEAR engages before expected touch
    assert planned.delta_z_m - planned.d_near_start_travel_m == pytest.approx(NEAR_MARGIN_M)


def test_fingerprint_changes_when_schedule_geometry_changes() -> None:
    a = plan_demo_from_delta_z(0.0055)
    b = plan_demo_from_delta_z(0.0080)
    assert a.campaign_fingerprint != b.campaign_fingerprint
    assert a.campaign_fingerprint != MAINLINE_FINGERPRINT
    assert b.campaign_fingerprint != MAINLINE_FINGERPRINT
    assert a.campaign_fingerprint == compute_b3_campaign_fingerprint(
        parent_fingerprint=MAINLINE_FINGERPRINT,
        schedule=a.schedule,
    )
    wave1 = load_schedule(
        ROOT / "config/step5d/autotune_v4_r008_contact_search_schedule_wave1_observation.json"
    )
    assert a.campaign_fingerprint != compute_b3_campaign_fingerprint(
        parent_fingerprint=MAINLINE_FINGERPRINT,
        schedule=wave1,
    )


def test_wave1_archive_still_pinned() -> None:
    schedule = load_schedule(
        ROOT / "config/step5d/autotune_v4_r008_contact_search_schedule_wave1_observation.json"
    )
    assert schedule.v_far_m_s == pytest.approx(FAR_SPEED_M_S)
    raw = dict(schedule.raw)
    raw["v_far_m_s"] = 0.002
    with pytest.raises(Exception, match="v_far_m_s"):
        validate_schedule(raw)


def test_default_schedule_is_wave4_far005() -> None:
    from step5d_autotune_v4_r008.schedule_planner import WAVE4_V_FAR_M_S

    schedule = load_schedule()
    assert schedule.version.startswith("b3-geometry-planned-wave4-far005")
    assert schedule.v_far_m_s == pytest.approx(WAVE4_V_FAR_M_S)
    assert schedule.v_far_m_s == pytest.approx(0.005)
    assert schedule.v_near_m_s == pytest.approx(0.0005)
    assert schedule.d_near_start_travel_m > 0.01


def test_planned_schedule_validator_accepts_planner_output() -> None:
    planned = plan_demo_from_delta_z(0.0055)
    again = validate_planned_schedule(planned.document)
    assert again.as_dict() == planned.schedule.as_dict()


def test_xy_mismatch_rejected() -> None:
    bad = (
        FIXED_HOME_POSE[0] + 0.005,
        FIXED_HOME_POSE[1],
        FIXED_HOME_POSE[2] - 0.0055,
        FIXED_HOME_POSE[3],
        FIXED_HOME_POSE[4],
        FIXED_HOME_POSE[5],
    )
    with pytest.raises(GeometryCalibrationError, match="contact XY"):
        build_geometry_calibration(
            contact_pose_m_rad=bad,
            provenance=PROVENANCE_OPERATOR_SEALED,
        )


def test_seal_tamper_detected() -> None:
    calibration = build_geometry_calibration(
        contact_pose_m_rad=_contact_at_delta(0.0055),
        provenance=PROVENANCE_OPERATOR_SEALED,
    )
    document = calibration.as_dict()
    document["delta_z_m"] = 0.006
    with pytest.raises(GeometryCalibrationError, match="seal|delta_z"):
        validate_geometry_calibration(document)


def test_cli_demo_json(tmp_path: Path) -> None:
    from plan_step5d_autotune_v4_r008_contact_search_from_geometry import main

    out = tmp_path / "planned_schedule.json"
    rc = main(["--demo-delta-z-m", "0.0055", "--write-schedule", str(out)])
    assert rc == 0
    assert out.is_file()
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["version"].startswith("b3-geometry-planned")
    assert document["notes"]["delta_z_m"] == pytest.approx(0.0055)
