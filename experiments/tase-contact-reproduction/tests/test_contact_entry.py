import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from contact_benchmark_outer import ContactOuterLoop
from contact_benchmark_provider import ContactCommandProvider
from contact_benchmark_protocol import ENTRY_DURATION_S, Task, protocol, quintic_entry


R = np.diag([1.0, -1.0, -1.0])


def make_outer(law=None):
    return ContactOuterLoop(
        law_step=law or (lambda force, dt: np.asarray(force) * 0.0001),
        anchor_m=[0.0, 0.0, 0.0],
        task_basis=np.eye(3),
        target_rotation=R,
        raw_force_limit_n=20.0,
        raw_torque_limit_nm=2.0,
    )


def outer_tick(outer, *, sample_time_s, phase, entry_time_s=None,
               path_time_s=None, injection_task_n=(0.0, 0.0, 0.0)):
    return outer.step(
        time_s=sample_time_s,
        dt_s=0.002,
        position_m=[0.0, 0.0, 0.0],
        rotation=R,
        raw_force_base_n=[0.0, 0.0, 5.0],
        raw_torque_base_nm=[0.0, 0.0, 0.0],
        phase=phase,
        entry_time_s=entry_time_s,
        path_time_s=path_time_s,
        force_reference_n=5.0,
        injection_task_n=injection_task_n,
    )


def test_quintic_entry_has_c1_seam_and_correct_derivatives():
    task = Task()
    start = task.entry_reference(0.0)
    middle = task.entry_reference(0.37)
    end = task.entry_reference(ENTRY_DURATION_S)
    formal = task.reference(0.0)

    np.testing.assert_allclose(start["position_m"], (0.0, 0.0, 0.0), atol=1e-15)
    np.testing.assert_allclose(start["velocity_m_s"], (0.0, 0.0, 0.0), atol=1e-15)
    np.testing.assert_allclose(start["acceleration_m_s2"], (0.0, 0.0, 0.0), atol=1e-15)
    np.testing.assert_allclose(end["position_m"], formal["position_m"], atol=1e-15)
    np.testing.assert_allclose(end["velocity_m_s"], formal["velocity_m_s"], atol=1e-15)
    np.testing.assert_allclose(end["acceleration_m_s2"], formal["acceleration_m_s2"], atol=1e-15)

    epsilon = 1e-6
    before = quintic_entry(0.37 - epsilon)
    after = quintic_entry(0.37 + epsilon)
    finite_position_velocity = (
        np.asarray(after["position_m"]) - np.asarray(before["position_m"])
    ) / (2.0 * epsilon)
    finite_velocity_acceleration = (
        np.asarray(after["velocity_m_s"]) - np.asarray(before["velocity_m_s"])
    ) / (2.0 * epsilon)
    np.testing.assert_allclose(finite_position_velocity, middle["velocity_m_s"], rtol=0, atol=1e-10)
    np.testing.assert_allclose(finite_velocity_acceleration, middle["acceleration_m_s2"], rtol=0, atol=1e-10)


def test_execution_reference_separates_entry_from_formal_metrics():
    task = Task()
    entry = task.execution_reference(0.5)
    seam = task.execution_reference(ENTRY_DURATION_S)
    terminal = task.execution_reference(ENTRY_DURATION_S + task.duration_s)

    assert entry["stage"] == "entry"
    assert entry["formal_time_s"] is None
    assert seam["stage"] == "formal"
    assert seam["formal_time_s"] == pytest.approx(0.0)
    assert seam["position_m"] == pytest.approx(task.entry_reference(1.0)["position_m"])
    assert seam["velocity_m_s"] == pytest.approx(task.entry_reference(1.0)["velocity_m_s"])
    assert terminal["stage"] == "formal"
    assert terminal["formal_time_s"] == pytest.approx(task.duration_s)
    with pytest.raises(ValueError):
        task.reference(task.duration_s + 1.0)
    with pytest.raises(ValueError):
        task.execution_reference(task.execution_duration_s + 1e-5)


