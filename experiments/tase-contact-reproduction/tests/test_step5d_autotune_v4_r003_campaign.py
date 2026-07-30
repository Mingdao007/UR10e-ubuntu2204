from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import run_step5d_autotune_v4_campaign as campaign  # noqa: E402
from step5d_autotune_v4 import contracts  # noqa: E402
from step5d_autotune_v4.baseline_ledger import (  # noqa: E402
    BaselineQualificationLedger,
    BaselineSuccessReceipt,
)
from step5d_autotune_v4.adapter import AdapterTick, V4RuntimeAdapter  # noqa: E402
from step5d_autotune_v4.control import RuntimeDecision, RuntimeObservation  # noqa: E402
from step5d_autotune_v4.live_attempt import (  # noqa: E402
    AttemptKind,
    V4LiveAttemptSpec,
)
from step5d_autotune_v4.path_controller import V4PathController  # noqa: E402
from step5d_autotune_v4.wire import CommandMode, SensorPacket  # noqa: E402


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_r003_plan_is_exactly_the_bounded_16_attempt_order() -> None:
    plan = campaign.build_campaign_plan()

    assert len(plan) == 16
    assert [item.phase for item in plan] == [
        "QUAL",
        "QUAL",
        "QUAL",
        "BATCH_A",
        "BATCH_A",
        "BATCH_A",
        "BATCH_A",
        "BATCH_A",
        "BATCH_B",
        "BATCH_B",
        "BATCH_B",
        "BATCH_B",
        "BATCH_B",
        "RETEST",
        "RETEST",
        "RETEST",
    ]
    assert [item.label for item in plan] == [
        "anchor",
        "anchor",
        "anchor",
        "anchor",
        "P-",
        "anchor",
        "P+",
        "anchor",
        "anchor",
        "D-",
        "anchor",
        "D+",
        "anchor",
        "incumbent",
        "incumbent",
        "incumbent",
    ]
    assert all(item.candidate.target_force_n == 5.0 for item in plan)
    for previous, current in zip(plan, plan[1:]):
        changed = campaign.changed_physical_coordinates(
            previous.candidate, current.candidate
        )
        if changed:
            assert len(changed) == 1
            contracts.validate_live_transition(previous.candidate, current.candidate)
            assert abs(
                math.log2(
                    getattr(current.candidate, changed[0])
                    / getattr(previous.candidate, changed[0])
                )
            ) <= 0.25 + 1e-12


def test_default_run_is_network_free_and_chain_is_append_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger_path = tmp_path / "campaign.jsonl"

    def fail_import(_name: str):
        raise AssertionError("dry-run must not bind the live writer")

    monkeypatch.setattr(campaign.importlib, "import_module", fail_import)
    first = campaign.run_campaign(ledger_path)

    assert first.completed
    assert not first.frozen
    assert first.attempts_run == 16
    rows = campaign.verify_sha256_chain(
        ledger_path, campaign_fingerprint=contracts.load_contract().campaign_fingerprint
    )
    assert len(rows) == 16
    before = ledger_path.read_bytes()
    second = campaign.run_campaign(ledger_path)
    assert second.completed
    assert ledger_path.read_bytes() == before


def test_qualification_failure_clears_streak_without_freezing_campaign(tmp_path: Path) -> None:
    def provider(attempt: campaign.CampaignAttempt, _state: campaign.CampaignSnapshot):
        if attempt.ordinal == 1:
            return campaign.AttemptOutcome(
                completed=False,
                terminal_stage=21,
                qualification_passed=False,
                reason="baseline_not_qualified",
            )
        if attempt.phase == "QUAL":
            return campaign.AttemptOutcome(
                completed=True,
                terminal_stage=22,
                qualification_passed=True,
                reason="qualified",
            )
        return campaign.AttemptOutcome(completed=True, terminal_stage=80, reason="ok")

    result = campaign.run_campaign(
        tmp_path / "qualification-failure.jsonl", outcome_provider=provider
    )

    assert result.attempts_run == 3
    assert not result.frozen
    assert not result.completed
    assert result.stopped
    assert result.stop_reason == "three_consecutive_qualifications_required_before_trials"
    assert [
        row["qualification_streak_after"] for row in result.rows[:3]
    ] == [0, 1, 2]


