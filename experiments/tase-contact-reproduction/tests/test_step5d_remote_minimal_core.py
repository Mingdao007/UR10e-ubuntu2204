"""Focused offline tests for minimal Step5d remote-control core."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import math
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.step5d_remote_control.core import (
    PreloadGate,
    RemoteControlStateMachine,
    RemotePhase,
    ReactionNormalFilter,
    apply_linear_caps,
    compute_outer_force_terms,
    cycloid_xy_reference,
    desired_retract_vectors,
    desired_search_vectors,
    joint_omega_bounds,
    load_r012_config,
    normalize_vector,
    rotate_toward,
    Step5dRemoteCoreError,
)


TEST_ROOT = Path(__file__).resolve().parents[1]
def _vec_norm(v: tuple[float, float, float] | list[float]) -> float:
    return math.sqrt(sum(float(x) * float(x) for x in v))
@pytest.fixture
def r012_cfg() -> dict:
    return load_r012_config(TEST_ROOT)

def dump_temp_config(tmp_path: Path, cfg: dict) -> Path:
    dst = tmp_path / "r012.yaml"
    dst.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return dst


def test_load_r012_config_resolves_and_types(r012_cfg: dict) -> None:
    assert r012_cfg["route"] == "ros2_remote_control_headless"
    assert r012_cfg["watchdog"]["max_abs_velocity_rad_s"] == 0.5
    assert r012_cfg["watchdog"]["max_acceleration_rad_s2"] == 0.5
    assert r012_cfg["watchdog"]["command_stale_s"] == 0.01
    assert r012_cfg["sensor"]["connect_timeout_s"] == 3.0
    assert r012_cfg["sensor"]["recv_timeout_s"] == 0.25
    assert r012_cfg["sensor"]["ready_timeout_s"] == 3.0
    assert r012_cfg["sensor"]["baseline_s"] == 1.0
    assert r012_cfg["sensor"]["rezero_s"] == 1.0
    assert r012_cfg["guard"]["normal_filter_rate_rad_s"] == 0.1
    assert r012_cfg["guard"]["rate_watchdog_max_miss_s"] == 0.01
    assert r012_cfg["path"]["basis"]["origin_xy_m"] == [0.487795411149049, 0.12932679270060748]
    assert r012_cfg["preflight"]["prior_xyz"] == [0.487834547, 0.129337053, 0.022863519]
    assert r012_cfg["preflight"]["prior_rotvec"] == [3.120752062, 0.0, 0.068626833]
    assert r012_cfg["kinematics"]["tcp_offset_tool0_m"] == [1.8186503701174852e-06, 2.2293003722353485e-07, 0.12209917288991741]
    assert r012_cfg["canary"]["evidence_max_age_s"] == 1800

def test_load_r012_config_rejects_unknown_and_missing_keys(r012_cfg: dict, tmp_path: Path) -> None:
    bad_unknown = deepcopy(r012_cfg)
    bad_unknown["guard"]["extra_key"] = "bad"
    bad_unknown_path = dump_temp_config(tmp_path, bad_unknown)
    with pytest.raises(Step5dRemoteCoreError):
        load_r012_config(tmp_path, config_path=str(bad_unknown_path.relative_to(tmp_path)))

    bad_missing = deepcopy(r012_cfg)
    del bad_missing["search"]["timeout_s"]
    bad_missing_path = dump_temp_config(tmp_path, bad_missing)
    with pytest.raises(Step5dRemoteCoreError):
        load_r012_config(tmp_path, config_path=str(bad_missing_path.relative_to(tmp_path)))
def test_load_r012_config_rejects_nonfinite(tmp_path: Path) -> None:
    cfg = load_r012_config(TEST_ROOT)
    bad = deepcopy(cfg)
    bad["kinematics"]["position_bound_gain_s_inv"] = float("nan")
    temp = tmp_path / "temp_nonfinite.yaml"
    temp.write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(Step5dRemoteCoreError):
        load_r012_config(tmp_path, config_path=str(temp.relative_to(tmp_path)))
def test_load_r012_config_rejects_bad_rnn_and_preload_watchdog_bounds(tmp_path: Path) -> None:
    cfg = load_r012_config(TEST_ROOT)
    bad_rnn = deepcopy(cfg)
    bad_rnn["rates"]["rnn_sigr_exponent_r"] = 1.5
    bad_path = dump_temp_config(tmp_path, bad_rnn)
    with pytest.raises(Step5dRemoteCoreError):
        load_r012_config(tmp_path, config_path=str(bad_path.relative_to(tmp_path)))
    bad_rnn["rates"]["rnn_sigr_exponent_r"] = 0.0
    bad_path.write_text(yaml.safe_dump(bad_rnn), encoding="utf-8")
    with pytest.raises(Step5dRemoteCoreError):
        load_r012_config(tmp_path, config_path=str(bad_path.relative_to(tmp_path)))

    bad_preload = deepcopy(cfg)
    bad_preload["preload"]["raw_min_n"] = 20.0
    bad_preload["preload"]["raw_max_n"] = 10.0
    bad_preload_path = tmp_path / "bad_preload.yaml"
    bad_preload_path.write_text(yaml.safe_dump(bad_preload), encoding="utf-8")
    with pytest.raises(Step5dRemoteCoreError):
        load_r012_config(tmp_path, config_path=str(bad_preload_path.relative_to(tmp_path)))

    bad_watchdog = deepcopy(cfg)
    bad_watchdog["watchdog"]["command_stale_s"] = 0.005
    bad_watchdog_path = tmp_path / "bad_watchdog.yaml"
    bad_watchdog_path.write_text(yaml.safe_dump(bad_watchdog), encoding="utf-8")
    with pytest.raises(Step5dRemoteCoreError):
        load_r012_config(tmp_path, config_path=str(bad_watchdog_path.relative_to(tmp_path)))
    bad_watchdog["watchdog"]["command_stale_s"] = 0.05
    bad_watchdog["guard"]["rate_watchdog_max_miss_s"] = 0.2
    bad_watchdog_path.write_text(yaml.safe_dump(bad_watchdog), encoding="utf-8")
    with pytest.raises(Step5dRemoteCoreError):
        load_r012_config(tmp_path, config_path=str(bad_watchdog_path.relative_to(tmp_path)))


def test_frame_normals_are_opposite(r012_cfg: dict, tmp_path: Path) -> None:
    reaction = r012_cfg["frame"]["reaction_normal_b"]
    approach = r012_cfg["frame"]["approach_normal_b"]
    assert all(math.isfinite(v) for v in reaction)
    assert all(math.isfinite(v) for v in approach)
    assert abs(_vec_norm(tuple(reaction)) - 1.0) < 1e-6
    assert abs(_vec_norm(tuple(approach)) - 1.0) < 1e-6
    assert all(abs(reaction[i] + approach[i]) <= 1e-6 for i in range(3))
    assert r012_cfg["frame"]["normal_sign"] == 1
    bad = deepcopy(r012_cfg)
    angle = math.radians(2.0)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    bad["frame"]["approach_normal_b"] = [
        -cos_a * reaction[0] - sin_a * reaction[1],
        sin_a * reaction[0] - cos_a * reaction[1],
        -reaction[2],
    ]
    bad_path = tmp_path / "approach_near_reaction.yaml"
    bad_path.write_text(yaml.safe_dump(bad), encoding="utf-8")
    with pytest.raises(Step5dRemoteCoreError):
        load_r012_config(tmp_path, config_path=str(bad_path.relative_to(tmp_path)))


def test_outer_force_terms_mapping(r012_cfg: dict) -> None:
    terms = compute_outer_force_terms(r012_cfg["force"])
    p = r012_cfg["force"]["p_gain"]
    i = r012_cfg["force"]["i_gain"]
    d = r012_cfg["force"]["damping"]
    assert terms["Md"] == pytest.approx(1.0 / p)
    assert terms["kf"] == pytest.approx(i / p)
    assert terms["Bd"] == pytest.approx(d / p)


def test_rotate_toward_rate_limited_slerp() -> None:
    cur = (0.0, 0.0, 1.0)
    tgt = (0.0, 1.0, 0.0)
    out = rotate_toward(cur, tgt, max_angle_rad=0.1)
    angle = math.acos(sum(a * b for a, b in zip(normalize_vector(out), normalize_vector(cur))))
    assert angle <= 0.1 + 1e-12


def test_reaction_filter_freeze_and_projection(r012_cfg: dict) -> None:
    filter_ = ReactionNormalFilter(r012_cfg)
    prior = tuple(r012_cfg["frame"]["reaction_normal_b"])
    assert filter_.update([1.0, 0.0, 0.0], load_force_n=1.0, dt=0.1) == pytest.approx(prior)
    filtered = filter_.update((0.0, 0.0, 5.0), load_force_n=5.0, dt=0.1)
    assert filter_.update((0.0, 0.0, -5.0), load_force_n=5.0, dt=0.1) == pytest.approx(filtered)
    assert filter_.update((0.0, 0.0, 20.0), load_force_n=5.0, dt=0.1) != pytest.approx(prior)
    cfg_tau = deepcopy(r012_cfg)
    cfg_tau["guard"]["normal_filter_tau_s"] = 0.0
    cfg_tau["guard"]["friction_projection"] = "off"
    tau_filter = ReactionNormalFilter(cfg_tau)
    assert tau_filter.update((0.0, 0.0, 1.0), load_force_n=1.0, dt=0.1) == pytest.approx(prior)
    assert tau_filter.update((prior[0] + 1e-3, prior[1], prior[2]), load_force_n=5.0, dt=0.1) != pytest.approx(prior)


def test_cycloid_xy_reference_uses_2d_basis() -> None:
    basis = {
        "origin_xy_m": [1.0, 2.0],
        "u_along_xy": [1.0, 0.0],
        "p_lateral_xy": [0.0, 1.0],
    }
    xy0 = cycloid_xy_reference(0.0, basis=basis, amplitude_m=0.015, omega_rad_s=0.1)
    assert xy0 == (1.0, 2.0)
    xy1 = cycloid_xy_reference(1.0, basis=basis, amplitude_m=0.015, omega_rad_s=0.1)
    assert xy1 != xy0


def test_apply_linear_caps_limits() -> None:
    bounded = apply_linear_caps(
        linear_twist=(0.0, 0.0, 0.009),
        angular_twist=(0.1, -0.06, 0.1),
        normal_axis_b=(0.0, 0.0, 1.0),
        tangent_limit_m_s=0.004,
        normal_limit_m_s=0.003,
        total_linear_limit_m_s=0.006,
        angular_limit_rad_s=0.05,
    )
    assert bounded[0][2] <= 0.003 + 1e-12
    x, y, z = bounded[1]
    expected_scale = 0.05 / math.sqrt(0.1 * 0.1 + 0.06 * 0.06 + 0.1 * 0.1)
    assert x == pytest.approx(0.1 * expected_scale, rel=1e-9)
    assert y == pytest.approx(-0.06 * expected_scale, rel=1e-9)
    assert z == pytest.approx(0.1 * expected_scale, rel=1e-9)


def test_apply_linear_caps_angular_norm_limit() -> None:
    bounded = apply_linear_caps(
        linear_twist=(0.0, 0.0, 0.0),
        angular_twist=(0.06, 0.08, 0.01),
        normal_axis_b=(0.0, 0.0, 1.0),
        tangent_limit_m_s=1.0,
        normal_limit_m_s=1.0,
        total_linear_limit_m_s=1.0,
        angular_limit_rad_s=0.1,
    )
    x, y, z = bounded[1]
    assert math.isclose(_vec_norm((x, y, z)), 0.1, rel_tol=1e-6)
    assert abs(x / y - 0.06 / 0.08) < 1e-9


def test_joint_omega_bounds_6d() -> None:
    q = [0.0, 0.0, 0.0, 0.1, -0.1, 0.2]
    q_min = [-1.0, -2.0, -3.0, -2.0, -2.0, -2.0]
    q_max = [1.0, 2.0, 3.0, 1.2, 1.0, 0.5]
    lower, upper = joint_omega_bounds(q, q_min, q_max, alpha_s_inv=4.0, qdot_limit_rad_s=0.5)
    assert lower == pytest.approx(tuple(max(4.0 * (q_min[i] - q[i]), -0.5) for i in range(6)))
    assert upper == pytest.approx(tuple(min(0.5, 4.0 * (q_max[i] - q[i])) for i in range(6)))


def test_joint_omega_bounds_reject_invalid_len_or_inverted() -> None:
    with pytest.raises(Step5dRemoteCoreError):
        joint_omega_bounds([0.0], [0.0] * 6, [0.0] * 6, alpha_s_inv=4.0, qdot_limit_rad_s=0.5)
    with pytest.raises(Step5dRemoteCoreError):
        joint_omega_bounds([0.0] * 6, [1.0] * 6, [0.0] * 6, alpha_s_inv=4.0, qdot_limit_rad_s=0.5)


def test_desired_vectors(r012_cfg: dict) -> None:
    search_twist, search_reaction = desired_search_vectors(r012_cfg, 0.0)
    approach = r012_cfg["frame"]["approach_normal_b"]
    assert search_twist[:3] == pytest.approx(tuple(r012_cfg["search"]["speed_m_s"] * a for a in approach))
    assert search_reaction == pytest.approx(tuple(r012_cfg["frame"]["approach_normal_b"]))
    retract_twist, retract_reaction = desired_retract_vectors(r012_cfg, 0.5)
    reaction = r012_cfg["frame"]["reaction_normal_b"]
    assert retract_twist[:3] == pytest.approx(tuple(r012_cfg["retract"]["speed_m_s"] * r for r in reaction))
    assert retract_reaction == pytest.approx(tuple(reaction))
    retract_stop, _ = desired_retract_vectors(r012_cfg, 1.0)
    assert retract_stop[:3] == (0.0, 0.0, 0.0)


def test_free_space_twist_is_exactly_tangent_and_reverses(r012_cfg: dict) -> None:
    from step5d_remote_control.primitives import free_space_twist

    reaction = tuple(r012_cfg["frame"]["reaction_normal_b"])
    duration = float(r012_cfg["canary"]["free_space_s"])
    forward = free_space_twist(r012_cfg, duration * 0.25)
    reverse = free_space_twist(r012_cfg, duration * 0.75)
    assert sum(forward[index] * reaction[index] for index in range(3)) == pytest.approx(
        0.0, abs=1e-15
    )
    assert reverse[:3] == pytest.approx(tuple(-value for value in forward[:3]))
    assert _vec_norm(forward[:3]) == pytest.approx(
        r012_cfg["canary"]["free_space_linear_limit_m_s"]
    )


def test_state_machine_ready_search_track_retract_stop(r012_cfg: dict) -> None:
    cfg = deepcopy(r012_cfg)
    cfg["run_duration_s"] = 0.2
    sm = RemoteControlStateMachine(cfg)
    out0 = sm.step(0.1, preload_ready=False, preload_fault=False, retract_progress=0.0, fault=False)
    assert out0.phase == RemotePhase.SEARCH
    out1 = sm.step(0.1, preload_ready=True, preload_fault=False, retract_progress=0.0, fault=False)
    assert out1.phase == RemotePhase.TRACK
    out2 = sm.step(0.1, preload_ready=True, preload_fault=False, retract_progress=0.0, fault=False)
    assert out2.phase == RemotePhase.TRACK
    out3 = sm.step(0.1, preload_ready=True, preload_fault=False, retract_progress=0.0, fault=False)
    assert out3.phase == RemotePhase.RETRACT
    out4 = sm.step(0.1, preload_ready=True, preload_fault=False, retract_progress=1.0, fault=False)
    assert out4.phase == RemotePhase.STOP


def test_state_machine_fault_abort(r012_cfg: dict) -> None:
    sm = RemoteControlStateMachine(r012_cfg)
    out = sm.step(0.1, preload_ready=False, preload_fault=False, retract_progress=0.0, fault=True)
    assert out.faulted
    assert out.phase == RemotePhase.ABORT


def test_state_machine_search_timeout_to_abort(r012_cfg: dict) -> None:
    cfg = deepcopy(r012_cfg)
    cfg["search"]["timeout_s"] = 0.0
    sm = RemoteControlStateMachine(cfg)
    sm.step(0.1, preload_ready=False, preload_fault=False, retract_progress=0.0, fault=False)
    out = sm.step(0.1, preload_ready=False, preload_fault=False, retract_progress=0.0, fault=False)
    assert out.phase == RemotePhase.ABORT
    assert out.faulted


def test_preload_gate_holds_and_overforce_fault(r012_cfg: dict) -> None:
    gate = PreloadGate(r012_cfg)
    assert gate.fault is False
    assert gate.step(dt=0.05, raw_force_n=8.0, filtered_force_n=8.5, force_norm_n=5.0) is False
    assert gate.step(dt=0.05, raw_force_n=8.0, filtered_force_n=8.5, force_norm_n=5.0) is True
    gate.reset()
    assert gate.step(dt=0.05, raw_force_n=6.0, filtered_force_n=8.0, force_norm_n=5.0) is False
    assert gate.fault is False
    gate.reset()
    assert gate.step(dt=0.01, raw_force_n=16.0, filtered_force_n=8.0, force_norm_n=5.0) is False
    assert gate.fault is True
    gate.reset()
    assert gate.step(dt=0.01, raw_force_n=10.0, filtered_force_n=15.0, force_norm_n=5.0) is False
    assert gate.fault is True
    gate.reset()
    assert gate.step(dt=0.01, raw_force_n=10.0, filtered_force_n=8.0, force_norm_n=30.0) is False
    assert gate.fault is True
