"""Offline tests for the production-capable but fail-closed r004 boundary."""

from __future__ import annotations

import hashlib
import inspect
import os
from dataclasses import replace
from pathlib import Path

import pytest

import run_step5d_autotune_v4_r004_campaign as campaign_cli
import step5d_autotune_v4_r004_live_writer as writer_cli
import step5d_bridge_authority as canonical_authority
from step5d_autotune_v4_r004.campaign import Attempt, promotion_decision
from step5d_autotune_v4_r004.contracts import load_contract, runtime_identity_limbs
from step5d_autotune_v4_r004.evidence import (
    AttemptEvidence,
    PathEvidenceCollector,
    PathSample,
    QualificationEvidence,
    QualificationEvidenceCollector,
    QualificationSample,
)
from step5d_autotune_v4_r004.fake_rtde import FakeLiveKunweiTransport, FakeLiveRTDETransport
from step5d_autotune_v4_r004.identity import ControllerReadbackReceipt, RuntimeIdentityEvidence, Script1StartReceipt
from step5d_autotune_v4_r004.ledger import DurableCampaignLedger, verify_ledger_hash_chain
from step5d_autotune_v4_r004.live_campaign import LiveCampaignRunner
from step5d_autotune_v4_r004.prerequisites import LivePrerequisites, PrerequisiteError
from step5d_autotune_v4_r004.timing import TimingEvidence
from step5d_autotune_v4_r004.wire import AttemptKind, SessionCommand
from step5d_autotune_v3.governance import read_proc_starttime_ticks
from step5d_eoat_profiles import load_new_eoat_profile


ROOT = Path(__file__).resolve().parents[1]


def test_path_clock_starts_at_tp_state25_not_early_path_command() -> None:
    source = inspect.getsource(writer_cli.LiveR004Writer.execute_attempt)
    assert (
        "state == 25\n"
        "                        and mode is CommandMode.PATH\n"
        "                        and self._path_command_started_mono_s is None"
    ) in source


def test_path_evidence_uses_common_tp_rtde_boundary_clock() -> None:
    source = inspect.getsource(writer_cli.LiveR004Writer.execute_attempt)
    assert "PathEvidenceCollector(require_path_boundary=True)" in source
    assert "path_collector.mark_path_start(" in source
    assert "rtde_timestamp_s=output.timestamp" in source
    assert "tp_sequence=output.consumed_packet_sequence" in source
    assert "path_clock_time_s = max(0.0, output.timestamp - self._path_rtde_origin_s)" in source
    assert "accepted_path_sample = path_collector.observe(observed_path_sample)" in source


def test_cached_state25_at_exact_path_end_uses_zero_path_fence_until_terminal() -> None:
    source = inspect.getsource(writer_cli.LiveR004Writer.execute_attempt)
    assert "path_elapsed_s = max(0.0, now - self._path_command_started_mono_s)" in source
    assert "path_end_fence = path_elapsed_s >= 60.0" in source
    assert "if path_end_fence:" in source
    assert "mode = CommandMode.PATH" in source
    assert "setpoint = 5.0" in source
    assert "qdot = (0.0,) * 6" in source
    assert "and path_elapsed_s < 60.0" in source
    assert 'float(sample_kwargs["path_time_s"]) < 60.0' in source
    assert "observed_path_sample.path_time_s < 60.0" in source
    assert "if state in {78, 80, 90}:" in source
    assert source.index("if path_end_fence:") < source.index("self._qualification_control.step")


def _triplet() -> dict[str, str]:
    return {
        suffix.removeprefix("."): hashlib.sha256(
            (ROOT / f"programs/step5/step5d/step5d_strict_rnn_autotune_v4_r004{suffix}").read_bytes()
        ).hexdigest()
        for suffix in (".script", ".txt", ".urp")
    }


