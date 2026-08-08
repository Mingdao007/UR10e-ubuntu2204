from __future__ import annotations

import argparse
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from run_tacdiffusion_formal_v4 import (
    _formal_acquisition_evidence_row,
    _formal_acquisition_input_values,
    _formal_acquisition_receive_available,
    _formal_handoff_idle_packet,
    _require_frozen_live_endpoints,
    _run_formal_acquisition_phase,
    _formal_track_state_torque_rows,
    _parse_exact_sensor_delivery_watchdog,
)
from ur10e_vic.tacdiffusion.formal_contact_acquisition import (
    ACQUISITION_FORCE_GUARD_N,
    ACQUISITION_BRAKING_DISTANCE_M,
    ACQUISITION_COMMAND_ABORT,
    ACQUISITION_COMMAND_PREPARE,
    ACQUISITION_DECELERATION_START_DISTANCE_M,
    ACQUISITION_DISCRETE_STOP_BOUND_M,
    ACQUISITION_HEARTBEAT_TIMEOUT_TICKS,
    ACQUISITION_LATCH_LOAD_N,
    ACQUISITION_LATCH_SAMPLES,
    ACQUISITION_MAX_DISTANCE_M,
    ACQUISITION_SENSOR_DELIVERY_WATCHDOG_S,
    ACQUISITION_PREPARE_TIMEOUT_TICKS,
    ACQUISITION_STATIONARY_TCP_SPEED_LIMIT_M_S,
    ACQUISITION_TORQUE_GUARD_NM,
    AcquisitionError,
    AcquisitionHeartbeatV1,
    AcquisitionHandoffV1,
    AcquisitionState,
    BumplessDirectTorqueTransitionV1,
    FIXED_TRACKING_STIFFNESS,
    FORMAL_ROUTE_IDENTITY,
    FormalContactAcquisitionControllerV1,
    KunweiAcquisitionSample,
    StationaryPoseSample,
    build_formal_contact_acquisition_urscript,
    build_formal_direct_torque_tracking_source_after_handoff,
    parse_formal_contact_acquisition_urscript,
)
from ur10e_vic.tacdiffusion.direct_torque_live_v4 import (
    LiveTubeContract,
    parse_live_receiver_source,
)
from ur10e_vic.tacdiffusion.eligibility import FormalEligibilityValidator
from ur10e_vic.tacdiffusion.formal_identity import resolve_formal_current_state
from ur10e_vic.tacdiffusion.formal_orchestration import ContactAcquisitionContractV1


ROOT = Path(__file__).resolve().parents[3]


def _controller() -> FormalContactAcquisitionControllerV1:
    return FormalContactAcquisitionControllerV1(ContactAcquisitionContractV1())


def _kunwei(
    index: int,
    *,
    normal_load_n: float = 0.0,
    force_norm_n: float = 0.0,
    torque_norm_nm: float = 0.0,
    route_identity: str = FORMAL_ROUTE_IDENTITY,
    safety_mode: str = "NORMAL",
    robot_mode: str = "RUNNING",
    host_age_s: float = 0.001,
    protective_stop: bool = False,
    joint_fault: bool = False,
) -> KunweiAcquisitionSample:
    return KunweiAcquisitionSample(
        sample_index=index,
        host_age_s=host_age_s,
        normal_load_n=normal_load_n,
        force_norm_n=force_norm_n,
        torque_norm_nm=torque_norm_nm,
        route_identity=route_identity,
        lease_id=11,
        episode_identity=22,
        safety_mode=safety_mode,
        robot_mode=robot_mode,
        protective_stop=protective_stop,
        joint_fault=joint_fault,
    )


def _stationary(time_s: float, *, speed: float = 0.0) -> StationaryPoseSample:
    return StationaryPoseSample(
        sample_time_s=time_s,
        host_age_s=0.001,
        actual_pose_base=(0.4, 0.1, 0.03, 3.12, 0.0, 0.0),
        tcp_speed_base=(speed, 0.0, 0.0, 0.0, 0.0, 0.0),
        joint_speed_rad_s=(0.0,) * 6,
        route_identity=FORMAL_ROUTE_IDENTITY,
        lease_id=11,
        episode_identity=22,
        safety_mode="NORMAL",
        robot_mode="RUNNING",
    )


