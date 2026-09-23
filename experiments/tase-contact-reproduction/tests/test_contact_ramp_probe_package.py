from __future__ import annotations

import json
from pathlib import Path
import re
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_contact_ramp_probe import (  # noqa: E402
    FORCE_NORM_PROBE_STOP_N,
    HOME_CONFIG,
    PROGRAM,
    RAMP_DURATIONS_S,
    _validate_home_input,
    build,
    numeric_sanity,
)
from figure8_home_config import CANONICAL_FIGURE8_HOME_POSE, CANONICAL_FIGURE8_HOME_Q  # noqa: E402


def test_probe_triplet_uses_canonical_figure8_tcp_and_joint_home(tmp_path):
    output = tmp_path / "probe"
    binding = build(HOME_CONFIG, output)
    assert binding["stamp"] == binding["version"]
    script = (output / f"{PROGRAM}.script").read_text(encoding="utf-8")
    home_script = (output / "step5d_contact_home_v1.script").read_text(encoding="utf-8")

    assert tuple(binding["home_pose_m_rad"]) == CANONICAL_FIGURE8_HOME_POSE
    assert tuple(binding["home_q_rad"]) == CANONICAL_FIGURE8_HOME_Q
    assert "local fixed_home_q = [0.7451654076576233, -1.8181091747679652" in script
    assert "local locked_home_q = fixed_home_q" in script
    assert "local arm_home_q = fixed_home_q" in script
    assert "# FIXED_HOME_Q_RAD: [0.7451654076576233, -1.8181091747679652" in script
    assert "local target_pose = p[0.462055181600, 0.177882596400" in home_script
    assert "preserved-home" not in json.dumps(binding)
    for name in (f"{PROGRAM}.script", f"{PROGRAM}.txt", f"{PROGRAM}.urp",
                 "step5d_contact_home_v1.script", "step5d_contact_home_v1.txt",
                 "step5d_contact_home_v1.urp"):
        assert (output / name).is_file()


def test_builder_rejects_old_or_modified_home(tmp_path):
    bad = json.loads(HOME_CONFIG.read_text(encoding="utf-8"))
    bad["joint_positions_rad"] = [0.6957, -1.7861, -2.5, -0.4, 1.5, -0.8]
    bad_path = tmp_path / "old-preserved-home.json"
    bad_path.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(ValueError, match="Figure-eight canonical Home joints"):
        _validate_home_input(bad_path)


def test_probe_rungs_have_exact_slope_and_guarded_numeric_sanity():
    sanity = numeric_sanity()
    assert tuple(sanity["ramp_durations_s"]) == RAMP_DURATIONS_S
    assert sanity["probe_stop"]["force_norm_n"] == FORCE_NORM_PROBE_STOP_N == 10.0
    assert sanity["release"]["path_commanded"] is False
    assert sanity["release"]["existing_gate_hold_s"] == 0.5
    assert sanity["release"]["post_5n_observation_max_s"] == 2.0
    for rung in sanity["rungs"]:
        assert rung["slope_n_s"] == pytest.approx(4.0 / rung["duration_s"])
        assert rung["setpoint_start_n"] == 1.0
        assert rung["setpoint_target_n"] == 5.0
        assert rung["nominal_delta_n_at_500_hz"] <= rung["tp_max_delta_at_80ms_n"]


def test_generated_tp_is_probe_only_and_has_force_norm_stop_in_recovery_too(tmp_path):
    output = tmp_path / "probe"
    build(HOME_CONFIG, output)
    script = (output / f"{PROGRAM}.script").read_text(encoding="utf-8")
    assert "kind != 1 or (probe_ramp_duration_s" in script
    assert "read_input_integer_register(34) != probe_ramp_duration_s" in script
    assert "(4.0 / probe_ramp_duration_s) * 0.080000000 + 0.010000000" in script
    assert "codex_r006_packet_guard(packet_reason, 20.0, 10.0, 2.0)" in script
    assert "codex_r006_recovery_packet_guard(packet_reason, 20.0, 10.0, 2.0)" in script
    assert not re.search(r"codex_r006_(?:recovery_)?packet_guard\(packet_reason, 20\.0, 20\.0, 2\.0\)", script)
    assert "local force_fuse_n = 20.000000000" in script
    assert "# CONTACT_CAPS: qdot<=0.05rad/s; speedj_accel=5rad/s2; force_norm<10N; torque_norm<2Nm" in script
    assert (output / f"{PROGRAM}.urp").stat().st_size > 0