def _prerequisites(*, epoch: int = 1, script1_sha: str = "c" * 64, observed_controller: float = 90.0, observed_runtime: float = 95.0) -> LivePrerequisites:
    contract = load_contract()
    profile = load_new_eoat_profile()
    triplet = _triplet()
    hi, lo = runtime_identity_limbs(contract.raw["program"], contract.sha256, contract.campaign_fingerprint)
    controller = ControllerReadbackReceipt(
        receipt_sha256="b" * 64,
        program=contract.raw["program"],
        controller_target=contract.raw["script2"]["controller_target"],
        script_sha256=triplet["script"],
        txt_sha256=triplet["txt"],
        urp_sha256=triplet["urp"],
        observed_at_s=observed_controller,
        runtime_protocol=606004,
        runtime_digest_hi=hi,
        runtime_digest_lo=lo,
        eoat_identity_sha256=contract.eoat_sha256,
        payload_kg=profile.payload_kg,
        payload_cog_m=profile.cog_m,
        tcp_offset_m_rad=profile.controller_tcp_m_rad,
        safety_mode="NORMAL",
        stationary=True,
        route_id="r004-test-route",
    )
    script1 = Script1StartReceipt(
        receipt_sha256=script1_sha,
        script_sha256=contract.script1_sha256["script"],
        observed_at_s=observed_controller,
        final_pose=(0.487834547, 0.129337053, 0.033, 3.120752062, 0.0, 0.068626833),
        final_q=(
            0.6282875537872314,
            -1.8318835697569789,
            -2.546542167663574,
            -0.3096270126155396,
            1.530116319656372,
            -0.9408276716815394,
        ),
        stationary=True,
        safety_mode="NORMAL",
        eoat_identity_sha256=contract.eoat_sha256,
    )
    runtime = RuntimeIdentityEvidence(
        program=contract.raw["program"],
        script_sha256=triplet["script"],
        runtime_protocol=606004,
        runtime_digest_hi=hi,
        runtime_digest_lo=lo,
        session_epoch=epoch,
        resident_session_id=f"r004-resident-{epoch}",
        program_running=True,
        uninterrupted=True,
        observed_at_s=observed_runtime,
    )
    return LivePrerequisites(
        contract=contract,
        controller=controller,
        script1=script1,
        runtime=runtime,
        expected_triplet=triplet,
        route_id="r004-test-route",
        session_epoch=epoch,
        resident_session_id=f"r004-resident-{epoch}",
        input_baseline_ledger_sha256="d" * 64,
    )


def _evidence() -> AttemptEvidence:
    collector = PathEvidenceCollector()
    for index in range(5500):
        collector.observe(
            PathSample(
                observed_at_s=index * 0.01,
                filtered_normal_n=5.0,
                force_norm_n=5.0,
                torque_norm_nm=0.1,
                sensor_fresh=True,
                state=25,
                safety_normal=True,
            )
        )
    return collector.finalize(
        return_gate_passed=True,
        contact_gate_passed=True,
        home_proof={"stationary": True, "return_guard": 127},
        mae_n=0.2,
        objective=0.8,
    )


def _qualification_evidence() -> QualificationEvidence:
    collector = QualificationEvidenceCollector()
    for sample in (
        QualificationSample(0.000, 0.5, 1.0, True, True, 20, 0, 0),
        QualificationSample(0.002, 1.0, 1.0, True, True, 21, 1, 1),
        QualificationSample(0.004, 5.0, 5.0, True, True, 21, 3, 1),
        QualificationSample(0.006, 0.0, 1.0, True, True, 78, 0, 1),
    ):
        collector.observe(sample)
    return collector.finalize(
        return_gate_passed=True,
        home_proof={"stationary": True, "return_guard": 127},
        timing_evidence=TimingEvidence(
            duration_s=0.006,
            successful_writer_publishes=3,
            distinct_rtde_frames=3,
            distinct_kunwei_frames=6,
            distinct_tp_consumed_packet_echoes=3,
            feedback_age_p99_s=0.002,
            max_fresh_gap_s=0.002,
        ),
    )


