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
    assert sanity["delta_class"] == "identity_plus_precontact_pose_and_clearance"
    assert sanity["precontact_pose_prior_id"] == "step5d_v3_physical_prior_contact_0p1_20260719"
    assert sanity["precontact_xyz_m"] == [0.487834547, 0.129337053, 0.022863519]
    assert sanity["precontact_rotvec_rad"] == [3.120752062, 0.0, 0.068626833]
    assert sanity["reaction_normal_b"] == [-0.043955267, 0.020079909, 0.998831683]
    assert sanity["approach_axis_b"] == [0.043955267, -0.020079909, -0.998831683]
    assert sanity["precontact_clearance_m"] == 0.005
    assert sanity["minimum_start_above_entry_m"] == 0.01
    assert sanity["qdot_cap_rad_s"] == 0.5
    assert sanity["precontact_entry_speed_m_s"] == 0.09
