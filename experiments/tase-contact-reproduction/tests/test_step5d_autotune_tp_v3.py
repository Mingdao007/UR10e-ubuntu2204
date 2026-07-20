from __future__ import annotations

import gzip
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_tp as v1  # noqa: E402
import build_step5d_autotune_tp_v3 as v3  # noqa: E402


def test_v3_script_has_one_evidence_bound_precontact_pose_delta() -> None:
    rendered = v3.render_script()
    v3.validate_rendered_script(rendered)
    assert "# CONTROL_PROFILE_ID: step5d_strict_rnn_autotune_v1" in rendered
    assert "def codex_step5d_autotune_trial_v1(" in rendered
    assert "def codex_step5d_strict_rnn_autotune_v3():" in rendered
    assert rendered.count("read_input_integer_register(26)") >= 2
    assert hashlib.sha256(v1.render_script().encode()).hexdigest() in rendered
    assert "# PRECONTACT_POSE_PRIOR_ID: step5d_v3_physical_prior_contact_0p1_20260719" in rendered
    assert "# PHYSICAL_PRIOR_SHA256: c8019aee2c293746e1edb23097aeab1d7dfb1b8dee09df10ce568fb634f47c9f" in rendered
    assert "local entry_x = 0.487834547" in rendered
    assert "local entry_y = 0.129337053" in rendered
    assert "local precontact_z = 0.022863519" in rendered
    assert "local target_rx = 3.120752062" in rendered
    assert "local target_ry = 0.000000000" in rendered
    assert "local target_rz = 0.068626833" in rendered
    assert "local entry_xy_pose = p[entry_x, entry_y, p_current[2]" in rendered
    assert "local entry_precontact_pose = p[entry_x, entry_y, precontact_z" in rendered
    assert "if p_current[2] < precontact_z + minimum_start_above_entry_m:" in rendered
    assert "movel(entry_precontact_pose, a=0.060, v=0.040, r=0.0)" in rendered
    assert "if batch_row_index > 1:" in rendered
    assert "elif p_current[2] < precontact_z + minimum_start_above_entry_m:" in rendered
    assert "read_input_integer_register(30) == batch_row_index" in rendered
    assert "codex_autotune_bounded_return_segment(rise_pose, 0.060, 0.040, 1.0)" in rendered
    assert "codex_autotune_bounded_return_segment(transfer_pose, 0.135, 0.090, 2.0)" in rendered
    assert "codex_autotune_bounded_return_segment(target_pose, 0.060, 0.040, 3.0)" in rendered
    assert "speedl([vx, vy, vz, wx, wy, wz], linear_accel_m_s2, 0.002, aRot=0.100)" in rendered
    assert "start_angle_rad > 0.349065850" in rendered
    assert "angular_speed_rad_s > 0.060" in rendered
    assert "angular_accel_rad_s2 > 0.500" in rendered
    assert "stopl(0.3, 0.100)" in rendered
    assert "write_output_float_register(39, codex_autotune_return_segment_id)" in rendered
    assert "write_output_float_register(44, codex_autotune_return_max_sample_gap_s)" in rendered
    assert "movel(campaign_home_pose, a=0.030, v=0.050, r=0.0)" not in rendered
    assert "codex_autotune_write_state(campaign_epoch, trial_id, 76" in rendered
    assert "codex_autotune_write_state(campaign_epoch, trial_id, 77" in rendered
    assert "write_output_integer_register(33, codex_autotune_return_guard_mask)" in rendered
    assert "def codex_autotune_certification_stop(" in rendered
    assert "def codex_autotune_certification_return(" in rendered
    assert "command == 4" in rendered
    assert "command == 5" in rendered
    assert "command == 6" in rendered
    assert "certification_profile_id != 9001" in rendered
    assert "write_output_float_register(45, trigger_controller_time_s)" in rendered
    assert "write_output_float_register(46, stop_transport_controller_time_s)" in rendered
    assert "write_output_float_register(47, codex_autotune_controller_time_s())" in rendered
    assert "if stale_s2 > 0.020:" in rendered
    assert "if stale_s2 > 1.000:" not in rendered
    certification_stop = rendered.split(
        "def codex_autotune_certification_stop(", 1
    )[1].split("def codex_autotune_certification_return(", 1)[0]
    assert certification_stop.index(
        "codex_autotune_write_state(campaign_epoch, trial_id, 80"
    ) < certification_stop.index(
        "local last_controller_time_s = codex_autotune_controller_time_s()"
    ) < certification_stop.index("while trigger_controller_time_s < 0.0:")