def test_live_cli_requires_ack_before_any_hardware_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("offline CLI refusal attempted a socket call")

    monkeypatch.setattr("socket.create_connection", forbidden)
    with pytest.raises(SystemExit) as writer_exit:
        writer_cli.main(["--mode", "live"])
    assert writer_exit.value.code == 2
    with pytest.raises(SystemExit) as campaign_exit:
        campaign_cli.main(["--run-live"])
    assert campaign_exit.value.code == 2


def test_receipt_expiry_and_runtime_digest_epoch_mismatch_fail_before_open(tmp_path: Path) -> None:
    contract = load_contract()
    expired = _prerequisites(observed_controller=0.0, observed_runtime=1.0)
    fake_rtde = FakeLiveRTDETransport(contract)
    fake_kunwei = FakeLiveKunweiTransport()
    writer = writer_cli.LiveR004Writer(
        expired,
        authority_root=tmp_path / "expired-authority",
        route_id="r004-test-route",
        attempt_id="step5d-v4-r004-expired-attempt",
        controller_transport=fake_rtde,
        kunwei_transport=fake_kunwei,
    )
    with pytest.raises(writer_cli.LiveWriterError, match="stale"):
        writer.open(live_ack=writer_cli.LIVE_ACK, now_s=400.0)
    assert not fake_rtde.opened and not fake_kunwei.opened

    valid = _prerequisites()
    bad_runtime = replace(valid.runtime, runtime_digest_hi=valid.runtime.runtime_digest_hi + 1)
    mismatched = replace(valid, runtime=bad_runtime)
    fake_rtde = FakeLiveRTDETransport(contract)
    fake_kunwei = FakeLiveKunweiTransport()
    writer = writer_cli.LiveR004Writer(
        mismatched,
        authority_root=tmp_path / "digest-authority",
        route_id="r004-test-route",
        attempt_id="step5d-v4-r004-digest-attempt",
        controller_transport=fake_rtde,
        kunwei_transport=fake_kunwei,
    )
    with pytest.raises(writer_cli.LiveWriterError):
        writer.open(live_ack=writer_cli.LIVE_ACK, now_s=100.0)
    assert not fake_rtde.opened and not fake_kunwei.opened


def test_r004_uses_canonical_resource_and_rejects_second_writer(tmp_path: Path) -> None:
    contract = load_contract()
    owner_pid = os.getppid()
    owner_starttime = read_proc_starttime_ticks(owner_pid)
    assert owner_starttime is not None
    first_id = "step5d-v4-r004-authority-a"
    second_id = "step5d-v4-r004-authority-b"
    first = canonical_authority.begin(
        tmp_path,
        first_id,
        owner_pid,
        owner_starttime,
        worktree_root=str(ROOT),
        launch_basis_path=str(contract.path),
        launch_basis_sha256=contract.sha256,
        resource_id=canonical_authority.DEFAULT_RESOURCE_ID,
    )
    try:
        with pytest.raises(canonical_authority.BridgeAuthorityError, match="another canonical owner"):
            canonical_authority.begin(
                tmp_path,
                second_id,
                owner_pid,
                owner_starttime,
                worktree_root=str(ROOT),
                launch_basis_path=str(contract.path),
                launch_basis_sha256=contract.sha256,
                resource_id=canonical_authority.DEFAULT_RESOURCE_ID,
            )
    finally:
        canonical_authority.revoke(
            tmp_path,
            first_id,
            owner_pid,
            owner_starttime,
            reason="failed",
            resource_id=canonical_authority.DEFAULT_RESOURCE_ID,
        )