@pytest.mark.parametrize("failure_field", ["safety_failure", "structural_failure"])
def test_safety_or_structural_failure_freezes_and_exits(
    tmp_path: Path, failure_field: str
) -> None:
    def provider(attempt: campaign.CampaignAttempt, _state: campaign.CampaignSnapshot):
        if attempt.ordinal == 5:
            return campaign.AttemptOutcome(
                completed=False,
                terminal_stage=21,
                reason=failure_field,
                **{failure_field: True},
            )
        return campaign.AttemptOutcome(
            completed=True,
            terminal_stage=(22 if attempt.phase == "QUAL" else 80),
            qualification_passed=(True if attempt.phase == "QUAL" else None),
            reason="ok",
        )

    result = campaign.run_campaign(tmp_path / f"{failure_field}.jsonl", outcome_provider=provider)

    assert result.frozen
    assert result.stopped
    assert result.attempts_run == 5
    assert result.rows[-1]["frozen"] is True
    assert result.rows[-1]["stop"] is True
    assert result.rows[-1][failure_field] is True
    assert not any(row["ordinal"] == 6 for row in result.rows)
    campaign.verify_sha256_chain(
        result.ledger_path,
        campaign_fingerprint=contracts.load_contract().campaign_fingerprint,
    )


def test_live_binding_fails_closed_before_motion_and_binds_lazily(tmp_path: Path) -> None:
    contract = contracts.load_contract()
    seal = BaselineQualificationLedger(contract).seal()
    triplet = {
        suffix: hashlib.sha256(
            (
                ROOT
                / "programs/step5/step5d"
                / f"step5d_strict_rnn_autotune_v4_r003.{suffix}"
            ).read_bytes()
        ).hexdigest()
        for suffix in ("urp", "script", "txt")
    }
    receipt_path = tmp_path / "controller-readback-receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "basename": "step5d_strict_rnn_autotune_v4_r003",
                "controller_target": (
                    "/programs/andyl/kunwei/step5/"
                    "step5d_strict_rnn_autotune_v4_r003.urp"
                ),
                "state": "controller read-back verified",
                "artifacts": {
                    "urp_sha256": triplet["urp"],
                    "script_sha256": triplet["script"],
                    "txt_sha256": triplet["txt"],
                },
                "gates": {
                    "local_triplet_sha_match": True,
                    "controller_triplet_sha_match": True,
                    "fresh_get_triplet_sha_match": True,
                    "cached_contents_exact": True,
                },
            }
        ),
        encoding="utf-8",
    )
    binding = campaign.RemoteBinding(
        contract,
        writer_contract_path=ROOT / "config/step5d/autotune_v4_live_writer_r003.json",
        triplet_sha256=triplet,
        baseline_ledger_seal=seal,
        controller_readback_receipt_path=receipt_path,
        module_name="test_lazy_writer_module",
    )
    with pytest.raises(campaign.RemoteBindingError, match="regular file"):
        campaign.RemoteBinding(
            contract,
            writer_contract_path=tmp_path / "missing-writer.json",
            triplet_sha256={"urp": "a" * 64, "script": "b" * 64, "txt": "c" * 64},
            baseline_ledger_sha256="d" * 64,
        ).preflight()
    assert not binding.module_bound

    observed: list[str] = []

    class FakeModule:
        pass

    def fake_import(name: str):
        assert name == "test_lazy_writer_module"
        observed.append(name)
        return FakeModule()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(campaign.importlib, "import_module", fake_import)
    try:
        binding.preflight()
        assert not binding.module_bound
        spec = binding._build_spec(campaign.build_campaign_plan(contract)[0])
        assert spec.kind is AttemptKind.QUALIFICATION
        binding._bind_module()
        assert binding.module_bound
        assert observed == ["test_lazy_writer_module"]
    finally:
        monkeypatch.undo()


