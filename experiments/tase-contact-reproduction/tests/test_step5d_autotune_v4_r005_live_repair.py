from __future__ import annotations

import json
from collections import deque
from types import SimpleNamespace
from typing import Callable

import pytest

from step5d_autotune_v4_r004.contracts import (
    RUNTIME_PROTOCOL,
    load_contract as load_r004_contract,
    runtime_identity_limbs,
)
from step5d_autotune_v4_r004.evidence import AttemptEvidence, PathSample
from step5d_autotune_v4_r004.home import HomeReference
from step5d_autotune_v4_r004.home_profile import load_fixed_home_profile
from step5d_autotune_v4_r004.session import SessionPhase
from step5d_autotune_v4_r004.timing import TimingEvidence
from step5d_autotune_v4_r004.transport import (
    R004OutputSnapshot,
    TransportError,
    expected_eoat,
)
from step5d_autotune_v4_r004.wire import AttemptKind, CommandMode, SessionCommand
import step5d_autotune_v4_r004_live_writer as writer_cli
from step5d_autotune_v4_r004_live_writer import LiveR004Writer
from step5d_autotune_v4_r005.contracts import Candidate
from step5d_autotune_v4_r005.live_adapter import R005LiveWriterAdapter
from step5d_autotune_v4_r005.runtime import Attempt


class _Authority:
    def __init__(self) -> None:
        self.assertions = 0

    def assert_active(self) -> None:
        self.assertions += 1


class _RTDE:
    def __init__(self, responses: list[object]) -> None:
        self.responses = deque(responses)
        self.close_count = 0
        self.open_count = 0

    def close(self) -> None:
        self.close_count += 1

    def open(self) -> None:
        self.open_count += 1

    def poll_output(self) -> R004OutputSnapshot | None:
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response  # type: ignore[return-value]


def _home_output(*, epoch: int = 7, observed_at_s: float = 100.0) -> R004OutputSnapshot:
    contract = load_r004_contract()
    profile = load_fixed_home_profile()
    runtime_hi, runtime_lo = runtime_identity_limbs(
        contract.raw["program"], contract.sha256, contract.campaign_fingerprint
    )
    payload, cog, tcp = expected_eoat()
    return R004OutputSnapshot(
        observed_at_s=observed_at_s,
        timestamp=observed_at_s,
        payload_kg=payload,
        payload_cog_m=tuple(cog),
        tcp_offset_m_rad=tuple(tcp),
        tcp_speed_m_s_rad_s=(0.0,) * 6,
        tcp_pose_m_rad=tuple(profile.pose),
        q_rad=(0.1, -0.2, 0.3, -0.4, 0.5, -0.6),
        qd_rad_s=(0.0,) * 6,
        safety_mode="NORMAL",
        robot_mode=7,
        runtime_state=2,
        consumed_packet_sequence=12,
        integer_echoes={
            24: epoch,
            25: 0,
            26: 78,
            27: 0,
            28: 0,
            29: 12,
            30: 1,
            31: 127,
            32: RUNTIME_PROTOCOL,
            33: runtime_hi,
            34: runtime_lo,
        },
    )


def _prearm_probe(responses: list[object]) -> tuple[LiveR004Writer, _RTDE, _Authority]:
    epoch = 7
    output = _home_output(epoch=epoch)
    rtde = _RTDE(responses)
    authority = _Authority()
    contract = load_r004_contract()
    profile = load_fixed_home_profile()
    writer = LiveR004Writer.__new__(LiveR004Writer)
    writer.contract = contract
    writer.prerequisites = SimpleNamespace(
        session_epoch=epoch,
        resident_session_id="r005-resident-test",
        controller=SimpleNamespace(script_sha256="a" * 64),
    )
    writer._opened = True
    writer._stopped = False
    writer._runtime_protocol = RUNTIME_PROTOCOL
    writer._authority_active = True
    writer.authority = authority
    writer.session = SimpleNamespace(
        phase=SessionPhase.READY_HOME_NEXT,
        observe_runtime=lambda _evidence: True,
    )
    writer._home = HomeReference(tuple(profile.pose), output.q_rad)
    writer._controller_transport = rtde
    writer._kunwei_transport = SimpleNamespace()
    writer._last_output = output
    writer._last_output_seen_wall_s = 1000.0
    writer._last_rtde_frame_mono_s = 1.0
    writer._last_rtde_frame_sequence = output.timestamp
    writer._last_identity_observed_s = 0.0
    writer._session_command_sequence = 0
    writer._ordinal = 0
    writer._wall_clock = lambda: 1000.0
    writer._mono_clock = lambda: 1.0
    writer._sleep = lambda _duration: None
    return writer, rtde, authority