def test_live_writer_cleanup_sends_stop_zeros_closes_routes_and_releases_lease(tmp_path: Path) -> None:
    contract = load_contract()
    events: list[str] = []
    rtde = FakeLiveRTDETransport(contract, events=events)
    kunwei = FakeLiveKunweiTransport(events=events, observed_clock=lambda: 0.01)
    writer = writer_cli.LiveR004Writer(
        _prerequisites(),
        authority_root=tmp_path / "authority",
        route_id="r004-test-route",
        attempt_id="step5d-v4-r004-cleanup-attempt",
        controller_transport=rtde,
        kunwei_transport=kunwei,
        wall_clock=lambda: 100.0,
        mono_clock=lambda: 0.01,
    )
    writer.open(live_ack=writer_cli.LIVE_ACK, now_s=100.0)
    ledger = DurableCampaignLedger(tmp_path / "ledger.jsonl")
    writer.arm(ordinal=1, kind=AttemptKind.QUALIFICATION, candidate_token=17, ledger=ledger)
    arm_packet = rtde.sent_packets[-1]
    assert arm_packet[0][2] > 0.0
    assert arm_packet[0][3] == 1.0
    assert arm_packet[0][4] == 0.0
    writer.close()
    assert rtde.closed and kunwei.closed and kunwei.stop_stream_sent
    assert writer.authority is not None and not writer.authority_active
    writer.authority.assert_revoked()
    assert events[-1] == "rtde.close"
    assert max(index for index, event in enumerate(events) if event == "rtde.send") < events.index("kunwei.stop_stream") < events.index("rtde.close")
    last_doubles, last_integers = rtde.sent_packets[-1]
    assert last_integers[3] == 3
    assert last_doubles[13:19] == (0.0,) * 6


class _FakeCampaignWriter:
    def __init__(self, *, epoch: int, script1_sha: str, fail_ordinal: int | None = None) -> None:
        self.session_epoch = epoch
        self.script1_receipt_sha256 = script1_sha
        self.controller_receipt_sha256 = "b" * 64
        self.input_baseline_ledger_sha256 = "d" * 64
        self.fail_ordinal = fail_ordinal
        self.arm_ordinals: list[int] = []
        self.rows_before_arm: list[int] = []
        self.committed_counts: list[int] = []
        self.evidence = _qualification_evidence()

    def bind_committed_qualifications(self, ledger: DurableCampaignLedger) -> int:
        return sum(
            row.get("status") == "completed" and row.get("qualification_passed") is True
            for row in ledger.rows
        )

    def commit_qualification_success(self, *, ordinal: int, ledger: DurableCampaignLedger) -> None:
        count = self.bind_committed_qualifications(ledger)
        assert count == ordinal
        self.committed_counts.append(count)

    def arm(self, *, ordinal: int, kind: AttemptKind, candidate_token: int, ledger: DurableCampaignLedger, resume: bool = False) -> None:
        del kind, candidate_token
        self.arm_ordinals.append(ordinal)
        self.rows_before_arm.append(len(ledger.rows))
        if not resume and not ledger.next_arm_allowed():
            raise AssertionError("fake writer bypassed durable row gate")

    def execute_attempt(self, attempt: Attempt, *, timeout_s: float = 180.0) -> QualificationEvidence:
        del timeout_s
        if attempt.ordinal == self.fail_ordinal:
            raise RuntimeError("injected interruption")
        return self.evidence


def test_live_campaign_runs_only_three_qualifications_with_durable_prior_row(tmp_path: Path) -> None:
    contract = load_contract()
    ledger = DurableCampaignLedger(tmp_path / "ordered.jsonl")
    writer = _FakeCampaignWriter(epoch=1, script1_sha="c" * 64)
    result = LiveCampaignRunner(contract, ledger, writer).run()
    assert writer.arm_ordinals == [1, 2, 3]
    assert writer.rows_before_arm == [0, 1, 2]
    assert writer.committed_counts == [1, 2, 3]
    assert len(result.rows) == 3
    assert result.qualification_only and result.qualification_complete
    assert result.full_campaign_blocked and not result.promotion["promotion_allowed"]
    rows = verify_ledger_hash_chain(ledger.path)
    assert all(row["durability"] == {"fsynced": True, "cold_read": True, "hash_verified": True} for row in rows)
    assert all(row["output_ledger_sha256"] for row in rows)