def test_acquisition_urscript_is_bounded_and_has_no_forbidden_force_or_motion_api() -> None:
    source = build_formal_contact_acquisition_urscript(ContactAcquisitionContractV1())
    parse_formal_contact_acquisition_urscript(source)
    assert "speedl([0.0, 0.0, -0.0005, 0.0, 0.0, 0.0]," in source
    assert "acquisition_max_distance_m = 0.025" in source
    assert "a=acquisition_acceleration_m_s2" in source
    assert "a=acquisition_deceleration_m_s2" in source
    assert "acquisition_control_period_s = 0.002" in source
    assert "acquisition_heartbeat_timeout_s = 0.080" in source
    assert "acquisition_heartbeat_timeout_ticks = 40" in source
    assert "acquisition_prepare_timeout_ticks = 200" in source
    assert "command_abort = 2" in source
    assert "command_prepare = 0" in source
    assert ACQUISITION_COMMAND_ABORT == 2
    assert ACQUISITION_COMMAND_PREPARE == 0
    assert "packet_sequence_before = read_input_integer_register(25)" in source
    assert "packet_sequence_after = read_input_integer_register(25)" in source
    assert "packet_gap_or_replay" in source
    assert "packet_heartbeat_timeout" in source
    assert "packet_prepare_timeout" in source
    assert "local acquisition_latched_by_host = 0" in source
    assert "local acquisition_handoff_ack = 0" in source
    assert "acquisition_latched_by_host = 1" in source
    assert "acquisition_handoff_ack = 1" in source
    assert "acquisition_latched_by_host = False" not in source
    assert "acquisition_handoff_ack = False" not in source
    assert "acquisition_latched_by_host = True" not in source
    assert "acquisition_handoff_ack = True" not in source
    assert "local motion_frame_consumed = False" in source
    assert "motion_frame_consumed = True" in source
    assert "if not motion_frame_consumed:" in source
    assert "speedl(t=control_period) already consumes this controller frame" in source
    assert "acquisition_braking_distance_m = 0.0000125000" in source
    assert "acquisition_deceleration_start_distance_m = 0.0249865000" in source
    assert source.count("stopl(a=acquisition_deceleration_m_s2)") >= 9
    zero_stop = "speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], a=acquisition_deceleration_m_s2, t=acquisition_control_period_s)"
    fault_markers = (
        "if not identity_ok:",
        "elif not packet_protocol_ok:",
        "elif packet_heartbeat_timeout:",
        "elif packet_lease != acquisition_lease or packet_episode != acquisition_episode:",
        "elif command == command_abort:",
    )
    for marker in fault_markers:
        start = source.index(marker)
        end = source.find("\n    elif ", start + len(marker))
        block = source[start:] if end < 0 else source[start:end]
        assert zero_stop in block
        assert "stopl(a=acquisition_deceleration_m_s2)" in block
        assert "acquisition_running = False" in block
    assert "actual_TCP_force" not in source
    assert "get_tcp_force" not in source
    assert "direct_torque(" not in source


def test_primary_barrier_is_inert_prepare_then_sequence_two_arms_motion() -> None:
    values = _formal_acquisition_input_values(
        command=ACQUISITION_COMMAND_PREPARE,
        sequence=1,
        lease_id=11,
        episode_identity=22,
        safety_normal=True,
        robot_running=True,
    )
    assert values[24] == ACQUISITION_COMMAND_PREPARE
    assert values[25] == 1

    source = inspect.getsource(_run_formal_acquisition_phase)
    prepare = source.index("command=ACQUISITION_COMMAND_PREPARE")
    prime = source.index("legacy._prime_idle_inputs")
    barrier = source.index("_send_urscript_with_primary_start_barrier")
    cursor = source.index("sensor_cursor = int(")
    host_start = source.index("controller.start(")
    active_start = source.index("command=ACQUISITION_COMMAND_START")
    assert prepare < prime < barrier < cursor < host_start < active_start
    assert "prepare_ack_observed" in source
    assert "if output_ack > sequence:" in source
    assert "if last_ack_sequence == sequence" in source
    assert "last_packet_values" in source
    assert "_formal_acquisition_receive_available" in source
    assert "now - last_packet_transmit_s >= 0.010" in source
    assert "now - pending_sequence_started_s" in source
    assert "formal_acquisition_ack_heartbeat_timeout" in source