def _arm_probe(
    poll: Callable[[], object],
) -> tuple[LiveR004Writer, list[dict[str, object]], list[str], dict[str, float]]:
    """Build only the typed arm seam; no socket, controller, or TP is opened."""

    clock = {"now": 0.0}
    sent: list[dict[str, object]] = []
    failures: list[str] = []
    session_arms: list[object] = []
    writer = LiveR004Writer.__new__(LiveR004Writer)
    writer.prerequisites = SimpleNamespace(session_epoch=7)
    writer._opened = True
    writer._stopped = False
    writer._packet_sequence = 100
    writer._session_command_sequence = 0
    writer._session_command = SessionCommand.HOLD
    writer._ordinal = 0
    writer._kind = AttemptKind.QUALIFICATION
    writer._candidate_token = 0
    writer._sticky_latched = 0
    writer._qualification_control = None
    writer._path_command_started_mono_s = None
    writer._path_rtde_origin_s = None
    writer._last_poll_was_fresh = False
    writer._mono_clock = lambda: clock["now"]
    writer._sleep = lambda duration: clock.__setitem__("now", clock["now"] + duration)
    writer._prearm_output = lambda *, allow_reconnect: SimpleNamespace()
    writer._read_sensor = lambda: SimpleNamespace()
    writer._poll_checked = lambda **_kwargs: poll()
    writer._fail_closed = lambda reason: failures.append(reason)

    def arm_session(request: object, **_kwargs: object) -> None:
        session_arms.append(request)

    writer.session = SimpleNamespace(arm=arm_session)

    def send_packet(
        _sensor: object,
        *,
        command_mode: CommandMode,
        proposed_qdot: tuple[float, ...] = (0.0,) * 6,
        **_kwargs: object,
    ) -> SimpleNamespace:
        packet = SimpleNamespace(
            sequence=writer._packet_sequence,
            command_mode=command_mode,
            proposed_qdot=tuple(proposed_qdot),
        )
        sent.append(
            {
                "sequence": packet.sequence,
                "command_mode": command_mode,
                "proposed_qdot": packet.proposed_qdot,
                "session_command": writer._session_command,
                "session_command_sequence": writer._session_command_sequence,
                "ordinal": writer._ordinal,
                "kind": writer._kind,
                "candidate_token": writer._candidate_token,
            }
        )
        writer._packet_sequence += 1
        return packet

    writer._send_packet = send_packet
    return writer, sent, failures, clock


def _arm_output(*, echo: bool) -> SimpleNamespace:
    return SimpleNamespace(
        integer_echoes={
            24: 7,
            25: 7 if echo else 0,
            26: 78,
            27: 77 if echo else 0,
            29: 1 if echo else 0,
            30: int(AttemptKind.BATCH_A),
        },
        tcp_speed_m_s_rad_s=(0.0,) * 6,
    )


def test_arm_delayed_echo_keeps_same_identity_alive_on_distinct_fresh_frames() -> None:
    writer: LiveR004Writer
    sent: list[dict[str, object]]
    failures: list[str]
    clock: dict[str, float]
    last_fresh_at = 0.0

    def poll() -> SimpleNamespace:
        nonlocal last_fresh_at
        fresh = clock["now"] - last_fresh_at >= 0.040 - 1e-12
        if fresh:
            last_fresh_at = clock["now"]
        writer._last_poll_was_fresh = fresh
        return _arm_output(echo=fresh and clock["now"] >= 0.120 - 1e-12)

    writer, sent, failures, clock = _arm_probe(poll)
    tick = writer.arm_unbounded(
        ordinal=7,
        kind=AttemptKind.BATCH_A,
        candidate_token=77,
    )

    assert not failures
    assert clock["now"] >= 0.120
    assert tick.packet_sequence == 103
    assert [packet["sequence"] for packet in sent] == [100, 101, 102, 103]
    assert all(packet["command_mode"] is CommandMode.HOLD for packet in sent)
    assert all(packet["proposed_qdot"] == (0.0,) * 6 for packet in sent)
    assert all(packet["session_command"] is SessionCommand.ARM for packet in sent)
    assert all(packet["session_command_sequence"] == 1 for packet in sent)
    assert all(packet["ordinal"] == 7 for packet in sent)
    assert all(packet["kind"] is AttemptKind.BATCH_A for packet in sent)
    assert all(packet["candidate_token"] == 77 for packet in sent)