def test_live_campaign_interruption_resume_retries_same_ordinal_with_new_identity(tmp_path: Path) -> None:
    contract = load_contract()
    ledger = DurableCampaignLedger(tmp_path / "resume.jsonl")
    first_writer = _FakeCampaignWriter(epoch=1, script1_sha="c" * 64, fail_ordinal=2)
    with pytest.raises(Exception, match="injected interruption"):
        LiveCampaignRunner(contract, ledger, first_writer).run()
    interrupted = ledger.rows[-1]
    assert interrupted["logical_attempt_ordinal"] == 2
    assert interrupted["gp_eligible"] is False
    last_completed_seal = ledger.rows[-2]["output_ledger_sha256"]

    second_writer = _FakeCampaignWriter(epoch=2, script1_sha="e" * 64)
    result = LiveCampaignRunner(contract, ledger, second_writer).resume(
        new_epoch=2,
        new_script1_receipt_sha256="e" * 64,
        stop_after_ordinal=2,
    )
    assert result.resumed
    assert second_writer.arm_ordinals == [2]
    assert len(result.rows) == 3
    assert result.rows[1]["attempt_execution_id"] == interrupted["attempt_execution_id"]
    assert result.rows[2]["logical_attempt_ordinal"] == 2
    assert result.rows[2]["attempt_execution_id"] != interrupted["attempt_execution_id"]
    assert ledger.rows[2]["previous_output_sha256"] == interrupted["output_ledger_sha256"]
    assert last_completed_seal == interrupted["previous_output_sha256"]
    verify_ledger_hash_chain(ledger.path)


def test_live_writer_blocks_ordinal_four_before_any_rtde_write(tmp_path: Path) -> None:
    contract = load_contract()
    rtde = FakeLiveRTDETransport(contract)
    kunwei = FakeLiveKunweiTransport()
    writer = writer_cli.LiveR004Writer(
        _prerequisites(),
        authority_root=tmp_path / "authority",
        route_id="r004-test-route",
        attempt_id="step5d-v4-r004-ordinal4-block",
        controller_transport=rtde,
        kunwei_transport=kunwei,
        wall_clock=lambda: 100.0,
        mono_clock=lambda: 0.01,
    )
    writer.open(live_ack=writer_cli.LIVE_ACK, now_s=100.0)
    before = len(rtde.sent_packets)
    try:
        with pytest.raises(writer_cli.LiveWriterError, match="qualification-only"):
            writer.arm(
                ordinal=4,
                kind=AttemptKind.BATCH_A,
                candidate_token=17,
                ledger=DurableCampaignLedger(tmp_path / "ledger.jsonl"),
            )
        assert len(rtde.sent_packets) == before
    finally:
        writer.close()


def test_fake_qualification_models_state21_canonical_control_and_real_evidence(tmp_path: Path) -> None:
    class Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def now(self) -> float:
            return self.value

        def sleep(self, duration: float) -> None:
            self.value += duration

    contract = load_contract()
    clock = Clock()
    rtde = FakeLiveRTDETransport(contract)
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    writer = writer_cli.LiveR004Writer(
        _prerequisites(),
        authority_root=tmp_path / "authority",
        route_id="r004-test-route",
        attempt_id="step5d-v4-r004-canonical-qualification",
        controller_transport=rtde,
        kunwei_transport=kunwei,
        wall_clock=lambda: 100.0,
        mono_clock=clock.now,
        sleep=clock.sleep,
    )
    ledger = DurableCampaignLedger(tmp_path / "ledger.jsonl")
    writer.open(live_ack=writer_cli.LIVE_ACK, now_s=100.0)
    try:
        attempt = __import__(
            "step5d_autotune_v4_r004.campaign", fromlist=["build_campaign_plan"]
        ).build_campaign_plan(contract)[0]
        writer.arm(
            ordinal=1,
            kind=AttemptKind.QUALIFICATION,
            candidate_token=17,
            ledger=ledger,
        )
        evidence = writer.execute_attempt(attempt)
        assert isinstance(evidence, QualificationEvidence)
        assert evidence.qualification_passed
        state21_packets = [
            packet
            for state, packet in zip(rtde.sent_from_states, rtde.sent_packets, strict=True)
            if state == 21
        ]
        assert state21_packets
        assert all(packet[1][1] != 0 for packet in state21_packets)
        assert any(packet[1][1] == 1 for packet in state21_packets)
        assert any(packet[1][1] == 3 for packet in state21_packets)
        assert any(packet[1][2] == 1 for packet in state21_packets)
        assert max(packet[0][20] for packet in state21_packets) > 1.0
        assert any(any(abs(value) > 0.0 for value in packet[0][13:19]) for packet in state21_packets)
    finally:
        writer.close()