def test_outer_entry_preserves_native_filter_state_and_transition_order():
    calls = []

    def law(force, dt):
        calls.append((np.asarray(force).copy(), dt))
        return np.asarray(force) * 0.0001

    outer = make_outer(law)
    baseline = outer_tick(outer, sample_time_s=100.0, phase="baseline")
    first_entry = outer_tick(outer, sample_time_s=100.002, phase="entry", entry_time_s=0.0)
    end_entry = outer_tick(outer, sample_time_s=100.004, phase="entry", entry_time_s=1.0)
    formal = outer_tick(outer, sample_time_s=100.006, phase="path", path_time_s=0.0)

    assert baseline["phase"] == "baseline"
    assert first_entry["stage"] == "entry"
    assert first_entry["formal_time_s"] is None
    assert first_entry["reference_force_n"] == 5.0
    assert first_entry["injection_task_n"] == (0.0, 0.0, 0.0)
    assert end_entry["reference_velocity_task_m_s"] == pytest.approx((0.004, 0.002, 0.0))
    assert formal["stage"] == "formal"
    assert formal["formal_time_s"] == pytest.approx(0.0)
    assert formal["injection_task_n"] == (0.0, 0.0, 0.0)
    assert len(calls) == 4
    assert outer.snapshot()["last_entry_time_s"] == pytest.approx(1.0)

    with pytest.raises(ValueError, match="completed entry"):
        other = make_outer()
        outer_tick(other, sample_time_s=0.0, phase="baseline")
        outer_tick(other, sample_time_s=0.002, phase="entry", entry_time_s=0.0)
        outer_tick(other, sample_time_s=0.004, phase="path", path_time_s=0.0)
    with pytest.raises(ValueError, match="phase transition"):
        outer_tick(outer, sample_time_s=100.008, phase="baseline")
    with pytest.raises(ValueError, match="formal PATH"):
        unfinished = make_outer()
        outer_tick(unfinished, sample_time_s=0.0, phase="baseline")
        outer_tick(unfinished, sample_time_s=0.002, phase="entry", entry_time_s=0.0)
        outer_tick(unfinished, sample_time_s=0.004, phase="entry", entry_time_s=0.5,
                   injection_task_n=(1.0, 0.0, 0.0))


def test_entry_snapshot_replays_formal_seam_exactly():
    outer = make_outer()
    outer_tick(outer, sample_time_s=0.0, phase="baseline")
    outer_tick(outer, sample_time_s=0.002, phase="entry", entry_time_s=0.0)
    outer_tick(outer, sample_time_s=0.004, phase="entry", entry_time_s=1.0)
    state = outer.snapshot()
    expected = outer_tick(outer, sample_time_s=0.006, phase="path", path_time_s=0.0)
    outer.restore(state)
    replay = outer_tick(outer, sample_time_s=0.006, phase="path", path_time_s=0.0)
    assert expected == replay


def test_provider_entry_is_explicit_and_never_injects_disturbance():
    class Runtime:
        def __init__(self):
            self.solver_profile = SimpleNamespace()
            self.last_sample_s = None
            self.calls = []
            self.kernel = SimpleNamespace(
                outer=SimpleNamespace(
                    settings=SimpleNamespace(filter_tau_s=0.02),
                    reaction=np.array([0.0, 0.0, 1.0]),
                    target=R,
                )
            )

        def step(self, **kwargs):
            self.calls.append(kwargs)
            phase = kwargs["phase"]
            entry_time = kwargs["path_time_s"] if phase == "entry" else None
            return {
                "qdot_rad_s": (0.0,) * 6,
                "jacobian_6x6": ((0.0,) * 6,) * 6,
                "filtered_force_base_n": (0.0, 0.0, 5.0),
                "path_error_task_m": (0.0, 0.0, 0.0),
                "entry_time_s": entry_time,
                "formal_time_s": None if phase == "entry" else kwargs["path_time_s"],
            }

    runtime = Runtime()
    provider = ContactCommandProvider(runtime=runtime, model_hashes={}, scenario="normal_pulse", amplitude_n=3.0)
    output = SimpleNamespace(
        observed_at_s=10.0,
        timestamp=10.0,
        safety_mode=1,
        safety_normal=True,
        tcp_speed_m_s_rad_s=(0.0,) * 6,
        tcp_offset_m_rad=(0.0,) * 6,
        payload_kg=0.0,
        payload_cog_m=(0.0,) * 3,
        q_rad=(0.0,) * 6,
        qd_rad_s=(0.0,) * 6,
        tcp_pose_m_rad=(0.0,) * 6,
    )
    sensor = SimpleNamespace(
        sensor_fresh=True,
        stop_request=False,
        observed_at_s=10.0,
        wrench=(0.0, 0.0, -5.0, 0.0, 0.0, 0.0),
    )
    provider.command(
        output=output, sensor=sensor, monotonic_s=10.0, actual_dt_s=0.002,
        mode="baseline", path_time_s=0.0, internal_setpoint_n=5.0,
    )
    provider.command(
        output=output, sensor=sensor, monotonic_s=10.002, actual_dt_s=0.002,
        mode="entry", entry_time_s=0.5, internal_setpoint_n=5.0,
    )
    call = runtime.calls[-1]
    assert call["phase"] == "entry"
    assert call["path_time_s"] == pytest.approx(0.5)
    assert call["injection_task_n"] == (0.0, 0.0, 0.0)
    assert provider.last_result["formal_time_s"] is None


def test_protocol_config_records_execution_contract():
    config = json.loads((ROOT / "config/contact_benchmark_protocol.json").read_text())
    generated = protocol()
    assert config == generated
    assert config["execution"]["duration_s"] == pytest.approx(63.83185307179586)
    assert config["execution"]["formal_metrics_start_s"] == pytest.approx(1.0)