def test_arm_cached_frames_do_not_advance_packet_sequence() -> None:
    writer: LiveR004Writer
    sent: list[dict[str, object]]
    failures: list[str]
    _clock: dict[str, float]
    poll_index = 0
    freshness = (False, False, False, True)

    def poll() -> SimpleNamespace:
        nonlocal poll_index
        current_index = poll_index
        writer._last_poll_was_fresh = freshness[current_index]
        poll_index += 1
        # A cached image may carry the desired echo, but it is still not an
        # ARM transition frame and must not cause a return or a new packet.
        return _arm_output(echo=current_index == 0 or writer._last_poll_was_fresh)

    writer, sent, failures, _clock = _arm_probe(poll)
    tick = writer.arm_unbounded(
        ordinal=7,
        kind=AttemptKind.BATCH_A,
        candidate_token=77,
    )

    assert not failures
    assert tick.packet_sequence == 101
    assert [packet["sequence"] for packet in sent] == [100, 101]


def test_arm_real_fresh_frame_stall_still_fails_closed_at_80ms() -> None:
    writer: LiveR004Writer
    sent: list[dict[str, object]]
    failures: list[str]
    clock: dict[str, float]

    def poll() -> SimpleNamespace:
        if clock["now"] >= 0.080:
            raise writer_cli.LiveWriterError(
                "runtime identity output is stale at the 80 ms hard limit"
            )
        writer._last_poll_was_fresh = False
        return _arm_output(echo=False)

    writer, sent, failures, clock = _arm_probe(poll)
    with pytest.raises(writer_cli.LiveWriterError, match="80 ms"):
        writer.arm_unbounded(
            ordinal=7,
            kind=AttemptKind.BATCH_A,
            candidate_token=77,
        )

    assert failures == ["runtime identity output is stale at the 80 ms hard limit"]
    assert [packet["sequence"] for packet in sent] == [100]
    assert writer._packet_sequence == 101


def test_home_only_prearm_socket_loss_reconnects_once_before_arm() -> None:
    fresh = _home_output(epoch=7, observed_at_s=101.0)
    writer, rtde, authority = _prearm_probe(
        [TransportError("canonical r004 RTDE output read failed: RTDE socket closed"), fresh]
    )

    result = writer._prearm_output(allow_reconnect=True)

    assert result is fresh
    assert rtde.close_count == 1
    assert rtde.open_count == 1
    assert writer._controller_transport is rtde
    assert authority.assertions >= 3
    assert writer._session_command_sequence == 0
    assert writer._ordinal == 0


def test_home_only_reconnect_waits_for_first_fresh_frame_after_empty_polls() -> None:
    fresh = _home_output(epoch=7, observed_at_s=101.0)
    writer, rtde, authority = _prearm_probe(
        [
            TransportError("canonical r004 RTDE output read failed: RTDE socket closed"),
            None,
            None,
            None,
            fresh,
        ]
    )

    result = writer._prearm_output(allow_reconnect=True)

    assert result is fresh
    assert rtde.close_count == 1
    assert rtde.open_count == 1
    assert authority.assertions >= 3
    assert writer._last_poll_was_fresh is True
    assert writer._session_command_sequence == 0
    assert writer._ordinal == 0


def test_home_only_reconnect_persistent_empty_output_fails_within_bound() -> None:
    writer, rtde, _authority = _prearm_probe(
        [
            TransportError("canonical r004 RTDE output read failed: RTDE socket closed"),
            *([None] * writer_cli.PREARM_FIRST_FRAME_MAX_POLLS),
        ]
    )

    with pytest.raises(RuntimeError, match="pre-ARM fresh runtime output timeout"):
        writer._prearm_output(allow_reconnect=True)

    assert rtde.close_count == 1
    assert rtde.open_count == 1
    assert writer._session_command_sequence == 0
    assert writer._ordinal == 0


def test_active_execution_missing_or_stale_output_remains_fail_closed() -> None:
    missing_writer, missing_rtde, _authority = _prearm_probe([None])
    missing_writer.session.phase = SessionPhase.RUNNING
    missing_writer._last_output = None
    with pytest.raises(RuntimeError, match="runtime output is missing"):
        missing_writer._poll_checked()
    assert missing_rtde.close_count == 0
    assert missing_rtde.open_count == 0

    stale_writer, stale_rtde, _authority = _prearm_probe([None])
    stale_writer.session.phase = SessionPhase.RUNNING
    stale_writer._wall_clock = lambda: 1000.081
    with pytest.raises(RuntimeError, match="stale"):
        stale_writer._poll_checked()
    assert stale_rtde.close_count == 0
    assert stale_rtde.open_count == 0


def test_mid_attempt_boundary_rejects_reconnect_even_when_enabled() -> None:
    writer, rtde, _authority = _prearm_probe([_home_output()])
    writer.session.phase = SessionPhase.RUNNING

    with pytest.raises(RuntimeError, match="READY_HOME_NEXT"):
        writer._prearm_output(allow_reconnect=True)

    assert rtde.close_count == 0
    assert rtde.open_count == 0