def test_acquisition_receive_uses_bounded_hot_socket_drain() -> None:
    class FakeRTDE:
        def __init__(self) -> None:
            self.call = None

        def receive_available_bounded(self, *args, **kwargs):
            self.call = (args, kwargs)
            return [{"timestamp": 1.0}]

    rtde = FakeRTDE()
    rows = _formal_acquisition_receive_available(rtde, 2, ["DOUBLE"], 0.005)
    assert rows == [{"timestamp": 1.0}]
    assert rtde.call is not None
    assert rtde.call[1]["max_samples"] == 4
    assert rtde.call[1]["max_wall_s"] == pytest.approx(0.004)


def test_generated_acquisition_closes_search_branch_before_stationary_elif() -> None:
    source = build_formal_contact_acquisition_urscript(
        ContactAcquisitionContractV1()
    )
    cruise = source.index(
        "speedl([0.0, 0.0, -0.0005, 0.0, 0.0, 0.0],"
    )
    stationary = source.index(
        "elif acquisition_state == 2 or acquisition_state == 3:"
    )
    assert "\n      end\n" in source[cruise:stationary]


def test_live_acquisition_packet_flow_is_prepare_then_ack_paced_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_id = 11
    episode_identity = 22
    pose = (0.4, 0.1, 0.03, 3.12, 0.0, 0.0)

    def output_sample(*, state: int, ack: int, timestamp: float, handoff: int = 0):
        return {
            "timestamp": timestamp,
            "actual_TCP_pose": pose,
            "actual_TCP_speed": (0.0,) * 6,
            "actual_qd": (0.0,) * 6,
            "joint_mode": (253,) * 6,
            "runtime_state": 2,
            "robot_mode": 7,
            "safety_mode": 1,
            "output_int_register_24": state,
            "output_int_register_25": ack,
            "output_int_register_26": 0,
            "output_int_register_27": 0 if state == 0 else lease_id,
            "output_int_register_28": 0 if state == 0 else episode_identity,
            "output_int_register_29": 4004001,
            "output_int_register_30": handoff,
            "output_int_register_31": int(state >= 2),
        }

    receive_batches = iter(
        (
            [output_sample(state=0, ack=1, timestamp=0.0)],
            # The first START transmission is not observed by the controller;
            # it continues to ACK PREPARE until the identical sequence-2
            # packet is resent.
            [output_sample(state=0, ack=1, timestamp=0.5)],
            [output_sample(state=1, ack=2, timestamp=1.0)],
            [output_sample(state=2, ack=3, timestamp=1.101)],
            [output_sample(state=3, ack=4, timestamp=1.203)],
            [output_sample(state=4, ack=5, timestamp=1.205, handoff=1)],
        )
    )

    class FakeRTDE:
        def __init__(self) -> None:
            self.sent: list[tuple[int, int]] = []

        def send_inputs(self, _recipe, _types, values) -> None:
            self.sent.append((int(values[24]), int(values[25])))

    class FakeKunwei:
        def __init__(self) -> None:
            self.calls = 0

        def snapshot(self, *, max_age_s: float):
            assert max_age_s == pytest.approx(0.080)
            return SimpleNamespace(sample_index=100)

        def snapshots_since(self, cursor: int, *, max_age_s: float):
            assert max_age_s == pytest.approx(0.080)
            self.calls += 1
            if self.calls < 3:
                return ()
            if self.calls > 3:
                assert cursor == 150
                return ()
            assert cursor == 100
            now = __import__("time").monotonic()
            return tuple(
                SimpleNamespace(
                    sample_index=index,
                    t_monotonic_s=now + (index - 100) * 0.006,
                    normal_load_n=1.1,
                    force_norm_n=1.1,
                    torque_norm_nm=0.0,
                )
                for index in range(101, 151)
            )

    monkeypatch.setattr(
        "run_tacdiffusion_formal_v4.legacy._prime_idle_inputs",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "run_tacdiffusion_formal_v4.legacy._send_urscript_with_primary_start_barrier",
        lambda *_args, **_kwargs: {"barrier_hold_s": 0.15},
    )
    monkeypatch.setattr(
        "run_tacdiffusion_formal_v4.legacy._receive_available",
        lambda *_args, **_kwargs: next(receive_batches),
    )
    clock = {"now": 0.0}

    def advancing_monotonic() -> float:
        clock["now"] += 0.006
        return clock["now"]

    monkeypatch.setattr(
        "run_tacdiffusion_formal_v4.time.monotonic", advancing_monotonic
    )
    rtde = FakeRTDE()
    rows: list[dict[str, object]] = []
    handoff, _, _, barrier = _run_formal_acquisition_phase(
        args=argparse.Namespace(robot_host="192.168.1.18", connect_timeout_s=3.0),
        rtde=rtde,
        input_recipe=1,
        input_types=[],
        output_recipe=2,
        output_types=[],
        contract=ContactAcquisitionContractV1(),
        controller=_controller(),
        kunwei=FakeKunwei(),
        lease_id=lease_id,
        episode_identity=episode_identity,
        acquisition_source=build_formal_contact_acquisition_urscript(
            ContactAcquisitionContractV1()
        ),
        attempt_id="attempt",
        acquisition_rows=rows,
    )
    assert rtde.sent[0] == (0, 1)
    assert rtde.sent.count((1, 2)) >= 2
    assert (1, 3) in rtde.sent
    assert (1, 4) in rtde.sent
    assert (1, 5) in rtde.sent
    assert [sequence for _, sequence in rtde.sent] == sorted(
        sequence for _, sequence in rtde.sent
    )
    assert handoff.anchor_pose_base == pose
    assert barrier["prepare_ack_observed"] is True
    assert barrier["motion_armed_during_barrier"] is False
    assert len(rows) == 5