def test_live_writer_parser_accepts_current_typed_attempt_spec(tmp_path: Path) -> None:
    contract = contracts.load_contract()
    spec = V4LiveAttemptSpec(
        attempt_id="parser-check",
        kind=AttemptKind.QUALIFICATION,
        candidate=contracts.V4Candidate(),
        contract_sha256=contract.sha256,
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256=contract.eoat_sha256,
        model_hashes=contract.model_hashes,
        triplet_sha256={"urp": "a" * 64, "script": "b" * 64, "txt": "c" * 64},
        input_baseline_ledger_sha256="d" * 64,
    )
    path = tmp_path / "attempt.json"
    path.write_text(json.dumps(spec.to_dict()), encoding="utf-8")

    import step5d_autotune_v4_live_writer as writer  # noqa: PLC0415

    parsed = writer.load_attempt_spec(path)
    assert parsed.attempt_id == spec.attempt_id
    assert parsed.contract_sha256 == contract.sha256
    assert parsed.v4_contract_path == contract.path


def test_live_writer_parser_rejects_string_path_requested(tmp_path: Path) -> None:
    contract = contracts.load_contract()
    document = V4LiveAttemptSpec(
        attempt_id="parser-bool-check",
        kind=AttemptKind.PD_TRIAL,
        candidate=contracts.V4Candidate(),
        contract_sha256=contract.sha256,
        campaign_fingerprint=contract.campaign_fingerprint,
        eoat_sha256=contract.eoat_sha256,
        model_hashes=contract.model_hashes,
        triplet_sha256={"urp": "a" * 64, "script": "b" * 64, "txt": "c" * 64},
        input_baseline_ledger_sha256="d" * 64,
        path_requested=True,
    ).to_dict()
    document["path_requested"] = "false"
    path = tmp_path / "attempt-string-bool.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    import step5d_autotune_v4_live_writer as writer  # noqa: PLC0415

    with pytest.raises(contracts.V4ContractError, match="path_requested must be bool"):
        writer.load_attempt_spec(path)


def test_dashboard_preflight_rejects_wrong_loaded_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import step5d_autotune_v4_live_writer as writer  # noqa: PLC0415

    writer_contract = writer.load_contract(
        ROOT / "config/step5d/autotune_v4_live_writer_r003.json"
    )
    monkeypatch.setattr(
        writer,
        "dashboard_exchange",
        lambda *_args, **_kwargs: {
            "is in remote control": "true",
            "safetymode": "Safetymode: NORMAL",
            "robotmode": "Robotmode: RUNNING",
            "running": "false",
            "programState": "STOPPED",
            "get loaded program": "Loaded program: /programs/wrong.urp",
        },
    )

    with pytest.raises(writer.LiveWriterError, match="loaded program identity"):
        writer._dashboard_preflight(writer_contract)


def _qualified_ledger(contract, count: int = 3) -> BaselineQualificationLedger:
    ledger = BaselineQualificationLedger(contract)
    for index in range(count):
        ledger.record_success(
            BaselineSuccessReceipt(
                attempt_id=f"qualification-{index + 1}",
                terminal_stage=22,
                campaign_fingerprint=contract.campaign_fingerprint,
                eoat_sha256=contract.eoat_sha256,
                target_force_n=5.0,
                sensor_authority="kunwei_only",
                completion_sha256=_sha(f"qualification-{index + 1}"),
            )
        )
    return ledger


def _adapter_tick(contract) -> AdapterTick:
    return AdapterTick(
        observation=RuntimeObservation(
            monotonic_s=1.0,
            heartbeat=3.0,
            one_newton_latched=True,
            filtered_normal_n=5.0,
            raw_normal_n=5.0,
            force_norm_n=5.0,
            torque_norm_nm=0.1,
            sensor_fresh=True,
            stationary=True,
        ),
        sensor=SensorPacket(
            normal_load_n=5.0,
            force_norm_n=5.0,
            heartbeat=3.0,
            sensor_fresh=True,
            stop_request=False,
            eoat_get_ack=True,
            torque_norm_nm=0.1,
            wrench=(0.0, 0.0, -5.0, 0.0, 0.0, 0.1),
            filtered_normal_load_n=5.0,
        ),
        proposed_qdot=(0.0,) * 6,
        jacobian_6x6=tuple(
            tuple(1.0 if row == column else 0.0 for column in range(6))
            for row in range(6)
        ),
        normal_base=(0.0, 0.0, 1.0),
        observed_model_hashes=contract.model_hashes,
        command_sequence=3,
    )


