from types import SimpleNamespace
import json

import pytest

from build_contact_ramp_probe import HOME_CONFIG, PROGRAM, build
from step5d_autotune_v4_r004.qualification import _contact_ramp_target_time_s
from step5d_autotune_v4_r006.live_adapter import (
    R006LiveAdapterError,
    _bind_r006_contact_ramp_probe_control,
)
from contact_yield_live_contract import load_contact_ramp_probe_identity_contract
import contact_yield_live as live_entry
from contact_yield_live import _contact_ramp_probe_diagnostics
from contact_yield_supervisor import ResidentSupervisor
from figure8_home_config import CANONICAL_FIGURE8_HOME_Q


def _contract():
    return SimpleNamespace(
        readable_runtime_identity=(26, 618002),
        raw={
            "program": "contact_ramp_probe_v1",
            "diagnostic_probe": True,
            "qualification_only": True,
            "guards": {"force_norm_probe_stop_n": 10.0},
        },
    )


def test_probe_observation_budget_starts_at_sampled_5n_target_boundary():
    assert _contact_ramp_target_time_s(
        now_s=10.002, after_latch_s=3.998, ramp_duration_s=4.0,
    ) is None
    assert _contact_ramp_target_time_s(
        now_s=10.006, after_latch_s=4.004, ramp_duration_s=4.0,
    ) == pytest.approx(10.002)


def test_probe_control_uses_approved_ramp_10n_host_stop_and_existing_half_second_gate():
    control = SimpleNamespace(ramp_duration_s=3.0, hard_force_norm_limit_n=10.0)
    _bind_r006_contact_ramp_probe_control(
        control, release_contract=_contract(), path_requested=False, ramp_duration_s=3.0,
    )
    assert control._required_hold_s == pytest.approx(0.1)
    assert control._probe_release_required is True
    assert control._probe_release_timeout_s == pytest.approx(2.0)
    assert control._path_entry_release_gate.hold_s == pytest.approx(0.5)
    assert control._probe_release_started_s is None


@pytest.mark.parametrize(
    ("changes", "match"),
    (
        ({"path_requested": True}, "qualification profile"),
        ({"ramp_duration_s": 5.0}, "qualification profile"),
        ({"release_contract": SimpleNamespace(readable_runtime_identity=(25, 618001), raw={})}, "qualification profile"),
    ),
)
def test_probe_control_rejects_path_or_unbound_runtime(changes, match):
    arguments = {
        "control": SimpleNamespace(ramp_duration_s=3.0, hard_force_norm_limit_n=10.0),
        "release_contract": _contract(),
        "path_requested": False,
        "ramp_duration_s": 3.0,
    }
    arguments.update(changes)
    with pytest.raises(R006LiveAdapterError, match=match):
        _bind_r006_contact_ramp_probe_control(**arguments)


def test_probe_contract_loads_exact_joint_home_and_triplet_identity(tmp_path):
    package_dir = tmp_path / "probe"
    build(HOME_CONFIG, package_dir)
    contract = load_contact_ramp_probe_identity_contract(
        package_dir / f"{PROGRAM}.binding.json"
    )
    assert contract.program == PROGRAM
    assert contract.home_program == "step5d_contact_home_v1"
    assert contract.readable_runtime_identity == (26, 618002)
    assert contract.home_q == CANONICAL_FIGURE8_HOME_Q
    assert contract.raw["diagnostic_probe"] is True
    assert contract.raw["qualification_only"] is True
    binding = json.loads((package_dir / f"{PROGRAM}.binding.json").read_text())
    assert binding["path_commanded"] is False
    assert binding["figure8_commanded"] is False
    assert binding["bo_observation"] is False


def test_supervisor_can_bind_probe_wire_identity_without_changing_production_default():
    row = {
        "received_monotonic_s": 1.0,
        "safety_mode": 1,
        "robot_mode": 7,
        "payload": 0.413,
        "payload_cog": [0.0011, 0.0031, 0.0163],
        "tcp_offset": [0.0, 0.0, 0.0874, 0.0, 0.0, 0.0],
        "output_int_register_32": 606006,
        "output_int_register_33": 26,
        "output_int_register_34": 618002,
    }

    class Observer:
        def latest(self):
            return dict(row)

    class Video:
        def check(self):
            return None

    supervisor = ResidentSupervisor(
        observer=Observer(), video=Video(), read_dashboard=lambda: {},
        writer=SimpleNamespace(), target="/programs/probe.urp",
        readable_runtime_identity=(26, 618002),
    )
    assert supervisor.check(identity=True)["output_int_register_33"] == 26