def test_acquisition_fault_abort_is_immediate_not_ack_paced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_output = {
        "timestamp": 0.0,
        "actual_TCP_pose": (0.4, 0.1, 0.03, 3.12, 0.0, 0.0),
        "actual_TCP_speed": (0.0,) * 6,
        "actual_qd": (0.0,) * 6,
        "runtime_state": 2,
        "robot_mode": 7,
        "safety_mode": 1,
        "output_int_register_24": 0,
        "output_int_register_25": 1,
        "output_int_register_26": 0,
        "output_int_register_27": 0,
        "output_int_register_28": 0,
        "output_int_register_29": 4004001,
        "output_int_register_30": 0,
        "output_int_register_31": 0,
    }

    class FakeRTDE:
        def __init__(self) -> None:
            self.sent: list[tuple[int, int]] = []

        def send_inputs(self, _recipe, _types, values) -> None:
            self.sent.append((int(values[24]), int(values[25])))

    class FailingKunwei:
        def snapshot(self, *, max_age_s: float):
            return SimpleNamespace(sample_index=100)

        def snapshots_since(self, _cursor: int, *, max_age_s: float):
            raise RuntimeError("kunwei_delivery_stale")

    monkeypatch.setattr(
        "run_tacdiffusion_formal_v4.legacy._prime_idle_inputs",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "run_tacdiffusion_formal_v4.legacy._send_urscript_with_primary_start_barrier",
        lambda *_args, **_kwargs: {"barrier_hold_s": 0.15},
    )
    monkeypatch.setattr(
        "run_tacdiffusion_formal_v4.legacy._receive_available",
        lambda *_args, **_kwargs: [prepare_output],
    )
    rtde = FakeRTDE()
    with pytest.raises(RuntimeError, match="formal_acquisition_sensor_fault"):
        _run_formal_acquisition_phase(
            args=argparse.Namespace(
                robot_host="192.168.1.18", connect_timeout_s=3.0
            ),
            rtde=rtde,
            input_recipe=1,
            input_types=[],
            output_recipe=2,
            output_types=[],
            contract=ContactAcquisitionContractV1(),
            controller=_controller(),
            kunwei=FailingKunwei(),
            lease_id=11,
            episode_identity=22,
            acquisition_source=build_formal_contact_acquisition_urscript(
                ContactAcquisitionContractV1()
            ),
            attempt_id="attempt",
            acquisition_rows=[],
        )
    assert rtde.sent == [(0, 1), (1, 2), (2, 3)]