def test_r005_attempt_projection_keeps_motion_timing_diagnostics_bounded() -> None:
    timing = TimingEvidence(
        duration_s=60.0,
        successful_writer_publishes=30000,
        distinct_rtde_frames=30000,
        distinct_kunwei_frames=30000,
        distinct_tp_consumed_packet_echoes=30000,
        feedback_age_p99_s=0.001,
        max_fresh_gap_s=0.002,
    )

    class Writer:
        offline_test_mode = True

        def __init__(self) -> None:
            self._path_sample_sink = None

        def run_60s(self, attempt: Attempt) -> AttemptEvidence:
            del attempt
            assert self._path_sample_sink is not None
            for index in range(600):
                path_time_s = 0.05 + index * 0.1
                sequence = index + 1
                self._path_sample_sink(
                    PathSample(
                        observed_at_s=200.0 + path_time_s,
                        filtered_normal_n=5.0,
                        force_norm_n=5.0,
                        torque_norm_nm=0.01,
                        sensor_fresh=True,
                        state=25,
                        safety_normal=True,
                        path_time_s=path_time_s,
                        path_phase=6,
                        desired_xy_m=(0.0, 0.0),
                        actual_xy_m=(0.0001, 0.0),
                        desired_velocity_m_s=(0.0, 0.0),
                        actual_velocity_m_s=(0.0001, 0.0),
                        qdot=(0.1,) * 6,
                        actual_qd=(0.1,) * 6,
                        source_ages_s={
                            "writer": 0.001,
                            "rtde": 0.001,
                            "kunwei": 0.001,
                            "tp": 0.001,
                        },
                        source_sequences={
                            "writer": sequence,
                            "rtde": sequence,
                            "kunwei": sequence,
                            "tp": sequence,
                        },
                        qd_lag_s=0.001,
                    )
                )
            return AttemptEvidence(
                complete_bins=550,
                effective_rate_hz=500.0,
                p99_packet_interval_s=0.002,
                max_packet_interval_s=0.002,
                mae_n=0.1,
                objective=0.1,
                safety_gate_passed=True,
                contact_gate_passed=True,
                return_gate_passed=True,
                home_proof={"stationary": True},
                path_samples=600,
                path_bin_ids=tuple(range(550)),
                evidence_sha256="c" * 64,
                path_duration_s=60.0,
                path_phase=6,
                xy_error_p95_m=0.0001,
                xy_error_max_m=0.0001,
                endpoint_error_max_m=0.0001,
                qd_correlation=0.99,
                qd_lag_s=0.001,
                timing_evidence=timing,
                metrics={
                    "path_observed_span_s": 59.9,
                    "path_physical_span_s": 59.9,
                    "path_coverage_interval_s": 0.1,
                    "path_cadence_hz": 10.0,
                    "path_duration_basis": "test_clock",
                    "velocity_error_p95_m_s": 0.0001,
                    "qd_joint_correlations": (0.99,) * 6,
                    "raw_samples": [{"sample": index} for index in range(32)],
                    "sample_identity_ids": ["d" * 64] * 10000,
                },
            )

    writer = Writer()
    adapter = R005LiveWriterAdapter(writer, contract=SimpleNamespace())
    writer._path_sample_sink = adapter.observe_r004_path_sample
    attempt = Attempt(
        epoch=1,
        attempt_sequence=1,
        candidate=Candidate(),
        kind="BO_TRIAL",
        dispatch_sequence=1,
        request_uid="request-1",
        execution_id="r005-execution-1",
    )

    result = adapter.run_60s(attempt)
    metrics = dict(result.metrics)

    assert metrics["motion_metrics_version"] == "r005-bounded-motion-diagnostics-v1"
    assert metrics["path_cadence_hz"] == pytest.approx(10.0)
    assert metrics["xy_error_p95_m"] == pytest.approx(0.0001)
    assert metrics["endpoint_error_max_m"] == pytest.approx(0.0001)
    assert metrics["velocity_error_p95_m_s"] == pytest.approx(0.0001)
    assert metrics["qd_correlation"] == pytest.approx(0.99)
    assert metrics["qd_lag_s"] == pytest.approx(0.001)
    assert metrics["timing_evidence"]["distinct_rtde_frames"] == 30000
    assert "raw_samples" not in metrics
    assert "sample_identity_ids" not in metrics
    assert "sample_identity_ids" not in metrics["force_objective"]
    assert len(metrics["qd_joint_correlations"]) == 6
    assert len(json.dumps(metrics, sort_keys=True)) < 10000
