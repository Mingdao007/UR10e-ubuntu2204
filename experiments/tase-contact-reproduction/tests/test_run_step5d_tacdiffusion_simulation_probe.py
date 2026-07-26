from __future__ import annotations

from argparse import Namespace
import json
import struct

import pytest

import run_step5d_tacdiffusion_simulation_probe as probe


def status(*, loaded: bool = True, stationary: bool = True):
    return {
        "controller_5_26": True,
        "local_control": True,
        "loaded_program_matches": loaded,
        "stationary": stationary,
        "dashboard": {
            "safetystatus": "Safetystatus: NORMAL",
            "robotmode": "Robotmode: RUNNING",
        },
        "rtde": {
            "safety_mode": probe.SAFETY_MODE_NORMAL,
            "robot_mode": probe.ROBOT_MODE_RUNNING,
        },
    }


def initial_status():
    result = status()
    result["rtde"].update(
        {
            "actual_TCP_pose": [0.48, 0.12, 0.06, 3.14, 0.0, 0.0],
            "actual_TCP_speed": [0.0] * 6,
            "actual_q": [0.0] * 6,
            "actual_qd": [0.0] * 6,
        }
    )
    return result


def output_sample(
    *,
    phase: int,
    state: int,
    fault: int = 0,
    ack: int = 0,
    runtime_state: int = probe.RUNTIME_PLAYING,
    pose=(0.48, 0.12, 0.06, 3.14, 0.0, 0.0),
    result_normal: float = 0.0,
    result_fault: float = 0.0,
):
    row = {
        "timestamp": float(ack) * 0.002,
        "actual_TCP_pose": list(pose),
        "actual_TCP_speed": [0.0] * 6,
        "actual_q": [0.0] * 6,
        "actual_qd": [0.0] * 6,
        "runtime_state": runtime_state,
        "robot_mode": probe.ROBOT_MODE_RUNNING,
        "safety_mode": probe.SAFETY_MODE_NORMAL,
    }
    row.update({f"output_double_register_{index}": 0.0 for index in range(24, 36)})
    row.update({f"output_int_register_{index}": 0 for index in range(24, 32)})
    row["output_double_register_25"] = 0.002
    row["output_double_register_32"] = result_normal
    row["output_double_register_33"] = result_fault
    row["output_int_register_24"] = state
    row["output_int_register_25"] = ack
    row["output_int_register_26"] = fault
    row["output_int_register_31"] = phase
    return row


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += float(duration)


class FakeProbeRTDE:
    instances = []

    def __init__(self, _host, timeout):
        self.sent = []
        self.send_phases = []
        self.phase = 1
        self.waiting_count = 2
        self.safe_exit_sent = False
        self.terminal_sent = False
        self.receive_calls = 0
        self.missing_calls = {7, 16}
        self.failed = False
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return None

    def negotiate(self):
        return None

    def setup_outputs(self, _frequency, _fields):
        return 1, []

    def setup_inputs(self, _fields):
        return 2, []

    def start(self):
        return None

    def send_inputs(self, _recipe, _types, values):
        packet = list(values)
        self.sent.append(packet)
        self.send_phases.append((self.phase, packet))

    def receive_latest_available(self, _recipe, _types, _fields, timeout_s):
        del timeout_s
        self.receive_calls += 1
        last = self.sent[-1]
        if self.phase == 1 and last[24] == probe.MODE_COMPLETE:
            self.phase = 2
            self.waiting_count = 2
        if self.phase == 2 and last[24] == probe.MODE_ARMED and last[25] >= 8:
            self.safe_exit_sent = True
        if self.safe_exit_sent and not self.terminal_sent:
            self.safe_exit_sent = False
            self.terminal_sent = True
            return output_sample(
                phase=2,
                state=probe.STATE_SAFE_EXIT,
                fault=probe.FAULT_SEQUENCE,
                ack=last[25],
                pose=(0.48005, 0.12005, 0.06005, 3.14, 0.0, 0.0),
            ), 1
        if self.terminal_sent:
            self.terminal_sent = False
            return output_sample(
                phase=99,
                state=probe.STATE_SAFE_EXIT,
                fault=probe.FAULT_SEQUENCE,
                ack=last[25],
                runtime_state=probe.RUNTIME_STOPPED,
                result_normal=1.0,
                result_fault=13.0,
            ), 1
        if self.receive_calls in self.missing_calls:
            return None, 0
        if self.waiting_count:
            self.waiting_count -= 1
            pose = (
                (0.48, 0.12, 0.06, 3.14, 0.0, 0.0)
                if self.phase == 1
                else (0.48005, 0.12005, 0.06005, 3.14, 0.0, 0.0)
            )
            return output_sample(phase=self.phase, state=probe.STATE_WAITING, pose=pose), 1
        ack = int(last[25]) if last[24] in {probe.MODE_ARMED, probe.MODE_COMPLETE} else 0
        base = 0.48 if self.phase == 1 else 0.48005
        noise = (self.receive_calls % 10) * 1.0e-6
        pose = (base + noise, 0.12, 0.06, 3.14, 0.0, 0.0)
        state = probe.STATE_ZERO_STARTUP if 0 < ack < 6 else probe.STATE_TORQUE
        return output_sample(phase=self.phase, state=state, ack=ack, pose=pose), 1