def test_formal_live_endpoints_are_exact_robot_and_kunwei_identities() -> None:
    accepted = argparse.Namespace(
        robot_host="192.168.1.18",
        sensor_ip="192.168.50.25",
        sensor_port=5152,
    )
    _require_frozen_live_endpoints(accepted)
    with pytest.raises(RuntimeError, match="controller_endpoint_identity_mismatch"):
        _require_frozen_live_endpoints(
            argparse.Namespace(
                robot_host="192.168.56.101",
                sensor_ip="192.168.50.25",
                sensor_port=5152,
            )
        )
    with pytest.raises(RuntimeError, match="kunwei_endpoint_identity_mismatch"):
        _require_frozen_live_endpoints(
            argparse.Namespace(
                robot_host="192.168.1.18",
                sensor_ip="192.168.50.99",
                sensor_port=5152,
            )
        )


def test_host_latch_requires_exactly_50_consecutive_native_samples() -> None:
    controller = _controller()
    controller.start(
        lease_id=11,
        episode_identity=22,
        route_identity=FORMAL_ROUTE_IDENTITY,
        monotonic_s=0.0,
    )
    for index in range(ACQUISITION_LATCH_SAMPLES - 1):
        assert controller.observe_kunwei(_kunwei(index, normal_load_n=ACQUISITION_LATCH_LOAD_N)) is False
    assert controller.state == AcquisitionState.VELOCITY_SEARCH
    assert controller.observe_kunwei(
        _kunwei(ACQUISITION_LATCH_SAMPLES - 1, normal_load_n=ACQUISITION_LATCH_LOAD_N)
    ) is True
    assert controller.state == AcquisitionState.STOPPING
    assert controller.contact_latch_count == ACQUISITION_LATCH_SAMPLES


def test_acquisition_host_heartbeat_has_exact_40_tick_timeout_and_strict_sequence_semantics() -> None:
    heartbeat = AcquisitionHeartbeatV1()
    assert heartbeat.control_period_s == 0.002
    assert heartbeat.timeout_s == 0.080
    signature = (1, 11, 22, 4004001, 1, 1, 0, 0)
    assert heartbeat.observe(1, signature) == "NEW"
    for _ in range(ACQUISITION_HEARTBEAT_TIMEOUT_TICKS - 1):
        assert heartbeat.observe(1, signature) == "HELD"
    with pytest.raises(AcquisitionError, match="heartbeat_timeout"):
        heartbeat.observe(1, signature)

    with pytest.raises(AcquisitionError, match="gap_or_replay"):
        AcquisitionHeartbeatV1().observe(2, signature)
    with pytest.raises(AcquisitionError, match="gap_or_replay"):
        heartbeat.observe(0, signature)
    payload_heartbeat = AcquisitionHeartbeatV1()
    payload_heartbeat.observe(1, signature)
    with pytest.raises(AcquisitionError, match="payload_changed"):
        # A same-sequence packet with changed fields is not a valid held packet.
        payload_heartbeat.observe(1, signature[:-1] + (1,))


def test_sensor_delivery_watchdog_is_exactly_80ms_and_not_native_frame_arrival_freshness() -> None:
    contract = ContactAcquisitionContractV1()
    assert contract.sensor_delivery_watchdog_s == ACQUISITION_SENSOR_DELIVERY_WATCHDOG_S
    assert contract.as_json()["sensor_delivery_watchdog_s"] == 0.080
    assert contract.as_json()["sensor_delivery_watchdog_semantics"] == (
        "latest_native_batch_delivery_age_only_not_per_frame_host_arrival"
    )
    with pytest.raises(ValueError, match="sensor_delivery_watchdog_s"):
        ContactAcquisitionContractV1(sensor_delivery_watchdog_s=0.010)
    with pytest.raises(argparse.ArgumentTypeError, match="exactly 0.080 s"):
        _parse_exact_sensor_delivery_watchdog("0.010")
    assert _parse_exact_sensor_delivery_watchdog("0.080") == 0.080


