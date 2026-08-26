from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step6_figure8_start_home_v1 as builder  # noqa: E402


STAMP = "2026-08-16T0000HKT_STEP6_FIGURE8_START_HOME_V1"


def test_calibration_only_home_is_vertical_first_bounded_and_offline_closed(tmp_path: Path) -> None:
    below = builder.route_geometry((0.450, 0.100, 0.020, 3.000, 0.100, 0.000))
    above = builder.route_geometry((0.450, 0.100, 0.050, 3.000, 0.100, 0.000))
    assert tuple(below["target_pose"]) == builder.TARGET_POSE
    assert below["safe_transfer_z_m"] == builder.TARGET_POSE[2]
    assert below["segments"][0]["kind"] == "vertical_current_xy_current_orientation"
    assert below["segments"][2]["included"] is False
    assert above["safe_transfer_z_m"] == 0.050
    assert above["segments"][0]["span_m"] == 0.0
    assert above["segments"][2]["included"] is True

    script = builder.build_package_script(STAMP)
    main_start = script.index(f"def {builder.PROGRAM_NAME}():")
    assert script[main_start:].splitlines()[1].strip() == "local current_pose = get_actual_tcp_pose()"
    target = "local target_pose = p[0.4620551816, 0.1778825964, 0.0345000000, 3.1207520620, 0.0000000000, 0.0686268330]"
    assert target in script
    rise = script.index("movel(rise_pose, a=0.060, v=0.040, r=0.0)")
    transfer = script.index("movel(transfer_pose, a=0.135, v=0.090, r=0.0)")
    descent = script.index("movel(descent_pose, a=0.060, v=0.040, r=0.0)")
    verify = script.rindex("codex_step6_start_home_final_stationary_verified(target_pose)")
    assert rise < transfer < descent < verify
    assert builder.SEGMENT_1_SPEED_M_S <= builder.MOTION_SPEED_MAX_M_S
    assert builder.SEGMENT_2_SPEED_M_S <= builder.MOTION_SPEED_MAX_M_S
    assert builder.SEGMENT_1_ACCEL_M_S2 <= builder.MOTION_ACCEL_MAX_M_S2
    assert builder.SEGMENT_2_ACCEL_M_S2 <= builder.MOTION_ACCEL_MAX_M_S2
    assert "get_actual_tcp_speed()" in script
    assert "get_actual_joint_speeds()" in script
    assert script.count("movel(") == 3
    assert script.count("stopl(0.1)") == 3
    assert all(token not in script.lower() for token in builder.FORBIDDEN_SCRIPT_TOKENS)
    assert "# HOME_STATUS: calibration-only Home" in script

    generated = builder.write_triplet(tmp_path, STAMP)
    assert all(generated["checks"].values())
    checked = builder.check_triplet(tmp_path, STAMP)
    assert checked["pass"] is True
    manifest = json.loads(
        (tmp_path / f"{builder.PROGRAM_NAME}.deploy-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["identity"]["home_status"] == "calibration_only_pending_contact_confirmation"
    assert manifest["identity"]["contact_derived_final_home_claimed"] is False
    assert manifest["motion_contract"]["vertical_first"] is True
    assert manifest["motion_contract"]["figure8_traversal"] is False
    assert manifest["live_acceptance"] is False