def test_real_path_timing_evidence_and_promotion_remain_fail_closed() -> None:
    evidence = _evidence()
    assert evidence.complete_bins == 550
    assert evidence.effective_rate_hz >= 75.0
    assert evidence.p99_packet_interval_s <= 0.020
    assert evidence.max_packet_interval_s < 0.080
    assert evidence.safety_gate_passed and evidence.contact_gate_passed and evidence.return_gate_passed

    contract = load_contract()
    plan = __import__("step5d_autotune_v4_r004.campaign", fromlist=["build_campaign_plan"]).build_campaign_plan(contract)
    anchor_uid = plan[0].candidate.uid
    candidate_uid = plan[4].candidate.uid
    rows = [
        {"phase": "BATCH_A", "candidate_uid": anchor_uid, "objective": 1.0, "gp_eligible": True},
        {"phase": "BATCH_B", "candidate_uid": anchor_uid, "objective": 1.0, "gp_eligible": True},
        {"phase": "BATCH_A", "candidate_uid": candidate_uid, "objective": 0.9, "gp_eligible": True},
    ]
    rows.extend(
        {
            "phase": "RETEST",
            "candidate_uid": candidate_uid,
            "objective": 0.9,
            "mae_n": 0.2,
            "completed": True,
            "complete_bins": evidence.complete_bins,
            "effective_rate_hz": evidence.effective_rate_hz,
            "p99_packet_interval_s": evidence.p99_packet_interval_s,
            "max_packet_interval_s": evidence.max_packet_interval_s,
            "safety_gate_passed": evidence.safety_gate_passed,
            "contact_gate_passed": evidence.contact_gate_passed,
            "return_gate_passed": evidence.return_gate_passed,
            "gp_eligible": True,
        }
        for _ in range(3)
    )
    decision = promotion_decision(rows, anchor_uid=anchor_uid)
    assert decision["promotion_allowed"] and decision["anchor_mutated"] is False


def test_gc_collection_precedes_arm_session_mutation_and_arm_packet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def now(self) -> float:
            return self.value

        def sleep(self, duration: float) -> None:
            self.value += duration

    contract = load_contract()
    clock = Clock()
    events: list[str] = []
    rtde = FakeLiveRTDETransport(contract)
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    writer = writer_cli.LiveR004Writer(
        _prerequisites(),
        authority_root=tmp_path / "authority",
        route_id="r004-test-route",
        attempt_id="step5d-v4-r004-gc-order",
        controller_transport=rtde,
        kunwei_transport=kunwei,
        wall_clock=lambda: 100.0,
        mono_clock=clock.now,
        sleep=clock.sleep,
    )
    writer.open(live_ack=writer_cli.LIVE_ACK, now_s=100.0)
    events.clear()

    def collect() -> int:
        events.append("gc.collect")
        return 0

    monkeypatch.setattr(writer_cli.gc, "collect", collect)
    session_arm = writer.session.arm

    def record_session_arm(
        request: object, *, ledger_ready: bool, allow_unbounded: bool = False
    ) -> object:
        events.append("session.arm")
        return session_arm(
            request,
            ledger_ready=ledger_ready,
            allow_unbounded=allow_unbounded,
        )

    monkeypatch.setattr(writer.session, "arm", record_session_arm)
    send_packet = writer._send_packet

    def record_send_packet(*args: object, **kwargs: object) -> object:
        events.append("arm.packet")
        return send_packet(*args, **kwargs)

    monkeypatch.setattr(writer, "_send_packet", record_send_packet)
    try:
        writer.arm(
            ordinal=1,
            kind=AttemptKind.QUALIFICATION,
            candidate_token=17,
            ledger=DurableCampaignLedger(tmp_path / "ledger.jsonl"),
        )
        assert events == ["gc.collect", "session.arm", "arm.packet", "arm.packet"]
    finally:
        writer.close()