def test_prepare_expiry_is_bounded_and_stationary_threshold_rejects_search_speed() -> None:
    assert ACQUISITION_PREPARE_TIMEOUT_TICKS * 0.002 == pytest.approx(0.400)
    assert ACQUISITION_STATIONARY_TCP_SPEED_LIMIT_M_S == pytest.approx(0.0001)
    assert ACQUISITION_STATIONARY_TCP_SPEED_LIMIT_M_S < 0.0005

    controller = _controller()
    controller.start(
        lease_id=11,
        episode_identity=22,
        route_identity=FORMAL_ROUTE_IDENTITY,
        monotonic_s=0.0,
    )
    for index in range(ACQUISITION_LATCH_SAMPLES):
        controller.observe_kunwei(_kunwei(index, normal_load_n=1.1))
    assert controller.observe_stationary(_stationary(0.0, speed=0.0005)) is None
    assert controller.state == AcquisitionState.STOPPING
    assert controller.observe_stationary(_stationary(0.101, speed=0.0)) is None
    assert controller.state == AcquisitionState.STATIONARY_DWELL
    assert controller.observe_stationary(_stationary(0.202, speed=0.0)) is not None


def test_acquisition_profile_never_commands_more_than_25mm() -> None:
    controller = _controller()
    controller.start(
        lease_id=11,
        episode_identity=22,
        route_identity=FORMAL_ROUTE_IDENTITY,
        monotonic_s=0.0,
    )
    before_stop = controller.command_at(50.0)
    at_stop = controller.command_at(50.1)
    assert before_stop.distance_m <= ACQUISITION_MAX_DISTANCE_M
    assert at_stop.distance_m == pytest.approx(ACQUISITION_MAX_DISTANCE_M)
    assert at_stop.state == AcquisitionState.SEARCH_EXHAUSTED
    assert at_stop.velocity_base == (0.0,) * 6
    assert controller.auto_return_allowed is True


def test_controller_side_deceleration_and_discrete_bound_fit_inside_25mm() -> None:
    braking_distance = 0.0005**2 / (2.0 * 0.01)
    assert braking_distance == pytest.approx(ACQUISITION_BRAKING_DISTANCE_M)
    physical_stop = (
        ACQUISITION_DECELERATION_START_DISTANCE_M
        + braking_distance
        + ACQUISITION_DISCRETE_STOP_BOUND_M
    )
    assert ACQUISITION_DECELERATION_START_DISTANCE_M < ACQUISITION_MAX_DISTANCE_M
    assert braking_distance == pytest.approx(0.0000125)
    assert physical_stop <= ACQUISITION_MAX_DISTANCE_M


def test_latch_stops_velocity_then_requires_fresh_stationary_dwell_for_handoff() -> None:
    controller = _controller()
    controller.start(
        lease_id=11,
        episode_identity=22,
        route_identity=FORMAL_ROUTE_IDENTITY,
        monotonic_s=0.0,
    )
    for index in range(ACQUISITION_LATCH_SAMPLES):
        controller.observe_kunwei(_kunwei(index, normal_load_n=1.1))
    assert controller.command_at(0.2).stop_velocity_control is True
    assert controller.observe_stationary(_stationary(0.0)) is None
    assert controller.state == AcquisitionState.STATIONARY_DWELL
    assert controller.observe_stationary(_stationary(0.050)) is None
    handoff = controller.observe_stationary(_stationary(0.101))
    assert handoff is not None
    assert controller.state == AcquisitionState.HANDOFF_READY
    assert handoff.anchor_pose_base == (0.4, 0.1, 0.03, 3.12, 0.0, 0.0)
    assert handoff.acquisition_training is False
    with pytest.raises(AcquisitionError, match="handoff_not_ready"):
        controller._fault("sensor_fault")
        _ = controller.handoff


def test_bumpless_transition_starts_at_actual_pose_zero_feedforward_and_k600() -> None:
    controller = _controller()
    controller.start(
        lease_id=11,
        episode_identity=22,
        route_identity=FORMAL_ROUTE_IDENTITY,
        monotonic_s=0.0,
    )
    for index in range(ACQUISITION_LATCH_SAMPLES):
        controller.observe_kunwei(_kunwei(index, normal_load_n=1.1))
    controller.observe_stationary(_stationary(0.0))
    handoff = controller.observe_stationary(_stationary(0.101))
    assert handoff is not None
    transition = BumplessDirectTorqueTransitionV1(handoff, ContactAcquisitionContractV1())
    initial = transition.start(handoff.anchor_pose_base)
    assert initial.desired_pose_base == handoff.anchor_pose_base
    assert initial.raw_feedforward_wrench == (0.0,) * 6
    assert initial.stiffness_6d == FIXED_TRACKING_STIFFNESS
    ramped = transition.step(0.25, (0.0, 0.0, 5.0, 0.0, 0.0, 0.0))
    assert 0.0 < ramped.raw_feedforward_wrench[2] < 5.0
    settled = transition.step(0.5, (0.0, 0.0, 5.0, 0.0, 0.0, 0.0))
    assert settled.phase == "TRACK"
    assert settled.raw_feedforward_wrench[2] == pytest.approx(5.0)
    assert transition.force_from_translation_error((0.001, 0.0, 0.0))[0] == pytest.approx(0.6)
    with pytest.raises(AcquisitionError, match="handoff_anchor_mismatch"):
        transition.start((0.40031, 0.1, 0.03, 3.12, 0.0, 0.0))


