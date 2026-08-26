from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(REPO / "src" / "ur10e_experiment_runtime"))

from run_step5d_autotune_v4_r013_live import (  # noqa: E402
    _handle_strategy_canary_terminal,
    _startup_resume_terminal,
)
from step5d_autotune_v4_r013.campaign import (  # noqa: E402
    Campaign,
    PhysicalAdmissionReceipt,
    R013CampaignError,
    STRATEGY_CANARY_KIND,
    candidate_token,
    strategy_canary_plan,
)
from step5d_autotune_v4_r013.domain import physical_candidate_key  # noqa: E402
from step5d_autotune_v4_r013.live_runtime import (  # noqa: E402
    _make_runtime_class,
    anti_windup_metrics_from_rows,
    mark_r013_safe_return_transition,
    runtime_strategy_receipt_from_rows,
)
from step5d_autotune_v4_r013.live_owner import (  # noqa: E402
    R013OwnerError,
    _validate_strategy_physical_bindings,
)
import step5d_autotune_v4_r013.live_runtime as live_runtime_module  # noqa: E402
from step5d_autotune_v4_r013.runtime_strategy import (  # noqa: E402
    DISABLED_RUNTIME_STRATEGY,
    R013RuntimeStrategyError,
    RuntimeStrategyReceiptLedger,
    runtime_strategy_sha256,
    target_correction_n,
    validate_runtime_strategy,
)
from step5d_paper_outer_loop import (  # noqa: E402
    CONDITIONAL_DOUBLE_CLAMP_POLICY,
    LEGACY_FORCE_INTEGRAL_POLICY,
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
)


PROFILE = ROOT / "config/step5d/r013_goal035_path_phase_target_profile_v1.json"
SEED = {
    "force_p_gain": 0.006727171322157698,
    "force_damping": 56.0,
    "force_i_gain": 0.0,
    "i_off": True,
    "normal_filter_tau_s": 0.05202781128136904,
    "orientation_ko": 0.05946035575013606,
    "motion_kp": 1.5,
    "target_force_n": 5.0,
}


def _strategy() -> dict[str, object]:
    return validate_runtime_strategy(json.loads(PROFILE.read_text(encoding="utf-8")))


def _metrics() -> dict[str, object]:
    return {
        "schema": "step5d.autotune-v4/r013-trial-anti-windup-v1",
        "policy": "conditional-double-clamp-v1",
        "max_abs_integral_n_s": 0.0,
        "max_abs_i_term": 0.0,
        "saturation_duty": 0.0,
        "freeze_duty": 0.0,
        "invariant_violation_count": 0,
        "reset_reasons": ["candidate_dispatch", "path_entry", "mode_exit_or_home"],
        "path_gain_hot_switch": False,
    }


def _admission(dispatch, *, admitted: bool, objective: float = 0.5):
    return PhysicalAdmissionReceipt(
        dispatch_id=dispatch.dispatch_id,
        candidate_token=candidate_token(dispatch.candidate),
        candidate_key=physical_candidate_key(dispatch.candidate),
        attempt_sequence=dispatch.ordinal,
        execution_id=f"execution-{dispatch.ordinal}",
        sealed_mae_n=objective,
        physical_eligible=admitted,
        timing_gate=admitted,
        motion_gate=admitted,
        qualification_passed=admitted,
        observation_uid=f"observation-{dispatch.ordinal}",
    )


def _receipt(strategy: dict[str, object]) -> dict[str, object]:
    return {
        "schema": "step5d.autotune-v4/r013-runtime-strategy-receipt-v1",
        "runtime_strategy_sha256": runtime_strategy_sha256(strategy),
        "enabled": True,
        "path_clock": "runtime_desired_twist_path_time_s",
        "path_sample_count": 30001,
        "formal_sample_count": 27500,
        "minimum_effective_target_n": 3.9632449269199883,
        "maximum_applied_correction_n": 1.0367550730800117,
        "first_path_time_s": 0.0,
        "last_path_time_s": 60.0,
        "maximum_path_clock_gap_s": 0.002,
        "violation_count": 0,
        "exit_restored_to_unmodified_target": True,
        "safe_return_transition_observed": True,
        "exit_mode": "safe_return",
    }


