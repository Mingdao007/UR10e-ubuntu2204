"""Deterministic offline regression tests for the isolated V4 r004 closure."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from step5d_autotune_v4_r004.baseline import (
    BaselineObservation,
    BaselinePhase,
    BaselineState,
    step_baseline,
)
from step5d_autotune_v4_r004.campaign import (
    AttemptOutcome,
    CampaignRunner,
    build_campaign_plan,
    promotion_decision,
)
from step5d_autotune_v4_r004.contracts import Candidate, load_contract
from step5d_autotune_v4_r004.fake_rtde import FakeRTDE
from step5d_autotune_v4_r004.freshness import PacketFreshnessGuard
from step5d_autotune_v4_r004.home import ReturnEvidence, evaluate_return
from step5d_autotune_v4_r004.identity import (
    ControllerReadbackReceipt,
    RuntimeIdentityEvidence,
    Script1StartReceipt,
    Script1UseLedger,
    SessionIdentityGate,
)
from step5d_autotune_v4_r004.ledger import (
    DurableCampaignLedger,
    LedgerError,
    verify_ledger_hash_chain,
)
import step5d_autotune_v4_r004.transport as r004_transport
from step5d_autotune_v4_r004.wire import (
    AttemptKind,
    CommandMode,
    DOUBLE_FIELDS,
    INPUT_INTEGER_REGISTERS,
    LAYOUT_TAG,
    OUTPUT_INTEGER_FIELDS,
    PacketPayload,
    SensorPacket,
    SessionCommand,
    SessionInput,
    validate_register_mappings,
    build_wire_packet,
)


ROOT = Path(__file__).resolve().parents[1]


def _payload(*, sequence: int = 7, integer_override: tuple[int, int] | None = None) -> PacketPayload:
    doubles = [0.0] * 24
    doubles[23] = LAYOUT_TAG
    doubles[22] = float(sequence)
    integers = [0] * 9
    if integer_override is not None:
        register, value = integer_override
        integers[register - 24] = value
    return PacketPayload(tuple(doubles), tuple(integers))


def _sensor() -> SensorPacket:
    return SensorPacket(
        normal_load_n=0.0,
        force_norm_n=0.0,
        heartbeat=1.0,
        sensor_fresh=True,
        stop_request=False,
        eoat_get_ack=True,
        torque_norm_nm=0.0,
        wrench=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        filtered_normal_n=0.0,
    )


def _session(
    *,
    command: SessionCommand = SessionCommand.HOLD,
    command_sequence: int = 0,
    ordinal: int = 0,
    kind: AttemptKind = AttemptKind.QUALIFICATION,
    token: int = 0,
) -> SessionInput:
    return SessionInput(
        baseline_consecutive_successes=0,
        command_mode=CommandMode.HOLD,
        sticky_one_newton_latched=0,
        session_command=command,
        session_command_sequence=command_sequence,
        session_epoch=1,
        logical_attempt_ordinal=ordinal,
        attempt_kind=kind,
        candidate_token=token,
    )


def test_f1_equal_sequence_reuse_is_accepted_before_80ms_then_reason43() -> None:
    guard = PacketFreshnessGuard()
    first = _payload(sequence=7)
    assert guard.observe(7, first, 0.000).accepted
    repeated = guard.observe(7, first, 0.002)
    assert repeated.accepted and repeated.reused_cached_payload
    assert repeated.reason_code == 0
    changed = guard.observe(7, _payload(sequence=7, integer_override=(27, 1)), 0.004)
    assert not changed.accepted and changed.stopped and changed.reason_code == 43

    held_guard = PacketFreshnessGuard()
    assert held_guard.observe(7, first, 0.000).accepted
    assert held_guard.observe(7, first, 0.079).accepted
    stale = held_guard.observe(7, first, 0.080)
    assert not stale.accepted and stale.reason_code == 43

    regression_guard = PacketFreshnessGuard()
    assert regression_guard.observe(8, _payload(sequence=8), 0.000).accepted
    regression = regression_guard.observe(7, _payload(sequence=7), 0.002)
    assert not regression.accepted and regression.reason_code == 43


def test_f2_fake_rtde_models_integer_registers_cadence_and_no_write_count_stage() -> None:
    contract = load_contract()
    fake = FakeRTDE(tp_hz=500.0, writer_hz=125.0)
    session = _session(
        command=SessionCommand.ARM,
        command_sequence=4,
        ordinal=3,
        kind=AttemptKind.BATCH_A,
        token=17,
    )
    packet = build_wire_packet(
        contract,
        Candidate(),
        sensor=_sensor(),
        proposed_qdot=(0.0,) * 6,
        internal_setpoint_n=1.0,
        packet_sequence=11,
        session=session,
    )
    fake.write_packet(packet)
    first = fake.tp_tick(timestamp_s=0.000)
    second = fake.tp_tick(timestamp_s=0.002)
    assert first.accepted and second.accepted
    assert second.reused_cached_payload
    assert fake.stage == "READY_HOME_NEXT"
    assert fake.write_count == 1
    assert fake.read_integer_register(27) == int(SessionCommand.ARM)
    assert fake.read_integer_register(30) == 3
    assert fake.read_integer_register(32) == 17
    fake.write_integer_register(27, int(SessionCommand.STOP))
    changed_payload = fake.tp_tick(timestamp_s=0.004)
    assert not changed_payload.accepted and changed_payload.reason_code == 43


def _baseline_observation(
    *, dt: float, latch: int, counter: int = 0, ready: bool = False
) -> BaselineObservation:
    return BaselineObservation(
        dt_s=dt,
        sticky_one_newton_latched=latch,
        baseline_consecutive_successes=counter,
        filtered_normal_n=5.0 if ready else 1.0,
        raw_normal_n=5.0 if ready else 1.0,
        force_norm_n=5.0 if ready else 1.0,
        torque_norm_nm=0.1,
        sensor_fresh=True,
        stationary=True,
        internal_setpoint_n=1.0,
    )


def test_f3_pre_latch_budget_does_not_consume_post_latch_budget() -> None:
    candidate = Candidate()
    state = BaselineState()
    for _ in range(250):
        state, _ = step_baseline(candidate, state, _baseline_observation(dt=0.0796, latch=0))
    assert state.phase is BaselinePhase.PRE_LATCH_WAIT
    state, _ = step_baseline(candidate, state, _baseline_observation(dt=0.002, latch=1))
    assert state.phase in {BaselinePhase.POST_LATCH_RAMP, BaselinePhase.ACQUIRE}
    assert state.pre_latch_elapsed_s == pytest.approx(19.9)
    assert state.post_latch_elapsed_s == pytest.approx(0.0)
    for _ in range(251):
        state, _ = step_baseline(candidate, state, _baseline_observation(dt=0.0796, latch=1))
    assert state.phase in {BaselinePhase.POST_LATCH_RAMP, BaselinePhase.ACQUIRE}
    state, command = step_baseline(candidate, state, _baseline_observation(dt=0.0796, latch=1))
    assert state.phase is BaselinePhase.FAILED
    assert "post_latch_force_acquisition_timeout" in command.reason

    regression_state, _ = step_baseline(
        candidate,
        BaselineState(phase=BaselinePhase.POST_LATCH_RAMP, post_latch_elapsed_s=1.0),
        _baseline_observation(dt=0.002, latch=0),
    )
    assert regression_state.phase is BaselinePhase.FAILED
    assert regression_state.stop_reason == "sticky_latch_regression"
    malformed_counter, _ = step_baseline(
        candidate,
        BaselineState(),
        _baseline_observation(dt=0.002, latch=0, counter=4),
    )
    assert malformed_counter.phase is BaselinePhase.FAILED
    regressed_counter, _ = step_baseline(
        candidate,
        BaselineState(last_baseline_consecutive_successes=2),
        _baseline_observation(dt=0.002, latch=0, counter=1),
    )
    assert regressed_counter.phase is BaselinePhase.FAILED
    assert regressed_counter.stop_reason == "malformed_baseline_progress_regression"


def _script1_receipt() -> Script1StartReceipt:
    contract = load_contract()
    return Script1StartReceipt(
        receipt_sha256="a" * 64,
        script_sha256=contract.script1_sha256["script"],
        observed_at_s=0.0,
        final_pose=(0.487834547, 0.129337053, 0.033, 3.120752062, 0.0, 0.068626833),
        final_q=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        stationary=True,
        safety_mode="NORMAL",
        eoat_identity_sha256=contract.eoat_sha256,
    )


def _controller_receipt() -> ControllerReadbackReceipt:
    contract = load_contract()
    triplet = {
        suffix: hashlib.sha256(
            (ROOT / f"programs/step5/step5d/step5d_strict_rnn_autotune_v4_r004{suffix}").read_bytes()
        ).hexdigest()
        for suffix in (".script", ".txt", ".urp")
    }
    return ControllerReadbackReceipt(
        receipt_sha256="b" * 64,
        program=contract.raw["program"],
        controller_target=contract.raw["script2"]["controller_target"],
        script_sha256=triplet[".script"],
        txt_sha256=triplet[".txt"],
        urp_sha256=triplet[".urp"],
        observed_at_s=0.0,
        runtime_protocol=606004,
        runtime_digest_hi=11,
        runtime_digest_lo=12,
        eoat_identity_sha256=contract.eoat_sha256,
    )


def test_f4_receipts_have_age_and_uninterrupted_session_invalidation() -> None:
    contract = load_contract()
    controller = _controller_receipt()
    script1 = _script1_receipt()
    triplet = {
        "script": controller.script_sha256,
        "txt": controller.txt_sha256,
        "urp": controller.urp_sha256,
    }
    controller.validate_at_play(contract, 300.0, expected_triplet=triplet)
    with pytest.raises(Exception):
        controller.validate_at_play(contract, 300.001, expected_triplet=triplet)
    script1.validate_for_epoch(
        contract,
        120.0,
        expected_script_sha256=contract.script1_sha256["script"],
        expected_eoat_sha256=contract.eoat_sha256,
    )
    with pytest.raises(Exception):
        script1.validate_for_epoch(
            contract,
            120.001,
            expected_script_sha256=contract.script1_sha256["script"],
            expected_eoat_sha256=contract.eoat_sha256,
        )
    gate = SessionIdentityGate(contract)
    gate.begin_play(
        controller_receipt=controller,
        script1_receipt=script1,
        now_s=0.0,
        epoch=1,
        session_id="resident-1",
        expected_triplet=triplet,
    )
    matching = RuntimeIdentityEvidence(
        program=controller.program,
        script_sha256=controller.script_sha256,
        runtime_protocol=controller.runtime_protocol,
        runtime_digest_hi=controller.runtime_digest_hi,
        runtime_digest_lo=controller.runtime_digest_lo,
        session_epoch=1,
        resident_session_id="resident-1",
        program_running=True,
        uninterrupted=True,
        observed_at_s=1.0,
    )
    assert gate.observe_runtime(matching)
    assert not gate.observe_runtime(matching)
    assert not gate.active
    assert gate.invalidated_reason == "runtime_identity_timestamp_not_newer"
    epoch_gate = SessionIdentityGate(contract)
    epoch_gate.begin_play(
        controller_receipt=controller,
        script1_receipt=script1,
        now_s=0.0,
        epoch=1,
        session_id="resident-epoch",
        expected_triplet=triplet,
    )
    assert not epoch_gate.observe_runtime(replace(matching, session_epoch=2, observed_at_s=2.0))
    assert epoch_gate.invalidated_reason == "runtime_identity_or_session_changed"
    with pytest.raises(Exception):
        gate.begin_play(
            controller_receipt=controller,
            script1_receipt=replace(script1, receipt_sha256="c" * 64),
            now_s=1.0,
            epoch=2,
            session_id="resident-2",
            expected_triplet=triplet,
        )
    uses = Script1UseLedger()
    uses.consume(script1, 1)
    with pytest.raises(Exception):
        uses.consume(script1, 2)


def test_f5_ledger_rows_are_durable_hash_verified_and_resume_is_new_execution(tmp_path: Path) -> None:
    def row(ordinal: int, *, status: str, completed: bool, gp: bool) -> dict[str, object]:
        return {
            "record_type": "attempt",
            "logical_attempt_ordinal": ordinal,
            "logical_attempt_id": f"logical-{ordinal}",
            "session_epoch": 1,
            "attempt_execution_id": f"exec-{ordinal}",
            "controller_receipt_sha256": "1" * 64,
            "script1_receipt_sha256": "2" * 64,
            "input_baseline_ledger_sha256": "3" * 64,
            "status": status,
            "completed": completed,
            "gp_eligible": gp,
            "output_seal": {"completion_sha256": "4" * 64},
            "durable_row": True,
            "durability": {"fsynced": True, "cold_read": True, "hash_verified": True},
        }

    completed_path = tmp_path / "completed-ledger.jsonl"
    completed_ledger = DurableCampaignLedger(completed_path)
    receipt = completed_ledger.append_attempt(row(1, status="completed", completed=True, gp=True))
    assert receipt.ready_for_next_arm
    assert completed_ledger.next_arm_allowed()
    verified = verify_ledger_hash_chain(completed_path)
    assert verified[0]["output_ledger_sha256"]
    for field in (
        "session_epoch",
        "attempt_execution_id",
        "controller_receipt_sha256",
        "script1_receipt_sha256",
        "input_baseline_ledger_sha256",
        "output_ledger_sha256",
    ):
        assert field in verified[0]

    interrupted_path = tmp_path / "interrupted-ledger.jsonl"
    interrupted_ledger = DurableCampaignLedger(interrupted_path)
    interrupted_ledger.append_attempt(row(1, status="interrupted", completed=False, gp=False))
    assert not interrupted_ledger.next_arm_allowed()
    with pytest.raises(LedgerError):
        interrupted_ledger.resume_after_interruption(
            new_epoch=2,
            script1_receipt_sha256="2" * 64,
        )
    resume = interrupted_ledger.resume_after_interruption(
        new_epoch=2,
        script1_receipt_sha256="5" * 64,
    )
    assert resume.logical_attempt_ordinal == 1
    assert resume.new_attempt_execution_id != "exec-1"
    assert resume.requires_script1 and resume.requires_new_epoch
    assert resume.last_completed_output_seal == "0" * 64

    tampered = json.loads(interrupted_path.read_text(encoding="utf-8").splitlines()[0])
    tampered["output_ledger_sha256"] = "f" * 64
    interrupted_path.write_text(json.dumps(tampered) + "\n", encoding="utf-8")
    with pytest.raises(LedgerError):
        verify_ledger_hash_chain(interrupted_path)


def test_r004_transport_adapter_uses_all_24_doubles_and_9_int_recipe_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class StubClient:
        def __init__(self, host: str, *, port: int, timeout: float) -> None:
            captured["init"] = (host, port, timeout)
            self.sock = object()

        def __enter__(self) -> "StubClient":
            return self

        def __exit__(self, *_args: object) -> None:
            captured["closed"] = True
            self.sock = None

        def negotiate(self) -> None:
            captured["negotiated"] = True

        def setup_outputs(self, _rate: float, fields: tuple[str, ...]) -> tuple[int, list[str]]:
            captured["output_fields"] = fields
            return 7, [
                "DOUBLE",
                "DOUBLE",
                "VECTOR3D",
                "VECTOR6D",
                "VECTOR6D",
                "VECTOR6D",
                "VECTOR6D",
                "VECTOR6D",
                "UINT32",
                "UINT32",
                "UINT32",
                *(["INT32"] * 11),
            ]

        def setup_inputs(self, fields: tuple[str, ...]) -> tuple[int, list[str]]:
            captured["input_fields"] = fields
            return 8, ["DOUBLE"] * 24 + ["UINT32", "INT32", "UINT32", "INT32", "UINT32", "INT32", "UINT32", "INT32", "UINT32"]

        def start(self) -> None:
            captured["started"] = True

        def send_input_sample(self, _recipe: int, _types: list[str], values: list[object]) -> None:
            captured["values"] = values

        def recv_latest_sample(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        r004_transport,
        "_canonical_v4",
        lambda: SimpleNamespace(WritableRTDEClient=StubClient),
    )
    transport = r004_transport.LiveR004RTDETransport("offline-test", port=30004)
    transport.open()
    transport.send_packet((0.0,) * 24, tuple(range(9)))
    values = captured["values"]
    assert isinstance(values, list) and len(values) == 33
    assert values[:24] == [0.0] * 24
    assert values[24:] == list(range(9))
    assert len(captured["input_fields"]) == 33
    assert captured["input_fields"][24:] == tuple(
        f"input_int_register_{register}" for register in range(24, 33)
    )
    assert captured["output_fields"][-11:] == tuple(
        f"output_int_register_{register}" for register in range(24, 35)
    )
    assert not any("integer_register" in field for field in captured["input_fields"])
    assert not any("integer_register" in field for field in captured["output_fields"])
    transport.close()
    assert captured["closed"] is True


def test_r004_contract_and_deploy_manifest_share_canonical_controller_target() -> None:
    contract = load_contract()
    manifest = json.loads(
        (
            ROOT
            / "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r004.deploy-manifest.json"
        ).read_text(encoding="utf-8")
    )
    selector = json.loads(
        (ROOT / "config/step5d/lineage_selector_v4_r004.json").read_text(encoding="utf-8")
    )
    stage_table = json.loads(
        (ROOT / "config/step5_stage_table.json").read_text(encoding="utf-8")
    )
    stage = next(item for item in stage_table["stages"] if item["id"] == "step5d_strict_rnn_autotune_v4")
    expected = "/programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v4_r004.urp"
    assert contract.raw["script2"]["controller_target"] == expected
    assert manifest["controller_target"] == expected
    assert selector["candidate_v4_r004"]["controller_target"] == expected
    assert stage["current_binding"]["controller_target"] == expected
    assert selector["candidate_v4_r004"]["release_contract_sha256"] == contract.sha256
    assert selector["candidate_v4_r004"]["offline_closure_sha256"] == hashlib.sha256(
        (ROOT / "config/step5d/autotune_v4_r004_offline_closure.json").read_bytes()
    ).hexdigest()


def test_campaign_order_acceptance_and_anchor_preserving_promotion(tmp_path: Path) -> None:
    contract = load_contract()
    plan = build_campaign_plan(contract)
    assert len(plan) == 16
    assert tuple(item.phase for item in plan) == (
        ("QUALIFICATION",) * 3 + ("BATCH_A",) * 5 + ("BATCH_B",) * 5 + ("RETEST",) * 3
    )
    assert all(item.candidate.target_force_n == 5.0 for item in plan)

    ledger = DurableCampaignLedger(tmp_path / "campaign-ledger.jsonl")
    rows = CampaignRunner(contract, ledger).run()
    assert len(rows) == 16
    assert all(
        field in rows[0]
        for field in (
            "session_epoch",
            "attempt_execution_id",
            "controller_receipt_sha256",
            "script1_receipt_sha256",
            "input_baseline_ledger_sha256",
            "output_ledger_sha256",
        )
    )

    anchor_uid = plan[0].candidate.uid
    candidate_uid = plan[4].candidate.uid
    batch_rows = [
        {
            "phase": "BATCH_A",
            "candidate_uid": anchor_uid,
            "objective": 1.0,
            "gp_eligible": True,
        },
        {
            "phase": "BATCH_B",
            "candidate_uid": anchor_uid,
            "objective": 1.0,
            "gp_eligible": True,
        },
        {
            "phase": "BATCH_A",
            "candidate_uid": candidate_uid,
            "objective": 0.9,
            "gp_eligible": True,
        },
    ]
    retest_rows = [
        {
            "phase": "RETEST",
            "candidate_uid": candidate_uid,
            "objective": 0.9,
            "mae_n": 0.2,
            "completed": True,
            "complete_bins": 550,
            "effective_rate_hz": 100.0,
            "p99_packet_interval_s": 0.01,
            "max_packet_interval_s": 0.02,
            "safety_gate_passed": True,
            "contact_gate_passed": True,
            "return_gate_passed": True,
            "gp_eligible": True,
        }
        for _ in range(3)
    ]
    decision = promotion_decision(batch_rows + retest_rows, anchor_uid=anchor_uid)
    assert decision["promotion_allowed"]
    assert decision["selected_candidate_uid"] == candidate_uid
    assert decision["anchor_mutated"] is False

    no_promotion = promotion_decision(
        batch_rows
        + [{**row, "objective": 0.99} for row in retest_rows],
        anchor_uid=anchor_uid,
    )
    assert not no_promotion["promotion_allowed"]
    assert no_promotion["anchor_mutated"] is False


def test_return_home_fault_never_requests_auto_home() -> None:
    passing = evaluate_return(
        ReturnEvidence(
            stationary=True,
            retract_z_m=0.005,
            transfer_floor_z_m=0.062863519,
            entry_linear_speed_m_s=0.01,
            return_linear_speed_m_s=0.01,
            entry_angular_speed_rad_s=0.01,
            return_angular_speed_rad_s=0.01,
            descended_to_captured_home=True,
            home_pose_error_m=0.001,
            home_orientation_error_rad=0.01,
            home_q_error_rad=0.005,
        )
    )
    assert passing.passed and passing.auto_home is False
    failing = evaluate_return(
        ReturnEvidence(
            stationary=False,
            retract_z_m=0.001,
            transfer_floor_z_m=0.05,
            entry_linear_speed_m_s=0.01,
            return_linear_speed_m_s=0.01,
            entry_angular_speed_rad_s=0.01,
            return_angular_speed_rad_s=0.01,
            descended_to_captured_home=False,
            home_pose_error_m=0.01,
            home_orientation_error_rad=0.1,
            home_q_error_rad=0.1,
            fault_reason="fault",
        )
    )
    assert not failing.passed and failing.stop_required and failing.auto_home is False


def test_wire_mappings_and_local_r004_triplet_are_explicit() -> None:
    validate_register_mappings()
    assert set(DOUBLE_FIELDS) == set(range(24, 48))
    assert set(INPUT_INTEGER_REGISTERS) == set(range(24, 33))
    assert set(OUTPUT_INTEGER_FIELDS) == set(range(24, 35))
    assert len(set(DOUBLE_FIELDS.values())) == 24
    assert len(set(OUTPUT_INTEGER_FIELDS.values())) == 11
    from build_step5d_autotune_v4_r004 import numeric_sanity, validate_triplet

    script_path = ROOT / "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r004.script"
    txt_path = ROOT / "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r004.txt"
    urp_path = ROOT / "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r004.urp"
    script = script_path.read_text(encoding="utf-8")
    stamp = script.splitlines()[0].removeprefix("# VERSION: ")
    checks = validate_triplet(script, txt_path.read_text(encoding="utf-8"), urp_path.read_bytes(), stamp)
    assert all(checks.values())
    assert numeric_sanity(script)["passed"]