def test_long_observable_gc_never_runs_after_arm_before_execute_packet_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def now(self) -> float:
            return self.value

        def sleep(self, duration: float) -> None:
            self.value += duration

    contract = load_contract()
    clock = Clock()
    phase = {"value": "pre-arm"}
    events: list[tuple[str, str]] = []
    rtde = FakeLiveRTDETransport(contract)
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    writer = writer_cli.LiveR004Writer(
        _prerequisites(),
        authority_root=tmp_path / "authority",
        route_id="r004-test-route",
        attempt_id="step5d-v4-r004-gc-window",
        controller_transport=rtde,
        kunwei_transport=kunwei,
        wall_clock=lambda: 100.0,
        mono_clock=clock.now,
        sleep=clock.sleep,
    )
    writer.open(live_ack=writer_cli.LIVE_ACK, now_s=100.0)
    events.clear()

    def long_collect() -> int:
        events.append(("gc.collect", phase["value"]))
        clock.value += 0.200
        return 0

    monkeypatch.setattr(writer_cli.gc, "collect", long_collect)
    class StubQualificationControl:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    monkeypatch.setattr(writer_cli, "CanonicalQualificationControl", StubQualificationControl)
    send_packet = writer._send_packet

    def record_send_packet(*args: object, **kwargs: object) -> object:
        events.append(("packet", phase["value"]))
        return send_packet(*args, **kwargs)

    monkeypatch.setattr(writer, "_send_packet", record_send_packet)
    poll_checked = writer._poll_checked

    def record_poll_checked(*args: object, **kwargs: object) -> object:
        events.append(("poll", phase["value"]))
        if phase["value"] == "execute" and any(
            kind == "poll" and event_phase == "execute"
            for kind, event_phase in events[:-1]
        ):
            raise RuntimeError("injected post-service poll failure")
        return poll_checked(*args, **kwargs)

    monkeypatch.setattr(writer, "_poll_checked", record_poll_checked)
    try:
        writer.arm(
            ordinal=1,
            kind=AttemptKind.QUALIFICATION,
            candidate_token=17,
            ledger=DurableCampaignLedger(tmp_path / "ledger.jsonl"),
        )
        phase["value"] = "execute"
        attempt = __import__(
            "step5d_autotune_v4_r004.campaign", fromlist=["build_campaign_plan"]
        ).build_campaign_plan(contract)[0]
        with pytest.raises(writer_cli.LiveWriterError, match="injected post-service poll failure"):
            writer.execute_attempt(attempt)

        arm_packet = events.index(("packet", "pre-arm"))
        first_execute_poll = events.index(("poll", "execute"))
        first_execute_packet = events.index(("packet", "execute"))
        assert events.index(("gc.collect", "pre-arm")) < arm_packet
        assert arm_packet < first_execute_poll < first_execute_packet
        assert not any(
            kind == "gc.collect"
            for kind, _phase in events[arm_packet : first_execute_packet + 1]
        )
    finally:
        writer.close()