def test_v3_triplet_has_exact_program_cache_and_stamp(tmp_path: Path) -> None:
    stamp = v3.source_stamp(
        datetime(2026, 7, 19, 0, 30, tzinfo=timezone(timedelta(hours=8)))
    )
    result = v3.write_triplet(tmp_path, stamp)
    assert result["program"] == "step5d_strict_rnn_autotune_v3"
    assert result["control_profile_id"] == "step5d_strict_rnn_autotune_v1"
    xml = gzip.decompress(
        (tmp_path / "step5d_strict_rnn_autotune_v3.urp").read_bytes()
    ).decode()
    assert 'name="step5d_strict_rnn_autotune_v3"' in xml
    assert "/programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v3.script" in xml
    assert stamp in xml
    assert result["checks"]["cached script"] is True
    sanity = json.loads(
        (tmp_path / "step5d_strict_rnn_autotune_v3.numeric-sanity.json").read_text()
    )
    assert sanity["delta_class"] == (
        "identity_precontact_prior_exact_batch_lifecycle_return_"
        "angular_envelope_stage25_watchdog_ticketed_certification_v3"
    )
    assert sanity["precontact_pose_prior_id"] == "step5d_v3_physical_prior_contact_0p1_20260719"
    assert sanity["precontact_xyz_m"] == [0.487834547, 0.129337053, 0.022863519]
    assert sanity["precontact_rotvec_rad"] == [3.120752062, 0.0, 0.068626833]
    assert sanity["reaction_normal_b"] == [-0.043955267, 0.020079909, 0.998831683]
    assert sanity["approach_axis_b"] == [0.043955267, -0.020079909, -0.998831683]
    assert sanity["physical_prior_sha256"] == (
        "c8019aee2c293746e1edb23097aeab1d7dfb1b8dee09df10ce568fb634f47c9f"
    )
    assert sanity["precontact_clearance_m"] == 0.005
    assert sanity["minimum_start_above_entry_m"] == 0.01
    assert sanity["qdot_cap_rad_s"] == 0.5
    assert sanity["stage25_stale_command_hold_s"] == 0.020
    assert sanity["precontact_entry_speed_m_s"] == 0.09
    assert sanity["input_integer_registers"] == list(range(24, 31))
    assert sanity["output_integer_registers"] == list(range(24, 34))
    assert sanity["safe_transfer_z_m"] == 0.033
    assert sanity["return_segment_count"] == 3
    assert sanity["return_controller"] == "speedl_bounded_twist_v1"
    assert sanity["return_angular_speed_limit_rad_s"] == 0.05
    assert sanity["return_angular_acceleration_limit_rad_s2"] == 0.1
    assert sanity["return_angular_speed_guard_rad_s"] == 0.06
    assert sanity["return_angular_acceleration_guard_rad_s2"] == 0.5
    assert sanity["return_angular_stop_deceleration_rad_s2"] == 0.1
    assert sanity["return_controller_period_s"] == 0.002
    assert sanity["return_sample_gap_clock"] == "controller_monotonic_time_mode_0"
    assert sanity["return_controller_max_sample_gap_s"] == 0.004
    assert sanity["return_segment_phase_codes"] == [40.1, 40.2, 40.3]
    assert sanity["return_continuous_telemetry_output_float_registers"] == list(
        range(39, 45)
    )
    assert sanity["certification_commands"] == {
        "direct_exact_stop": 4,
        "stale_watchdog_exact_stop": 5,
        "return_route": 6,
    }
    assert sanity["certification_execution_profile_id"] == 9001
    assert sanity["certification_samples_per_stop_procedure"] == 3
    assert sanity["certification_safe_z_min_m"] == 0.033
    assert sanity["certification_excursion_m"] == 0.004
    assert sanity["certification_linear_speed_m_s"] == 0.01
    assert sanity["certification_linear_acceleration_m_s2"] == 0.06
    assert sanity["certification_stop_telemetry_output_float_registers"] == [45, 46, 47]
    assert sanity["batch_row_policy"] == (
        "rows_1_to_9_near_ready_row_10_campaign_home"
    )