def _campaign(tmp_path: Path) -> Campaign:
    strategy = _strategy()
    campaign = Campaign.create(
        tmp_path / "strategy.jsonl",
        campaign_id="r013-strategy",
        run_id="fresh",
        attempt_id="attempt",
        noise_floor_n2=0.01,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
        runtime_strategy=strategy,
    )
    campaign.install_strategy_canary_plan(
        strategy_canary_plan(
            runtime_strategy_sha256_value=campaign.runtime_strategy_sha256
        )
    )
    return campaign


def test_strategy_profile_is_strict_bounded_and_truthfully_labeled() -> None:
    strategy = _strategy()
    assert strategy["source"]["direction_selection_used_holdout"] is True
    assert strategy["source"]["holdout_role"].endswith("not_pristine_confirmatory")
    assert target_correction_n(strategy, path_time_s=4.999, mode="path") == 0.0
    assert target_correction_n(strategy, path_time_s=5.0, mode="path") == 0.0
    assert 0.0 < target_correction_n(strategy, path_time_s=59.999, mode="path") < 0.001
    assert target_correction_n(strategy, path_time_s=60.0, mode="path") == 0.0
    assert target_correction_n(strategy, path_time_s=32.5, mode="hold") == 0.0

    for mutation in (
        {**strategy, "version": True},
        {**strategy, "unexpected": 1},
        {**strategy, "maximum_correction_n": 1.5},
    ):
        with pytest.raises(R013RuntimeStrategyError):
            validate_runtime_strategy(mutation)
    with pytest.raises(R013RuntimeStrategyError, match="exact JSON number"):
        target_correction_n(strategy, path_time_s=True, mode="path")
    with pytest.raises(R013RuntimeStrategyError, match="exact JSON number"):
        target_correction_n(strategy, path_time_s="32.5", mode="path")


def test_runtime_receipt_requires_continuous_authoritative_path_clock() -> None:
    strategy = _strategy()
    identity = runtime_strategy_sha256(strategy)
    rows = []
    for index in range(1201):
        path_time = index * 0.05
        correction = target_correction_n(strategy, path_time_s=path_time, mode="path")
        rows.append(
            {
                "runtime_strategy_path_time_s": path_time,
                "phase_target_correction_n": correction,
                "effective_force_target_n": 5.0 - correction,
                "unmodified_force_target_n": 5.0,
                "runtime_strategy_sha256": identity,
                "runtime_strategy_enabled": True,
            }
        )
    runtime = SimpleNamespace(
        _r013_last_strategy_correction_n=0.0,
        _r013_last_effective_target_n=5.0,
        _r013_last_unmodified_target_n=5.0,
        _r013_strategy_safe_return_observed=True,
        _r013_strategy_exit_mode="safe_return",
    )
    receipt = runtime_strategy_receipt_from_rows(
        rows,
        strategy=strategy,
        runtime=runtime,
    )
    assert receipt["first_path_time_s"] == 0.0
    assert receipt["last_path_time_s"] == 60.0
    assert receipt["maximum_path_clock_gap_s"] <= 0.051
    assert receipt["exit_restored_to_unmodified_target"] is True

    discontinuous = [row for row in rows if row["runtime_strategy_path_time_s"] != 10.0]
    with pytest.raises(ValueError, match="PATH/exit"):
        runtime_strategy_receipt_from_rows(
            discontinuous,
            strategy=strategy,
            runtime=runtime,
        )
    forged_path_exit = SimpleNamespace(
        _r013_last_mode="path",
        _r013_last_strategy_correction_n=0.0,
        _r013_last_effective_target_n=5.0,
        _r013_last_unmodified_target_n=5.0,
        _r013_strategy_safe_return_observed=False,
        _r013_strategy_exit_mode=None,
    )
    with pytest.raises(ValueError, match="PATH/exit"):
        runtime_strategy_receipt_from_rows(
            rows,
            strategy=strategy,
            runtime=forged_path_exit,
        )