def test_gc_collection_failure_fails_closed_without_arm_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def now(self) -> float:
            return self.value

        def sleep(self, duration: float) -> None:
            self.value += duration

    contract = load_contract()
    clock = Clock()
    rtde = FakeLiveRTDETransport(contract)
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    writer = writer_cli.LiveR004Writer(
        _prerequisites(),
        authority_root=tmp_path / "authority",
        route_id="r004-test-route",
        attempt_id="step5d-v4-r004-gc-failure",
        controller_transport=rtde,
        kunwei_transport=kunwei,
        wall_clock=lambda: 100.0,
        mono_clock=clock.now,
        sleep=clock.sleep,
    )
    writer.open(live_ack=writer_cli.LIVE_ACK, now_s=100.0)
    before = len(rtde.sent_packets)

    def fail_collect() -> int:
        raise RuntimeError("injected gc failure")

    monkeypatch.setattr(writer_cli.gc, "collect", fail_collect)
    try:
        with pytest.raises(writer_cli.LiveWriterError, match="injected gc failure"):
            writer.arm(
                ordinal=1,
                kind=AttemptKind.QUALIFICATION,
                candidate_token=17,
                ledger=DurableCampaignLedger(tmp_path / "ledger.jsonl"),
            )
        cleanup_packets = rtde.sent_packets[before:]
        assert cleanup_packets
        assert all(packet[1][3] != int(SessionCommand.ARM) for packet in cleanup_packets)
        assert any(packet[1][3] == int(SessionCommand.STOP) for packet in cleanup_packets)
        assert writer.session.phase.value == "stopped"
        assert writer._failed_closed is True
    finally:
        writer.close()


@pytest.mark.parametrize("was_enabled", [True, False])
def test_execute_attempt_restores_preexisting_gc_state_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, was_enabled: bool
) -> None:
    class Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def now(self) -> float:
            return self.value

        def sleep(self, duration: float) -> None:
            self.value += duration

    contract = load_contract()
    clock = Clock()
    rtde = FakeLiveRTDETransport(contract)
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    writer = writer_cli.LiveR004Writer(
        _prerequisites(),
        authority_root=tmp_path / "authority",
        route_id="r004-test-route",
        attempt_id="step5d-v4-r004-gc-restore",
        controller_transport=rtde,
        kunwei_transport=kunwei,
        wall_clock=lambda: 100.0,
        mono_clock=clock.now,
        sleep=clock.sleep,
    )
    writer.open(live_ack=writer_cli.LIVE_ACK, now_s=100.0)
    monkeypatch.setattr(writer_cli.gc, "collect", lambda: 0)
    writer.arm(
        ordinal=1,
        kind=AttemptKind.QUALIFICATION,
        candidate_token=17,
        ledger=DurableCampaignLedger(tmp_path / "ledger.jsonl"),
    )

    gc_state = {"enabled": was_enabled}
    gc_calls: list[str] = []

    def isenabled() -> bool:
        return gc_state["enabled"]

    def disable() -> None:
        gc_calls.append("disable")
        gc_state["enabled"] = False

    def enable() -> None:
        gc_calls.append("enable")
        gc_state["enabled"] = True

    monkeypatch.setattr(writer_cli.gc, "isenabled", isenabled)
    monkeypatch.setattr(writer_cli.gc, "disable", disable)
    monkeypatch.setattr(writer_cli.gc, "enable", enable)

    class StubQualificationControl:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    monkeypatch.setattr(writer_cli, "CanonicalQualificationControl", StubQualificationControl)

    def fail_poll(*_args: object, **_kwargs: object) -> object:
        assert gc_state["enabled"] is False
        raise RuntimeError("injected execute failure")

    monkeypatch.setattr(writer, "_poll_checked", fail_poll)
    try:
        attempt = __import__(
            "step5d_autotune_v4_r004.campaign", fromlist=["build_campaign_plan"]
        ).build_campaign_plan(contract)[0]
        with pytest.raises(writer_cli.LiveWriterError, match="injected execute failure"):
            writer.execute_attempt(attempt)
        assert gc_calls == (["disable", "enable"] if was_enabled else ["disable"])
        assert gc_state["enabled"] is was_enabled
    finally:
        writer.close()