def test_handoff_idle_reprime_replaces_entry_lineage_with_fresh_anchor_and_guard() -> None:
    anchor = (0.4, 0.1, 0.03, 3.12, 0.0, 0.0)
    handoff = AcquisitionHandoffV1(
        route_identity=FORMAL_ROUTE_IDENTITY,
        lease_id=11,
        episode_identity=22,
        contact_latch_sample_index=1050,
        stationary_dwell_s=0.1,
        anchor_pose_base=anchor,
    )
    guard = SimpleNamespace(
        wrench_tcp_si=(0.0, 0.0, 1.2, 0.0, 0.0, 0.0),
        sample_index=1100,
        receive_batch_id=17,
        nominal_sensor_time_s=1.1,
        t_monotonic_s=12.0,
    )
    packet = _formal_handoff_idle_packet(
        handoff=handoff,
        guard=guard,
        lease_id=11,
        episode_identity=22,
    )
    assert packet.lineage.command_sequence == 0
    assert packet.lineage.desired_pose == anchor
    assert packet.lineage.commanded_k == FIXED_TRACKING_STIFFNESS
    assert packet.lineage.commanded_raw_f_ff == (0.0,) * 6
    assert packet.lineage.kunwei_sample_index == 1100
    assert packet.lineage.kunwei_receive_batch_id == 17
    assert packet.lineage.kunwei_guard_wrench_tcp_si == guard.wrench_tcp_si
    assert packet.lineage.desired_pose != (0.48, 0.13, 0.033, 3.12, 0.0, 0.0)


def test_acquisition_evidence_row_is_non_training_and_not_direct_torque_protocol_evidence() -> None:
    sample = {
        "timestamp": 1.0,
        "actual_TCP_pose": (0.4, 0.1, 0.03, 3.12, 0.0, 0.0),
        "actual_TCP_speed": (0.0,) * 6,
        "actual_qd": (0.0,) * 6,
        "runtime_state": 0,
        "robot_mode": 7,
        "safety_mode": 1,
        "output_int_register_24": 1,
        "output_int_register_25": 3,
        "output_int_register_26": 0,
        "output_int_register_27": 11,
        "output_int_register_28": 22,
        "output_int_register_29": 4004001,
        "output_int_register_30": 0,
        "output_int_register_31": 0,
    }
    row = _formal_acquisition_evidence_row(
        sample,
        elapsed_s=0.02,
        attempt_id="attempt",
        host_sequence=3,
    )
    assert row["acquisition_evidence_only"] is True
    assert row["acquisition_training"] is False
    assert row["formal_training_included"] is False
    assert row["protocol_evidence"] is False
    assert "receiver_state" not in row
    assert "ack_sequence" not in row
    assert "command_desired_pose_0" not in row
    assert _formal_track_state_torque_rows([row]) == []


@pytest.mark.parametrize(
    "sample",
    (
        _kunwei(0, host_age_s=ACQUISITION_SENSOR_DELIVERY_WATCHDOG_S + 0.001),
        _kunwei(0, force_norm_n=ACQUISITION_FORCE_GUARD_N + 0.01),
        _kunwei(0, torque_norm_nm=ACQUISITION_TORQUE_GUARD_NM + 0.01),
        _kunwei(0, protective_stop=True),
        _kunwei(0, safety_mode="REDUCED"),
        _kunwei(0, joint_fault=True),
        _kunwei(0, route_identity="wrong-route"),
    ),
)
def test_sensor_guard_safety_and_route_faults_latch_without_auto_return(
    sample: KunweiAcquisitionSample,
) -> None:
    controller = _controller()
    controller.start(
        lease_id=11,
        episode_identity=22,
        route_identity=FORMAL_ROUTE_IDENTITY,
        monotonic_s=0.0,
    )
    with pytest.raises(AcquisitionError):
        controller.observe_kunwei(sample)
    assert controller.state == AcquisitionState.FAULT
    assert controller.auto_return_allowed is False


