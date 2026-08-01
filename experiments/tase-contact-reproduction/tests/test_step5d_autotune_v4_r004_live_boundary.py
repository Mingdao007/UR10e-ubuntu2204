"""Offline tests for the production-capable but fail-closed r004 boundary."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest

import run_step5d_autotune_v4_r004_campaign as campaign_cli
import step5d_autotune_v4_r004_live_writer as writer_cli
import step5d_bridge_authority as canonical_authority
from step5d_autotune_v4_r004.campaign import Attempt, promotion_decision
from step5d_autotune_v4_r004.contracts import load_contract, runtime_identity_limbs
from step5d_autotune_v4_r004.evidence import AttemptEvidence, PathEvidenceCollector, PathSample
from step5d_autotune_v4_r004.fake_rtde import FakeLiveKunweiTransport, FakeLiveRTDETransport
from step5d_autotune_v4_r004.identity import ControllerReadbackReceipt, RuntimeIdentityEvidence, Script1StartReceipt
from step5d_autotune_v4_r004.ledger import DurableCampaignLedger, verify_ledger_hash_chain
from step5d_autotune_v4_r004.live_campaign import LiveCampaignRunner
from step5d_autotune_v4_r004.prerequisites import LivePrerequisites, PrerequisiteError
from step5d_autotune_v4_r004.wire import AttemptKind
from step5d_autotune_v3.governance import read_proc_starttime_ticks
from step5d_eoat_profiles import load_new_eoat_profile


ROOT = Path(__file__).resolve().parents[1]


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
        final_q=(0.0,) * 6,
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
    kunwei = FakeLiveKunweiTransport(events=events)
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
        self.evidence = _evidence()

    def arm(self, *, ordinal: int, kind: AttemptKind, candidate_token: int, ledger: DurableCampaignLedger, resume: bool = False) -> None:
        del kind, candidate_token
        self.arm_ordinals.append(ordinal)
        self.rows_before_arm.append(len(ledger.rows))
        if not resume and not ledger.next_arm_allowed():
            raise AssertionError("fake writer bypassed durable row gate")

    def execute_attempt(self, attempt: Attempt, *, timeout_s: float = 180.0) -> AttemptEvidence:
        del timeout_s
        if attempt.ordinal == self.fail_ordinal:
            raise RuntimeError("injected interruption")
        return self.evidence


def test_live_campaign_orders_all_16_arms_and_requires_durable_prior_row(tmp_path: Path) -> None:
    contract = load_contract()
    ledger = DurableCampaignLedger(tmp_path / "ordered.jsonl")
    writer = _FakeCampaignWriter(epoch=1, script1_sha="c" * 64)
    result = LiveCampaignRunner(contract, ledger, writer).run()
    assert writer.arm_ordinals == list(range(1, 17))
    assert writer.rows_before_arm == list(range(16))
    assert len(result.rows) == 16
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
    )
    assert result.resumed
    assert second_writer.arm_ordinals[0] == 2
    assert len(result.rows) == 17
    assert result.rows[-16]["attempt_execution_id"] == interrupted["attempt_execution_id"]
    assert result.rows[-15]["logical_attempt_ordinal"] == 2
    assert result.rows[-15]["attempt_execution_id"] != interrupted["attempt_execution_id"]
    assert ledger.rows[-15]["previous_output_sha256"] == interrupted["output_ledger_sha256"]
    assert last_completed_seal == interrupted["previous_output_sha256"]
    verify_ledger_hash_chain(ledger.path)


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