@pytest.mark.parametrize(
    ("path_requested", "expected_mode"),
    ((False, CommandMode.RETRACT), (True, CommandMode.PATH)),
)
def test_full_qualification_requires_explicit_path_request(
    monkeypatch: pytest.MonkeyPatch,
    path_requested: bool,
    expected_mode: CommandMode,
) -> None:
    contract = contracts.load_contract()
    adapter = V4RuntimeAdapter(
        contract,
        contracts.V4Candidate(),
        baseline_ledger=_qualified_ledger(contract),
        attempt_id="mode-check",
        path_requested=path_requested,
    )
    monkeypatch.setattr(
        adapter.control,
        "step",
        lambda *_args, **_kwargs: RuntimeDecision(
            phase="success",
            qdot=(0.0,) * 6,
            internal_setpoint_n=5.0,
            candidate_target_force_n=5.0,
            stop=False,
            freeze_bo=False,
            retract_allowed=True,
            full_path_allowed=True,
            reason="",
        ),
    )

    result = adapter.tick(_adapter_tick(contract))

    assert not result.packet.stop_dominant
    assert result.packet.integers_by_register[25] == int(expected_mode)


def test_force_error_keeps_integrated_search_negative_z_sign() -> None:
    log = V4PathController(contracts.V4Candidate()).step(
        actual_dt_s=0.008,
        raw_normal_n=1.0,
        setpoint_n=5.0,
        mode="baseline",
    )

    assert log.proposed_qdot[2] < 0.0


def test_baseline_calibrated_outer_loop_is_strictly_normal_only() -> None:
    from step5d_autotune_v4.calibrated_runtime import V4CalibratedRuntime  # noqa: PLC0415

    runtime = V4CalibratedRuntime(contracts.load_contract(), contracts.V4Candidate())
    twist = runtime.desired_twist(
        actual_tcp_pose=(
            0.487834547,
            0.129337053,
            0.008044839,
            3.120752062,
            0.0,
            0.068626833,
        ),
        actual_tcp_speed=(0.0,) * 6,
        force_tcp_n=(0.0, 0.0, -0.5),
        filtered_normal_n=0.5,
        internal_setpoint_n=1.0,
        actual_dt_s=0.008,
        mode="baseline",
        path_time_s=0.0,
    )

    assert twist[:2] == (0.0, 0.0)
    assert twist[3:] == (0.0, 0.0, 0.0)
    assert twist[2] < 0.0


def test_calibrated_tcp_jacobian_is_not_identity() -> None:
    from step5c_calibrated_kinematics_audit import build_calibrated_model  # noqa: PLC0415
    from step5d_autotune_v4.calibrated_runtime import tcp_jacobian_base  # noqa: PLC0415

    jacobian = tcp_jacobian_base(
        build_calibrated_model(),
        (0.0, -1.2, 1.8, -2.1, -1.57, 0.0),
    )

    assert jacobian.shape == (6, 6)
    assert all(math.isfinite(float(value)) for value in jacobian.flat)
    assert any(
        abs(float(jacobian[row, column]) - (1.0 if row == column else 0.0))
        > 1e-12
        for row in range(6)
        for column in range(6)
    )


def test_damping_changes_the_mature_outer_loop_command() -> None:
    from dataclasses import replace as dc_replace  # noqa: PLC0415

    from step5d_autotune_v4.calibrated_runtime import V4CalibratedRuntime  # noqa: PLC0415

    contract = contracts.load_contract()
    anchor = contracts.V4Candidate()
    low = V4CalibratedRuntime(
        contract, dc_replace(anchor, force_damping=23.5450996)
    )
    high = V4CalibratedRuntime(
        contract, dc_replace(anchor, force_damping=33.2977992)
    )
    kwargs = {
        "actual_tcp_pose": (
            0.487834547,
            0.129337053,
            0.008044839,
            3.120752062,
            0.0,
            0.068626833,
        ),
        "actual_tcp_speed": (0.0, 0.0, -0.0001, 0.0, 0.0, 0.0),
        "force_tcp_n": (0.0, 0.0, -4.0),
        "filtered_normal_n": 4.0,
        "internal_setpoint_n": 5.0,
        "actual_dt_s": 0.008,
        "mode": "baseline",
        "path_time_s": 0.0,
    }

    low.desired_twist(**kwargs)
    high.desired_twist(**kwargs)
    low_twist = low.desired_twist(**kwargs)
    high_twist = high.desired_twist(**kwargs)

    assert low_twist != high_twist