def test_runtime_strategy_changes_only_path_target_and_restores_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = _strategy()
    monkeypatch.setattr(live_runtime_module, "_ACTIVE_STRATEGY", strategy)

    class Base:
        def __init__(self) -> None:
            self.candidate = SimpleNamespace()
            self.calls = []

        def desired_twist(self, *, mode: str, **kwargs):
            self.calls.append((mode, dict(kwargs)))
            return (0.0,) * 6

    runtime = _make_runtime_class(Base)()
    runtime.desired_twist(mode="path", path_time_s=32.5, internal_setpoint_n=5.0)
    expected = 5.0 - target_correction_n(
        strategy,
        path_time_s=32.5,
        mode="path",
    )
    assert runtime.calls[-1][1]["internal_setpoint_n"] == pytest.approx(expected)
    assert runtime._r013_last_strategy_correction_n > 1.0

    with pytest.raises(ValueError, match="not verified"):
        mark_r013_safe_return_transition(runtime, safe_return_verified=False)
    mark_r013_safe_return_transition(runtime, safe_return_verified=True)
    assert runtime._r013_last_mode == "safe_return"
    assert runtime._r013_last_strategy_correction_n == 0.0
    assert runtime._r013_last_effective_target_n == 5.0
    assert runtime._r013_last_unmodified_target_n == 5.0
    assert "mode_exit_or_home" in runtime.r013_reset_reasons
    assert runtime._r013_strategy_safe_return_observed is True
    assert runtime._r013_strategy_exit_mode == "safe_return"

    runtime.desired_twist(mode="hold", path_time_s=60.0, internal_setpoint_n=5.0)
    assert runtime.calls[-1][1]["internal_setpoint_n"] == 5.0
    assert runtime._r013_last_strategy_correction_n == 0.0

    runtime.desired_twist(mode="baseline", path_time_s=0.0, internal_setpoint_n=1.0)
    assert runtime.calls[-1][1]["internal_setpoint_n"] == 1.0

    with pytest.raises(ValueError, match="fixed 5 N"):
        runtime.desired_twist(mode="path", path_time_s=20.0, internal_setpoint_n=4.9)