def test_probe_diagnostics_use_collected_sensor_and_rtde_traces_without_changing_core_evidence():
    control = SimpleNamespace(
        _probe_release_required=True,
        _probe_release_started_s=10.0,
        _probe_release_opened_s=10.5,
        _probe_release_timeout_s=2.0,
        _path_entry_release_gate=SimpleNamespace(hold_s=.5),
        hard_force_norm_limit_n=10.0,
    )
    writer = SimpleNamespace(
        _qualification_control=control,
        raw_observations=[
            {"host_use_monotonic_s": 10.1, "corrected_wrench_n_nm": [0., 0., -4.8, 0., 0., 0.]},
            {"host_use_monotonic_s": 10.4, "corrected_wrench_n_nm": [0., 0., -4.2, 0., 0., 0.]},
        ],
        robot_observations=[
            {"tcp_speed_m_s_rad_s": [.003, 0., 0., 0., 0., 0.], "qd_rad_s": [.02, 0., 0., 0., 0., 0.]},
        ],
    )
    diagnostics = _contact_ramp_probe_diagnostics(
        SimpleNamespace(writer=writer), 4.0,
    )
    assert diagnostics["target_to_stable_force_s"] == pytest.approx(.5)
    assert diagnostics["ramp_start_to_stable_force_s"] == pytest.approx(4.5)
    assert diagnostics["raw_normal_min_after_5n_n"] == pytest.approx(4.2)
    assert diagnostics["force_norm_peak_n"] == pytest.approx(4.8)
    assert diagnostics["max_tcp_linear_speed_m_s"] == pytest.approx(.003)
    assert diagnostics["max_joint_speed_rad_s"] == pytest.approx(.02)


def test_resident_qualification_seals_probe_diagnostics(monkeypatch, tmp_path):
    class AdmissionWriter:
        def __init__(self):
            self.baseline = None

        def set_baseline_state(self, **kwargs):
            self.baseline = dict(kwargs)

    class Writer:
        def __init__(self):
            self.writer = AdmissionWriter()
            self._mono_clock = lambda: 10.0
            self._wall_clock = lambda: 100.0
            self._sleep = lambda _duration: None
            self._r013_path_early_end_controller = None

    class Session:
        lifecycle_events = []
        transport_trace = []
        refreshes = []
        last_arm_invoked_sequence = 1
        last_completed_sequence = None

        def prepare_session(self, **_kwargs):
            return None

        def seal_attempt(self, item, **_kwargs):
            item.setdefault("lifecycle", {})["sealed"] = True
            return item

        def seal_service_tail(self):
            return {"sealed": True}

    session = Session()
    mature = SimpleNamespace(writer=Writer())
    provider = SimpleNamespace(snapshot=lambda: {"attempt_state": "probe"})
    attempt = {
        "evidence_eligible": True,
        "evidence": {"metrics": {"complete": True}},
        "lifecycle": {"home_verified": True},
    }
    observed = {}

    def collect_probe_diagnostics(_mature, duration):
        observed["duration"] = duration
        return {"duration_s": duration}

    monkeypatch.setattr(live_entry, "prepare_session", lambda **_kwargs: session)
    monkeypatch.setattr(live_entry, "run_attempt", lambda *_args, **_kwargs: dict(attempt))
    monkeypatch.setattr(live_entry, "_contact_ramp_probe_diagnostics", collect_probe_diagnostics)
    monkeypatch.setattr(
        live_entry, "finish_session",
        lambda *_args, **_kwargs: {"stopped": True, "program_stopped": True, "home_verified": True},
    )
    monkeypatch.setattr(live_entry, "lifecycle_events_from_writer", lambda *_args, **_kwargs: [])

    result = live_entry._run_live_with_resident_session(
        args=SimpleNamespace(command="qualify", run_dir=tmp_path, control_cpu=4, duration=None),
        controller_transport=object(), mature=mature, runtime=object(), provider=provider,
        prerequisites=object(), request=None, contract=_contract(), video_url="rtsp://unused",
        owner_holder=None, owner_path=None, owner=None, old_handlers={}, receipt={"attempts": []},
        lifecycle_clock=lambda: 10.0, parameter_bindings=None, parameter_files=None,
        resident_candidate_dir=None, research_campaign=False, attempt_count=1,
        defer_recovery=False, refresh_readback=None, dashboard_stop_and_verify=None,
        deferred_seals=None, diagnostic_probe=True, probe_ramp_duration_s=4.0,
    )

    assert observed["duration"] == pytest.approx(4.0)
    assert result["attempts"][0]["contact_ramp_probe_diagnostics"] == {"duration_s": 4.0}
    assert mature.writer.writer.baseline == {
        "consecutive_successes": 1,
        "sticky_one_newton_latched": 0,
    }