def test_retests_use_the_best_eligible_tested_candidate(tmp_path: Path) -> None:
    def provider(
        attempt: campaign.CampaignAttempt,
        _state: campaign.CampaignSnapshot,
    ) -> campaign.AttemptOutcome:
        if attempt.phase == "QUAL":
            return campaign.AttemptOutcome(
                completed=True,
                terminal_stage=22,
                qualification_passed=True,
                completion_sha256=_sha(attempt.attempt_id),
            )
        objective = (
            0.18
            if attempt.phase == "RETEST"
            else 0.20
            if attempt.label == "P-"
            else 0.40
            if attempt.label == "anchor"
            else 0.60
        )
        return campaign.AttemptOutcome(
            completed=True,
            terminal_stage=80,
            completion_sha256=_sha(attempt.attempt_id),
            metrics={
                "objective": objective,
                "mae_n": objective,
                "complete_bins": 550,
                "p99_normal_n": 5.5,
                "max_force_norm_n": 6.0,
                "max_torque_norm_nm": 0.1,
                "timing_acceptance": {"passed": True},
                "replay_eligible_shape": True,
            },
        )

    result = campaign.run_campaign(
        tmp_path / "selected-incumbent.jsonl",
        outcome_provider=provider,
    )
    p_minus_uid = campaign.build_campaign_plan()[4].candidate.candidate_uid

    assert result.completed
    assert result.selected_incumbent_uid == p_minus_uid
    assert result.promotion_allowed
    assert all(
        row["candidate_uid"] == p_minus_uid
        for row in result.rows
        if row["phase"] == "RETEST"
    )


def test_terminal_reason_is_bound_to_attempt_kind() -> None:
    pd_attempt = campaign.build_campaign_plan()[3]
    qual_attempt = campaign.build_campaign_plan()[0]
    pd_wrong = campaign.RemoteBinding._outcome_from_summary(
        pd_attempt,
        {
            "status": "tp_terminal_observed",
            "terminal": {"stage": 80, "reason": 31},
            "metrics": {},
        },
    )
    qual_wrong = campaign.RemoteBinding._outcome_from_summary(
        qual_attempt,
        {
            "status": "tp_terminal_observed",
            "terminal": {"stage": 80, "reason": 32},
            "metrics": {},
        },
    )

    assert not pd_wrong.completed and pd_wrong.structural_failure
    assert not qual_wrong.completed and qual_wrong.structural_failure
    assert pd_wrong.reason == "terminal_reason_kind_mismatch"


def test_failed_timing_and_replay_shape_never_enter_gp(tmp_path: Path) -> None:
    def provider(
        attempt: campaign.CampaignAttempt, _state: campaign.CampaignSnapshot
    ) -> campaign.AttemptOutcome:
        if attempt.phase == "QUAL":
            return campaign.AttemptOutcome(
                completed=True,
                terminal_stage=22,
                qualification_passed=True,
                completion_sha256=_sha(attempt.attempt_id),
            )
        return campaign.AttemptOutcome(
            completed=True,
            terminal_stage=80,
            completion_sha256=_sha(attempt.attempt_id),
            metrics={
                "objective": 0.2,
                "mae_n": 0.2,
                "complete_bins": 550,
                "p99_normal_n": 5.5,
                "max_force_norm_n": 6.0,
                "max_torque_norm_nm": 0.1,
                "timing_acceptance": {"passed": False, "rate_hz": 50.0},
                "replay_eligible_shape": False,
            },
        )

    result = campaign.run_campaign(tmp_path / "timing-failure.jsonl", outcome_provider=provider)

    assert not any(row["gp_eligible"] for row in result.rows[3:])


def test_resume_preserves_historical_incomplete_outcome(tmp_path: Path) -> None:
    ledger = tmp_path / "resume-incomplete.jsonl"

    def interrupted(
        attempt: campaign.CampaignAttempt, _state: campaign.CampaignSnapshot
    ) -> campaign.AttemptOutcome:
        if attempt.ordinal == 1:
            return campaign.AttemptOutcome(
                completed=False,
                terminal_stage=21,
                qualification_passed=False,
                reason="qualification_miss",
            )
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        campaign.run_campaign(ledger, outcome_provider=interrupted)
    resumed = campaign.run_campaign(ledger)

    assert not resumed.completed
    assert resumed.rows[0]["completed"] is False