def test_formal_candidate_window_excludes_acquisition_and_requires_torque_state() -> None:
    rows = [
        {"formal_phase": "CONTACT_SEARCH", "receiver_state": 2},
        {"formal_phase": "TRACK", "receiver_state": 1},
        {"formal_phase": "TRACK", "receiver_state": 2},
    ]
    selected = _formal_track_state_torque_rows(rows)
    assert selected == [{"formal_phase": "TRACK", "receiver_state": 2}]


def test_formal_eligibility_validator_rejects_acquisition_rows() -> None:
    decision = FormalEligibilityValidator().evaluate(
        [{"capture_phase": "ACQUISITION", "receiver_state": 2}],
        recorder_health={},
        first_live_shadow=False,
    )
    assert decision.predicates["track_state_torque_window"] is False


def test_independent_resolver_binds_formal_identity_without_legacy_pointer() -> None:
    resolved = resolve_formal_current_state(ROOT)
    assert resolved["ok"] is True
    assert resolved["lineage"] == "tacdiffusion_formal_v4"
    assert resolved["current_stage_id"] == "formal_v4_no_contact_qualification"
    assert resolved["state"] == "OFFLINE_READY_LIVE_QUALIFICATION_PENDING"
    assert resolved["live_ready"] is False
    assert '"config/current_stage.json"' not in inspect.getsource(resolve_formal_current_state)


def test_direct_torque_source_is_constructible_only_from_handoff() -> None:
    anchor = (0.4, 0.1, 0.03, 3.12, 0.0, 0.0)
    handoff = AcquisitionHandoffV1(
        route_identity=FORMAL_ROUTE_IDENTITY,
        lease_id=11,
        episode_identity=22,
        contact_latch_sample_index=1050,
        stationary_dwell_s=0.1,
        anchor_pose_base=anchor,
    )
    tube = LiveTubeContract(
        center_base_m=anchor[:3],
        anchor_pose_base=anchor,
        u_axis_base=(1.0, 0.0, 0.0),
        v_axis_base=(0.0, 1.0, 0.0),
        safe_u_half_width_m=0.012,
        safe_v_half_width_m=0.012,
        normal_half_width_m=0.0255,
        orientation_tolerance_rad=0.1,
    )
    source = build_formal_direct_torque_tracking_source_after_handoff(
        tube,
        handoff,
        ContactAcquisitionContractV1(),
        fresh_actual_pose_base=anchor,
    )
    parsed = parse_live_receiver_source(source)
    assert parsed.formal_handoff_required is True
    assert parsed.model_inactive_expert_feedforward_allowed is True
    assert parsed.formal_handoff_max_mismatch_m == pytest.approx(0.0003)
    assert "local model_inactive_expert_feedforward_allowed = True" in source
    mode_zero = source[source.index("if model_mode == 0:") : source.index("elif model_mode == 1:")]
    assert "model_sequence != 0 or model_period_us != 0 or model_timestamp_us != 0" in mode_zero
    assert "if not model_inactive_expert_feedforward_allowed:" in mode_zero
    assert "guard_force_norm > 50.0 or guard_torque_norm > 4.0" in source
    assert parsed.formal_contact_entry_transition_profile == "formal_contact_entry_transition_v1"
    assert parsed.formal_contact_entry_transition_ticks == 25
    assert "local formal_handoff_anchor_pose = p[" in source
    assert "0.40000000000000002" in source
    assert "actual_TCP_force" not in source
    assert "get_tcp_force" not in source
    with pytest.raises(TypeError, match="acquisition handoff"):
        build_formal_direct_torque_tracking_source_after_handoff(
            tube,
            None,  # type: ignore[arg-type]
            ContactAcquisitionContractV1(),
            fresh_actual_pose_base=anchor,
        )