class StaleCompletionFakeProbeRTDE(FakeProbeRTDE):
    def __init__(self, host, timeout):
        super().__init__(host, timeout)
        self.stale_completion_pending = True

    def receive_latest_available(self, recipe, types, fields, timeout_s):
        if self.stale_completion_pending:
            self.stale_completion_pending = False
            return output_sample(
                phase=99,
                state=probe.STATE_WAITING,
                runtime_state=probe.RUNTIME_STOPPED,
                result_normal=13.0,
                result_fault=13.0,
            ), 1
        return super().receive_latest_available(recipe, types, fields, timeout_s)


def run_fake_probe(monkeypatch, tmp_path, fake_class=FakeProbeRTDE):
    clock = FakeClock()
    fake_class.instances.clear()
    monkeypatch.setattr(probe.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(probe.time, "sleep", clock.sleep)
    monkeypatch.setattr(probe, "readonly_status", lambda _host: initial_status())
    monkeypatch.setattr(
        probe,
        "dashboard_snapshot",
        lambda _host: {"programState": "STOPPED", "robotmode": "RUNNING"},
    )
    monkeypatch.setattr(probe, "ProbeRTDE", fake_class)
    args = Namespace(
        robot_host="192.0.2.1",
        connect_timeout_s=1.0,
        wait_for_play_s=2.0,
        valid_ticks=6,
        fault_after_ticks=6,
        tcp_speed_abort=0.0005,
        joint_speed_abort=0.01,
        tcp_position_abort=0.0002,
        joint_position_abort=0.002,
        evidence=tmp_path / "evidence.json",
        run_probe=True,
        write_rtde_inputs=True,
        tp_simulation_visible=True,
    )
    result = probe.run_probe(args)
    return result, fake_class.instances[-1]


def test_packet_contract_is_24_doubles_plus_8_integers() -> None:
    values = probe.input_values(
        (0.48, 0.12, 0.06, 3.14, 0.0, 0.0),
        mode=probe.MODE_ARMED,
        sequence=7,
        lease_id=11,
    )
    assert len(values) == 32
    assert values[:6] == [0.48, 0.12, 0.06, 3.14, 0.0, 0.0]
    assert values[6:12] == list(probe.FIXED_STIFFNESS)
    assert values[12:18] == list(probe.FIXED_DAMPING)
    assert values[18:24] == [0.0] * 6
    assert values[24:] == [
        probe.MODE_ARMED,
        7,
        7,
        11,
        0,
        0,
        0,
        probe.WRENCH_FRAME_TOKEN,
    ]


def test_start_preflight_requires_exact_probe_and_stationary_controller() -> None:
    probe.validate_start_preflight(status())
    with pytest.raises(RuntimeError, match="simulation_probe_not_loaded"):
        probe.validate_start_preflight(status(loaded=False))
    with pytest.raises(RuntimeError, match="robot_not_stationary"):
        probe.validate_start_preflight(status(stationary=False))


def test_readonly_stationary_contract_accepts_controller_denormals(monkeypatch) -> None:
    monkeypatch.setattr(
        probe,
        "dashboard_snapshot",
        lambda _host: {
            "PolyscopeVersion": "URSoftware 5.26.0",
            "get loaded program": f"Loaded program: {probe.CONTROLLER_PROGRAM}",
            "is in remote control": "false",
        },
    )
    monkeypatch.setattr(
        probe,
        "read_rtde_once",
        lambda *_args, **_kwargs: {
            "actual_TCP_pose": [0.0] * 6,
            "actual_TCP_speed": [1.0e-45] * 6,
            "actual_q": [0.0] * 6,
            "actual_qd": [-1.0e-45] * 6,
            "runtime_state": probe.RUNTIME_STOPPED,
            "robot_mode": probe.ROBOT_MODE_RUNNING,
            "safety_mode": probe.SAFETY_MODE_NORMAL,
        },
    )
    assert probe.readonly_status("192.0.2.1")["stationary"] is True


def test_start_requires_all_three_explicit_gates(monkeypatch) -> None:
    args = Namespace(
        run_probe=True,
        write_rtde_inputs=True,
        tp_simulation_visible=False,
    )
    monkeypatch.setattr(
        probe,
        "readonly_status",
        lambda _host: (_ for _ in ()).throw(AssertionError("must fail before network")),
    )
    args.robot_host = "192.0.2.1"
    with pytest.raises(RuntimeError, match="tp-simulation-visible"):
        probe.run_probe(args)


def test_source_has_no_dashboard_mutation_or_program_control() -> None:
    source = probe.Path(probe.__file__).read_text(encoding="utf-8")
    for forbidden in (
        '"load"',
        '"play"',
        '"stop"',
        '"power on"',
        '"brake release"',
    ):
        assert forbidden not in source


def test_absolute_release_schedule_records_missed_slots_without_burst() -> None:
    clock = FakeClock()
    schedule = probe.AbsoluteReleaseSchedule(clock=clock.monotonic, sleeper=clock.sleep)
    first = schedule.release()
    assert first["missed_releases"] == 0
    clock.now = 0.0065
    late = schedule.release()
    assert late["missed_releases"] == 2
    assert late["deadline_lateness_s"] == pytest.approx(0.0045)
    next_release = schedule.release()
    assert next_release["actual_monotonic_s"] == pytest.approx(0.008)
    assert schedule.timing()["missed_release_count"] == 2


def test_fake_rtde_uses_fixed_equilibrium_and_continuous_sequences(monkeypatch, tmp_path) -> None:
    result, fake = run_fake_probe(monkeypatch, tmp_path)
    assert result["phase_results"] == {"normal": 1.0, "sequence_fault": 13.0}
    assert result["status"] == "passed"
    assert result["release_timing"]["period_s"] == pytest.approx(0.002)
    assert result["release_timing"]["missed_release_count"] == 0

    for phase, expected_pose in (
        (1, (0.48, 0.12, 0.06, 3.14, 0.0, 0.0)),
        (2, (0.48005, 0.12005, 0.06005, 3.14, 0.0, 0.0)),
    ):
        packets = [
            packet
            for packet_phase, packet in fake.send_phases
            if packet_phase == phase and packet[24] == probe.MODE_ARMED
        ]
        assert packets
        expected_bytes = struct.pack("!6d", *expected_pose)
        assert {struct.pack("!6d", *packet[:6]) for packet in packets} == {expected_bytes}

    phase1_sequences = [
        packet[25]
        for packet_phase, packet in fake.send_phases
        if packet_phase == 1 and packet[24] == probe.MODE_ARMED
    ]
    phase2_sequences = [
        packet[25]
        for packet_phase, packet in fake.send_phases
        if packet_phase == 2 and packet[24] == probe.MODE_ARMED
    ]
    assert phase1_sequences == list(range(1, 7))
    gap_index = next(
        index
        for index in range(1, len(phase2_sequences))
        if phase2_sequences[index] != phase2_sequences[index - 1] + 1
    )
    assert phase2_sequences[:gap_index] == list(range(1, gap_index + 1))
    assert phase2_sequences[gap_index] == phase2_sequences[gap_index - 1] + 2
    assert all(value == phase2_sequences[gap_index] for value in phase2_sequences[gap_index:])
    assert result["faults_by_phase"]["1"] == [0]
    assert probe.FAULT_SEQUENCE in result["faults_by_phase"]["2"]
    assert result["evidence_path"] == str(tmp_path / "evidence.json")


def test_stale_terminal_registers_do_not_complete_a_new_run(monkeypatch, tmp_path) -> None:
    result, fake = run_fake_probe(monkeypatch, tmp_path, StaleCompletionFakeProbeRTDE)
    assert result["status"] == "passed"
    assert result["observed_playing_phase"] is True
    assert result["phase_results"] == {"normal": 1.0, "sequence_fault": 13.0}
    assert fake.sent[1][24] == probe.MODE_DISABLED


class FailingFakeProbeRTDE(FakeProbeRTDE):
    def send_inputs(self, recipe, types, values):
        if values[24] == probe.MODE_ARMED and values[25] == 3 and not self.failed:
            self.failed = True
            raise RuntimeError("synthetic_send_failure")
        return super().send_inputs(recipe, types, values)


def test_failure_evidence_survives_rtde_exception(monkeypatch, tmp_path) -> None:
    with pytest.raises(RuntimeError, match="synthetic_send_failure"):
        run_fake_probe(monkeypatch, tmp_path, FailingFakeProbeRTDE)
    evidence = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "failed"
    assert evidence["failure"]["message"] == "synthetic_send_failure"
    assert evidence["final_dashboard_state"]["programState"] == "STOPPED"
    assert evidence["events"]
    failed_attempts = [
        event
        for event in evidence["events"]
        if event["kind"] == "normal_armed_attempt"
        and event["outgoing"]["sequence"] == 3
    ]
    assert failed_attempts
    assert "phase" in failed_attempts[-1]
    assert "deadline_lateness_s" in failed_attempts[-1]
    assert failed_attempts[-1]["outgoing_mode"] == probe.MODE_ARMED
    assert failed_attempts[-1]["outgoing_sequence"] == 3
    assert failed_attempts[-1]["outgoing_heartbeat"] == 3