def test_i_off_runtime_uses_legacy_policy_while_i_on_uses_conditional_clamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage A must not send zero I into the strict conditional policy."""

    seen: list[str] = []

    def original(config, state, _inputs, **_kwargs):
        seen.append(config.force_integral_policy)
        return SimpleNamespace(next_state=state, diagnostics={})

    state = Step5dOuterLoopState()
    config = Step5dOuterLoopConfig()

    i_off_runtime = SimpleNamespace(
        _r013_candidate=SimpleNamespace(i_off=True),
        _r013_compute_mode=None,
        _r013_last_diagnostics={},
        _r013_handoff=SimpleNamespace(receipt=lambda: {}),
    )
    monkeypatch.setattr(live_runtime_module, "_ACTIVE_RUNTIME", i_off_runtime)
    live_runtime_module._r013_compute(original, config, state, object())
    assert seen[-1] == LEGACY_FORCE_INTEGRAL_POLICY

    i_on_runtime = SimpleNamespace(
        _r013_candidate=SimpleNamespace(i_off=False),
        _r013_compute_mode=None,
        _r013_last_diagnostics={},
        _r013_handoff=SimpleNamespace(receipt=lambda: {}),
    )
    monkeypatch.setattr(live_runtime_module, "_ACTIVE_RUNTIME", i_on_runtime)
    live_runtime_module._r013_compute(original, config, state, object())
    assert seen[-1] == CONDITIONAL_DOUBLE_CLAMP_POLICY


def test_limit_aware_candidate_reaches_outer_loop_config_before_compute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []

    def original(config, state, _inputs, **_kwargs):
        seen.append(float(config.force_integral_limit_n_s))
        return SimpleNamespace(next_state=state, diagnostics={})

    runtime = SimpleNamespace(
        _r013_candidate=SimpleNamespace(
            i_off=False,
            force_p_gain=0.019027313840405524,
            force_i_gain=0.001810193359837562,
            integral_state_limit_n_s=5.0,
        ),
        _r013_compute_mode=None,
        _r013_last_diagnostics={},
        _r013_handoff=SimpleNamespace(receipt=lambda: {}),
    )
    monkeypatch.setattr(live_runtime_module, "_ACTIVE_RUNTIME", runtime)
    live_runtime_module._r013_compute(
        original,
        Step5dOuterLoopConfig(),
        Step5dOuterLoopState(),
        object(),
    )
    assert seen == [pytest.approx(5.0)]


def test_i_off_legacy_path_does_not_update_integral_state() -> None:
    inputs = Step5dOuterLoopInputs(
        tcp_pose_base=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        tcp_speed_base=(0.0,) * 6,
        force_tcp_n=(0.0, 0.0, 5.0),
        x_pd_base=(0.0, 0.0, 0.0),
        xdot_pd_base=(0.0, 0.0, 0.0),
        dt_s=0.002,
        integral_enabled=False,
        integral_reset_reason="i_off",
        control_reaction_normal_base=(0.0, 0.0, 1.0),
    )
    output = compute_step5d_outer_loop(
        Step5dOuterLoopConfig(force_integral_policy=LEGACY_FORCE_INTEGRAL_POLICY),
        Step5dOuterLoopState(force_integral_n_s=0.75),
        inputs,
    )
    assert output.next_state.force_integral_n_s == pytest.approx(0.75)
    receipt = anti_windup_metrics_from_rows(
        [
            {
                "force_integral_n_s": 0.0,
                "force_integral_limit_n_s": 1.0,
                "integral_i_term": 0.0,
                "integral_saturated": False,
                "integral_conditional_frozen": False,
            }
        ],
        candidate={"force_p_gain": 0.02, "force_i_gain": 0.0, "i_off": True},
    )
    assert receipt["policy"] == LEGACY_FORCE_INTEGRAL_POLICY
    assert receipt["max_abs_integral_n_s"] == 0.0


def test_runtime_strategy_validation_is_not_repeated_in_500hz_hot_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = _strategy()
    monkeypatch.setattr(live_runtime_module, "_ACTIVE_STRATEGY", strategy)

    class Base:
        def __init__(self) -> None:
            self.candidate = SimpleNamespace()

        def desired_twist(self, *, mode: str, **kwargs):
            return (0.0,) * 6

    runtime = _make_runtime_class(Base)()

    def fail_if_revalidated(_value):
        raise AssertionError("runtime strategy was revalidated in the control tick")

    monkeypatch.setattr(
        live_runtime_module,
        "validate_runtime_strategy",
        fail_if_revalidated,
    )
    runtime.desired_twist(
        mode="baseline",
        path_time_s=0.0,
        internal_setpoint_n=1.0,
    )
    runtime.desired_twist(
        mode="path",
        path_time_s=32.5,
        internal_setpoint_n=5.0,
    )
    assert runtime._r013_last_strategy_correction_n > 1.0


def test_installed_runtime_patch_rejects_a_different_second_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = _strategy()
    different = json.loads(json.dumps(strategy))
    different["target_correction_n"][2] += 0.01
    monkeypatch.setattr(live_runtime_module, "_INSTALLED", True)
    monkeypatch.setattr(live_runtime_module, "_ACTIVE_STRATEGY", strategy)

    live_runtime_module.install_r013_runtime_patch(
        strategy, handoff_policy="freeze_carry_v1"
    )
    with pytest.raises(ValueError, match="another strategy"):
        live_runtime_module.install_r013_runtime_patch(
            different, handoff_policy="freeze_carry_v1"
        )


def test_strategy_sidecar_binds_and_detects_tampering(tmp_path: Path) -> None:
    strategy = _strategy()
    path = tmp_path / "strategy-sidecar.jsonl"
    ledger = RuntimeStrategyReceiptLedger(
        path,
        campaign_id="campaign",
        run_id="run",
        attempt_id="attempt",
        strategy=strategy,
    )
    campaign = _campaign(tmp_path / "campaign")
    dispatch, _ = campaign.ask()
    admission = _admission(dispatch, admitted=True)
    row = ledger.append(
        dispatch_id=dispatch.dispatch_id,
        physical_admission=admission.as_dict(),
        runtime_strategy_receipt=_receipt(strategy),
    )
    assert len(row["row_sha256"]) == 64
    RuntimeStrategyReceiptLedger(
        path,
        campaign_id="campaign",
        run_id="run",
        attempt_id="attempt",
        strategy=strategy,
    )

    values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    values[-1]["runtime_strategy_receipt"]["enabled"] = False
    path.write_text("\n".join(json.dumps(value) for value in values) + "\n", encoding="utf-8")
    with pytest.raises(R013RuntimeStrategyError, match="chain|receipt"):
        RuntimeStrategyReceiptLedger(
            path,
            campaign_id="campaign",
            run_id="run",
            attempt_id="attempt",
            strategy=strategy,
        )


def test_strategy_resume_requires_sidecar_and_exact_campaign_cross_join(
    tmp_path: Path,
) -> None:
    strategy = _strategy()
    campaign = _campaign(tmp_path / "campaign")
    sidecar_path = tmp_path / "strategy-sidecar.jsonl"
    sidecar = RuntimeStrategyReceiptLedger(
        sidecar_path,
        campaign_id=str(campaign.ledger.header["campaign_id"]),
        run_id=str(campaign.ledger.header["run_id"]),
        attempt_id=str(campaign.ledger.header["attempt_id"]),
        strategy=strategy,
    )
    dispatch, _ = campaign.ask()
    admission = _admission(dispatch, admitted=True, objective=0.5)
    receipt = _receipt(strategy)
    sidecar_row = sidecar.append(
        dispatch_id=dispatch.dispatch_id,
        physical_admission=admission.as_dict(),
        runtime_strategy_receipt=receipt,
    )
    campaign.tell_exact(
        admission=admission,
        anti_windup_metrics=_metrics(),
        runtime_strategy_receipt=receipt,
        runtime_strategy_sidecar_sha256=str(sidecar_row["row_sha256"]),
    )
    sidecar.validate_campaign_records(campaign.ledger.records)
    physical_record = SimpleNamespace(
        candidate=dict(dispatch.candidate),
        attempt_sequence=admission.attempt_sequence,
        mae_n=admission.sealed_mae_n,
        eligible=admission.physical_eligible,
        timing_gate=admission.timing_gate,
        motion_gate=admission.motion_gate,
        qualification_passed=admission.qualification_passed,
        observation_uid=admission.observation_uid,
        sealed=True,
        metrics={"execution_id": admission.execution_id},
    )
    _validate_strategy_physical_bindings(
        strategy_sidecar=sidecar,
        campaign=campaign,
        physical_records=(physical_record,),
    )
    forged_physical = SimpleNamespace(
        **{
            **physical_record.__dict__,
            "mae_n": 0.3,
        }
    )
    with pytest.raises(R013OwnerError, match="physical admission differs"):
        _validate_strategy_physical_bindings(
            strategy_sidecar=sidecar,
            campaign=campaign,
            physical_records=(forged_physical,),
        )

    forged_records = [dict(record) for record in campaign.ledger.records]
    forged_records[-1] = {
        **forged_records[-1],
        "payload": {
            **forged_records[-1]["payload"],
            "runtime_strategy_sidecar_sha256": "0" * 64,
        },
    }
    with pytest.raises(R013RuntimeStrategyError, match="campaign receipt binding"):
        sidecar.validate_campaign_records(forged_records)

    sidecar_path.unlink()
    with pytest.raises(R013RuntimeStrategyError, match="missing on resume"):
        RuntimeStrategyReceiptLedger(
            sidecar_path,
            campaign_id=str(campaign.ledger.header["campaign_id"]),
            run_id=str(campaign.ledger.header["run_id"]),
            attempt_id=str(campaign.ledger.header["attempt_id"]),
            strategy=strategy,
            must_exist=True,
        )


def test_enabled_snapshot_requires_persisted_strategy_hash(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    records = [
        json.loads(line)
        for line in campaign.ledger.path.read_text(encoding="utf-8").splitlines()
    ]
    del records[0]["optimizer_snapshot"]["runtime_strategy_sha256"]
    campaign.ledger.path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(R013CampaignError, match="snapshot lacks its identity"):
        Campaign.resume(campaign.ledger.path)


def test_replay_binds_objective_to_physical_admission_mae(tmp_path: Path) -> None:
    strategy = _strategy()
    campaign = _campaign(tmp_path)
    dispatch, _ = campaign.ask()
    campaign.tell_exact(
        admission=_admission(dispatch, admitted=True, objective=0.5),
        anti_windup_metrics=_metrics(),
        runtime_strategy_receipt=_receipt(strategy),
        runtime_strategy_sidecar_sha256="1" * 64,
    )
    records = [
        json.loads(line)
        for line in campaign.ledger.path.read_text(encoding="utf-8").splitlines()
    ]
    records[-1]["payload"]["objective_n"] = 0.3
    campaign.ledger.path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(R013CampaignError, match="objective/noise differs"):
        Campaign.resume(campaign.ledger.path)


def test_strategy_canary_counts_rejections_and_stops_before_warm_or_bo(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    strategy_receipt = _receipt(_strategy())
    first, proposal = campaign.ask()
    assert first.kind == STRATEGY_CANARY_KIND
    assert proposal is None
    with pytest.raises(R013CampaignError, match="receipt"):
        campaign.tell_exact(
            admission=_admission(first, admitted=False),
            anti_windup_metrics=_metrics(),
        )
    campaign.tell_exact(
        admission=_admission(first, admitted=False),
        anti_windup_metrics=_metrics(),
        runtime_strategy_receipt=strategy_receipt,
        runtime_strategy_sidecar_sha256="a" * 64,
    )
    for _ in range(3):
        dispatch, proposal = campaign.ask()
        assert dispatch.kind == STRATEGY_CANARY_KIND
        assert proposal is None
        campaign.tell_exact(
            admission=_admission(dispatch, admitted=True, objective=0.5),
            anti_windup_metrics=_metrics(),
            runtime_strategy_receipt=strategy_receipt,
            runtime_strategy_sidecar_sha256="b" * 64,
        )
    assert campaign.strategy_canary_summary["physical_attempt_count"] == 4
    assert campaign.strategy_canary_summary["rejected_attempt_count"] == 1
    assert campaign.strategy_canary_summary["complete"] is True
    assert campaign.target_achieved is False
    with pytest.raises(R013CampaignError, match="canary is terminal"):
        campaign.ask()
    resumed = Campaign.resume(campaign.ledger.path)
    assert resumed.strategy_canary_summary == campaign.strategy_canary_summary


def test_strategy_canary_confirmation_can_achieve_target(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    strategy_receipt = _receipt(_strategy())
    kinds = []
    for objective in (0.30, 0.34, 0.35):
        dispatch, _ = campaign.ask()
        kinds.append(dispatch.kind)
        campaign.tell_exact(
            admission=_admission(dispatch, admitted=True, objective=objective),
            anti_windup_metrics=_metrics(),
            runtime_strategy_receipt=strategy_receipt,
            runtime_strategy_sidecar_sha256="c" * 64,
        )
    assert kinds == [STRATEGY_CANARY_KIND, "CONFIRMATION", "CONFIRMATION"]
    assert campaign.target_achieved is True
    assert campaign.confirmation_summary().confirmed_incumbent.admitted_exact_count == 3


def test_strategy_canary_has_six_attempt_fail_closed_limit(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    strategy_receipt = _receipt(_strategy())
    for _ in range(6):
        dispatch, _ = campaign.ask()
        campaign.tell_exact(
            admission=_admission(dispatch, admitted=False),
            anti_windup_metrics=_metrics(),
            runtime_strategy_receipt=strategy_receipt,
            runtime_strategy_sidecar_sha256="d" * 64,
        )
    assert campaign.strategy_canary_summary["attempt_limit_exhausted"] is True
    with pytest.raises(R013CampaignError, match="canary is terminal"):
        campaign.ask()


def test_strategy_canary_attempt_cap_never_hides_inflight_recovery(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    strategy_receipt = _receipt(_strategy())
    for _ in range(5):
        dispatch, _ = campaign.ask()
        campaign.tell_exact(
            admission=_admission(dispatch, admitted=False),
            anti_windup_metrics=_metrics(),
            runtime_strategy_receipt=strategy_receipt,
            runtime_strategy_sidecar_sha256="f" * 64,
        )
    campaign.ask()
    code, status = _startup_resume_terminal(campaign=campaign, run_dir=tmp_path)
    assert code == 1
    assert status["state"] == "recovery_required"


def test_strategy_canary_terminal_handler_revokes_without_claiming_success(
    tmp_path: Path,
) -> None:
    campaign = _campaign(tmp_path)
    strategy_receipt = _receipt(_strategy())
    for _ in range(3):
        dispatch, _ = campaign.ask()
        campaign.tell_exact(
            admission=_admission(dispatch, admitted=True, objective=0.5),
            anti_windup_metrics=_metrics(),
            runtime_strategy_receipt=strategy_receipt,
            runtime_strategy_sidecar_sha256="e" * 64,
        )
    stopped = []
    published = []
    terminal = _handle_strategy_canary_terminal(
        campaign=campaign,
        context=SimpleNamespace(stop=stopped.append),
        status={"state": "exact_sealed"},
        write_status=published.append,
    )
    assert terminal["state"] == "strategy_canary_complete_no_target"
    assert terminal["target_achieved"] is False
    assert stopped == ["strategy_canary_complete_no_target"]
    assert published == [terminal]


def test_disabled_strategy_cannot_install_strategy_canary(tmp_path: Path) -> None:
    campaign = Campaign.create(
        tmp_path / "disabled.jsonl",
        campaign_id="disabled",
        run_id="run",
        attempt_id="attempt",
        noise_floor_n2=0.01,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
        runtime_strategy=DISABLED_RUNTIME_STRATEGY,
    )
    with pytest.raises(R013CampaignError, match="enabled strategy"):
        campaign.install_strategy_canary_plan(
            strategy_canary_plan(runtime_strategy_sha256_value="0" * 64)
        )


def test_enabled_strategy_cannot_dispatch_before_fresh_canary_plan(
    tmp_path: Path,
) -> None:
    campaign = Campaign.create(
        tmp_path / "enabled-without-plan.jsonl",
        campaign_id="enabled",
        run_id="run",
        attempt_id="attempt",
        noise_floor_n2=0.01,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
        runtime_strategy=_strategy(),
    )
    with pytest.raises(R013CampaignError, match="lacks its fresh canary plan"):
        campaign.ask()
