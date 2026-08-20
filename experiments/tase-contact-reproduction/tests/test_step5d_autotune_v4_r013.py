from __future__ import annotations

from dataclasses import replace
import json
from inspect import getsource
import math
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from typing import Any, Callable, Sequence

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(REPO / "src" / "ur10e_experiment_runtime"))

import step5d_autotune_v4_r013.campaign as campaign_module  # noqa: E402
import step5d_autotune_v4_r004_live_writer as writer_module  # noqa: E402
import step5d_autotune_v4_r013.live_owner as live_owner_module  # noqa: E402

from kunwei_rtde_bridge import (  # noqa: E402
    BridgeState,
    STEP5D_INTEGRAL_POLICY_LEGACY,
    STEP5D_INTEGRAL_POLICY_R013,
    parse_args,
    step5d_conditional_anti_windup_step,
)
from step5d_paper_outer_loop import Step5dOuterLoopState  # noqa: E402
from step5d_autotune_v4_r013.campaign import (  # noqa: E402
    ANCHOR_RETEST_CANDIDATES,
    ANCHOR_RETEST_KIND,
    BUDGETED_FLOOR_V1,
    LOCAL_REFINEMENT_CANDIDATES,
    LOCAL_REFINEMENT_KIND,
    HIGH_KI_PROBE_CANDIDATES,
    HIGH_KI_PROBE_KIND,
    NORMAL_VELOCITY_GAIN_PROBE_CANDIDATES,
    NORMAL_VELOCITY_GAIN_PROBE_KIND,
    LOWER_P_OVER_D_PROBE_CANDIDATES,
    LOWER_P_OVER_D_PROBE_KIND,
    STRATEGY_CANARY_KIND,
    Campaign,
    CampaignFingerprint,
    CONFIRMATION_KIND,
    CONFIRMATION_THRESHOLD_N,
    FIXED_KI_SEEDS,
    MIN_EXACT_ROWS_FOR_BO,
    PhysicalAdmissionReceipt,
    R013CampaignError,
    anchor_retest_plan,
    candidate_token,
    candidate_pool,
    local_refinement_plan,
    high_ki_probe_plan,
    normal_velocity_gain_probe_plan,
    lower_p_over_d_probe_plan,
    select_seed_template,
    warm_start_candidates,
)
from step5d_autotune_v4_r013.domain import (  # noqa: E402
    EXTENDED_KI_MAX,
    KI_MAX,
    SENTINEL_KI,
    candidate_to_log_features,
    candidate_to_normalized,
    normalized_to_candidate,
    physical_candidate_key,
)
from step5d_autotune_v4_r013.gp import (  # noqa: E402
    ProductionGPConfig,
    R013GPError,
    _exact_row,
    fit_core_production_gp,
    grouped_observation_noise,
)
from step5d_autotune_v4_r013.campaign_config import (  # noqa: E402
    MANUAL_CANARY_ROLE,
    R013ManualCanaryPreparationProfileV1,
    load_r013_budgeted_floor_config,
    materialize_r013_handoff_selection,
)
from step5d_autotune_v4_r013.floor_coordinator import (  # noqa: E402
    BOUNDARY_NOVEL,
    CORE_BO_NOVEL,
    CORE_SOBOL_NOVEL,
    CORRECTION_BO_NOVEL,
    CORRECTION_SOBOL_NOVEL,
    POLISH_CORE_NOVEL,
    POLISH_CORRECTION_NOVEL,
    REPEAT,
    SENTINEL,
    FloorDiscoveryCoordinator,
    FloorDiscoveryPolicyV1,
    FloorCoordinatorError,
    FloorCandidateProposal,
    BlockProposalContract,
    ControllerCoreBlockV1,
    CorrectionBlockV1,
    FloorTrialSpec,
    CORE_PROPOSAL_CONTRACT,
    HandoffABPlanV1,
    RuntimePrimitiveNotInstalled,
)
from step5d_autotune_v4_r013.path_context import (  # noqa: E402
    CYCLOID_PATH_ID,
    FIGURE8_GEOMETRY_STATUS,
    CycloidPathProviderV1,
    FigureEightPathProviderV1,
    PathContextError,
)
from step5d_autotune_v4_r013.force_correction import (  # noqa: E402
    CORRECTION_FEATURE_NAMES,
    FORCE_FRAME_SEMANTICS,
    ForceCorrectionError,
    ForceCorrectionPolicyV1,
    ForceCorrectionStateV1,
)
from step5d_autotune_v4_r013.metrics import (  # noqa: E402
    CYCLOID_METRIC,
    FIGURE8_METRIC,
    GapPreservingMetricAccumulatorV1,
    MetricError,
    MetricFingerprintV1,
    MetricGapError,
)
from step5d_autotune_v4_r013.figure8_transfer import (  # noqa: E402
    AWAITING_CYCLOID_STATUS,
    FigureEightTransferPackageV1,
    load_figure8_transfer_template,
)
from step5d_autotune_v4_r013.ledger import LEDGER_SCHEMA, R013LedgerError  # noqa: E402
from step5d_autotune_v4_r013.contact_transient import (  # noqa: E402
    ContactTransientError,
    contact_transient_receipt,
)
from step5d_autotune_v4_r013.controller_triplet import (  # noqa: E402
    R013_V_FAR_M_S,
    R013_V_NEAR_M_S,
    build_controller_triplet,
    validate_controller_triplet,
)
from step5d_autotune_v4_r013.runtime_contract import (  # noqa: E402
    BridgeBinding,
    validate_bridge_binding,
)
from step5d_autotune_v4_r013.live_owner import (  # noqa: E402
    R013_PATH_ENTRY_RATE_LIMIT_ENV,
    R013OwnerError,
    _apply_r013_qualification_timing_acceptance,
    _bind_r012_guard_stack,
    _cleanup_failed_live_build,
    _epoch_qualification_records,
    _r013_attempt_kind,
    _validate_resident_ready_binding,
    build_r013_live_context,
)
import step5d_autotune_v4_r013.live_owner as live_owner_module  # noqa: E402
import step5d_autotune_v4_r013.prepare_live as prepare_module  # noqa: E402
from step5d_autotune_v4_r004.contracts import SCRIPT1_TARGET_POSE  # noqa: E402
from step5d_autotune_v4_r004.baseline_runtime import (  # noqa: E402
    BaselineObservation,
    PathEntryReleaseState,
    step_path_entry_release,
)
from step5d_autotune_v4_r004.qualification import CanonicalQualificationControl  # noqa: E402
from step5d_autotune_v4_r008.path_entry_rate_limit import (  # noqa: E402
    ENV_FLAG as R008_PATH_ENTRY_RATE_LIMIT_ENV,
    PathEntryRateLimitConfig,
)
from run_step5d_autotune_v4_r013_live import (  # noqa: E402
    _handle_target_achieved,
    _startup_resume_terminal,
)
from step5d_autotune_v4_r004.wire import AttemptKind  # noqa: E402
from step5d_autotune_v4_r012.path_cbf_live import (  # noqa: E402
    R012_HARD_TUBE_AXES_M,
    R012_SOFT_CBF_AXES_M,
    R012PathGuardStack,
)


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


def _trial_metrics() -> dict[str, object]:
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


def _admission(
    dispatch,
    *,
    physical_eligible: bool = True,
    timing_gate: bool = True,
    motion_gate: bool = True,
    sealed_mae_n: float = 1.0,
) -> PhysicalAdmissionReceipt:
    return PhysicalAdmissionReceipt(
        dispatch_id=dispatch.dispatch_id,
        candidate_token=candidate_token(dispatch.candidate),
        candidate_key=physical_candidate_key(dispatch.candidate),
        attempt_sequence=dispatch.ordinal,
        execution_id=f"test-{dispatch.dispatch_id}",
        sealed_mae_n=sealed_mae_n,
        physical_eligible=physical_eligible,
        timing_gate=timing_gate,
        motion_gate=motion_gate,
        qualification_passed=physical_eligible,
        observation_uid=f"observation-{dispatch.ordinal}",
    )


def _step(**overrides):
    values = {
        "force_error_n": 1.0,
        "integral_state_n_s": 0.0,
        "normal_velocity_m_s": 0.0,
        "dt_s": 0.1,
        "force_p_gain": 0.001,
        "force_i_gain": 0.001,
        "force_damping": 0.0,
        "normal_velocity_limit_m_s": 0.003,
    }
    values.update(overrides)
    return step5d_conditional_anti_windup_step(**values)


def _transient_rows(times, forces, *, filtered: bool = True):
    rows = []
    for time, force in zip(times, forces, strict=True):
        row = {
            "relative_path_time_s": time,
            "normal_load_n": force + 100.0,
        }
        if filtered:
            row["filtered_normal_n"] = force
        rows.append(row)
    return rows


def test_r013_controller_triplet_transforms_near_speed_and_retains_guards(tmp_path: Path) -> None:
    paths = build_controller_triplet(
        tmp_path,
        stamp="2026-08-15T0000Z_STEP5D_AUTOTUNE_V4_R013_613013",
    )
    script = paths["script"].read_text(encoding="utf-8")
    checks = validate_controller_triplet(
        script,
        paths["txt"].read_text(encoding="utf-8"),
        paths["urp"].read_bytes(),
    )
    assert all(checks.values())
    assert f"local v_far_m_s = {R013_V_FAR_M_S:.9f}" in script
    assert f"local v_near_m_s = {R013_V_NEAR_M_S:.9f}" in script
    assert "near=0.0002 m/s" in script
    assert "ACTIVE_LEASE_S = 0.080000000" in script
    assert "local force_fuse_n = 50.000000000" in script
    assert "travel >= 0.025000000" in script
    assert "contact_elapsed_s >= 90.000000000" in script


def test_path_entry_release_dwell_resets_and_opens_at_bounded_boundaries() -> None:
    good = BaselineObservation(
        dt_s=0.05,
        one_newton_latched=True,
        filtered_normal_n=4.0,
        raw_normal_n=3.0,
        force_norm_n=7.0,
        torque_norm_nm=0.30,
        sensor_fresh=True,
        stationary=True,
    )
    state = PathEntryReleaseState()
    for _ in range(9):
        state = step_path_entry_release(state, good)
    assert state.dwell_s == pytest.approx(0.45)
    assert state.opened is False
    state = step_path_entry_release(
        state,
        replace(good, filtered_normal_n=5.6),
    )
    assert state == PathEntryReleaseState()
    for _ in range(10):
        state = step_path_entry_release(state, good)
    assert state.dwell_s == pytest.approx(0.5)
    assert state.opened is True


def test_state21_release_source_holds_zero_qdot_until_tp_state25() -> None:
    source = getsource(CanonicalQualificationControl.step)
    pending = source.index("path_entry_release_dwell_pending")
    state25_check = source.index("if tp_state != 25")
    path_controller = source.index('mode="path"')
    assert pending < state25_check < path_controller
    assert "command_mode=CommandMode.BASELINE" in source[:pending]
    assert source[state25_check:path_controller].count("qdot=(0.0,) * 6") >= 1
    assert "path_errors(" in source[state25_check:path_controller]


def test_r006_native_control_initializes_shared_path_release_state() -> None:
    from step5d_autotune_v4_r006.live_adapter import (
        _R006NativeCanonicalQualificationControl,
    )

    source = getsource(_R006NativeCanonicalQualificationControl.__post_init__)
    assert "PathEntryReleaseGate" in source
    assert "self._path_entry_release_gate" in source
    assert "self._path_entry_release_state = PathEntryReleaseState()" in source


def test_r013_path_entry_ramp_is_scoped_and_r008_default_remains_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(R008_PATH_ENTRY_RATE_LIMIT_ENV, raising=False)
    assert PathEntryRateLimitConfig.from_environ().is_armed() is False
    source = getsource(build_r013_live_context)
    assert 'os.environ[R013_PATH_ENTRY_RATE_LIMIT_ENV] = "1"' in source
    assert "path_entry_rate_limit_env =" in source
    assert "os.environ.pop(R013_PATH_ENTRY_RATE_LIMIT_ENV, None)" in source
    monkeypatch.setattr(live_owner_module, "uninstall_r013_runtime_patch", lambda: None)
    monkeypatch.setenv(R013_PATH_ENTRY_RATE_LIMIT_ENV, "1")
    live_owner_module._cleanup_failed_live_build(
        adapter=None,
        state20_trace=None,
        state25_trace=None,
        register_writer=SimpleNamespace(close=lambda: None),
        path_entry_rate_limit_env=(False, None),
    )
    assert R013_PATH_ENTRY_RATE_LIMIT_ENV not in os.environ


def test_contact_transient_receipt_settling_trace_uses_filtered_force() -> None:
    rows = _transient_rows(
        [0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8],
        [8.0, 5.4, 5.2, 6.0, 5.3, 5.1, 5.0, 5.0, 5.0, 5.0],
    )
    receipt = contact_transient_receipt(rows)
    assert receipt["sample_count"] == 10
    assert receipt["first_force_n"] == pytest.approx(8.0)
    assert receipt["min_force_n"] == pytest.approx(5.0)
    assert receipt["max_force_n"] == pytest.approx(8.0)
    assert receipt["mae_to_target_n"] == pytest.approx(0.5)
    assert receipt["p95_abs_error_n"] == pytest.approx(3.0)
    assert receipt["max_abs_error_n"] == pytest.approx(3.0)
    assert receipt["maximum_gap_s"] == pytest.approx(0.2)
    assert receipt["first_continuous_band_1s_s"] == pytest.approx(0.8)
    assert receipt["rebound_count"] == 1


def test_contact_transient_receipt_rejects_bad_order_and_reports_gaps() -> None:
    gap_receipt = contact_transient_receipt(
        _transient_rows([0.0, 0.1, 0.5], [5.0, 5.0, 5.0])
    )
    assert gap_receipt["maximum_gap_s"] == pytest.approx(0.4)
    with pytest.raises(ContactTransientError, match="nonfinite"):
        contact_transient_receipt(
            [{"relative_path_time_s": 0.0, "filtered_normal_n": float("nan")}]
        )
    with pytest.raises(ContactTransientError, match="unsorted"):
        contact_transient_receipt(
            _transient_rows([0.0, 0.2, 0.1], [5.0, 5.0, 5.0])
        )


def test_contact_transient_receipt_never_settles_and_falls_back_to_raw_force() -> None:
    receipt = contact_transient_receipt(
        _transient_rows([0.0, 1.0, 2.0, 3.0, 4.0], [6.0] * 5, filtered=False)
    )
    assert receipt["first_force_n"] == pytest.approx(106.0)
    assert receipt["first_continuous_band_1s_s"] is None
    assert receipt["rebound_count"] == 0


def test_live_writer_packet_history_is_bounded_without_dropping_recent_qdot() -> None:
    history = writer_module.BoundedPacketHistory()
    count = writer_module.PACKET_HISTORY_LIMIT + 17
    for sequence in range(count):
        history.record(
            sequence,
            published_at_s=float(sequence),
            qdot=(float(sequence),) * 6,
        )

    expected_first = count - writer_module.PACKET_HISTORY_LIMIT
    assert len(history) == writer_module.PACKET_HISTORY_LIMIT
    assert history.oldest_sequence == expected_first
    assert history.consumed(count - 1).qdot == (float(count - 1),) * 6
    with pytest.raises(writer_module.LiveWriterError, match="regressed"):
        history.consumed(count - 2)
    missing = writer_module.BoundedPacketHistory(limit=2)
    for sequence in range(3):
        missing.record(sequence, published_at_s=float(sequence), qdot=(0.0,) * 6)
    with pytest.raises(writer_module.LiveWriterError, match="outside bounded history"):
        missing.consumed(0)


@pytest.mark.parametrize(
    ("velocity", "error"),
    ((0.0029, 10.0), (-0.0029, -10.0)),
)
def test_conditional_integration_freezes_both_saturation_directions(velocity, error) -> None:
    result = _step(
        force_error_n=error,
        integral_state_n_s=0.2 if error > 0.0 else -0.2,
        normal_velocity_m_s=velocity,
    )
    assert result.conditional_frozen
    assert result.velocity_saturated
    assert result.integral_state_n_s == pytest.approx(0.2 if error > 0.0 else -0.2)
    assert abs(result.applied_normal_velocity_m_s) == pytest.approx(0.003)


def test_reverse_error_unwinds_while_velocity_remains_saturated() -> None:
    result = _step(
        force_error_n=-0.01,
        integral_state_n_s=0.5,
        normal_velocity_m_s=0.004,
        force_damping=0.0,
    )
    assert result.velocity_saturated
    assert not result.conditional_frozen
    assert result.integral_state_n_s < 0.5
    assert result.applied_normal_velocity_m_s == pytest.approx(0.003)


def test_double_clamp_and_zero_dt() -> None:
    state_clamped = _step(
        force_error_n=100.0,
        dt_s=1.0,
        force_p_gain=0.01,
        force_i_gain=0.001,
        normal_velocity_limit_m_s=100.0,
    )
    assert state_clamped.state_clamped
    assert not state_clamped.authority_clamped
    assert state_clamped.integral_state_n_s == pytest.approx(1.0)

    authority_clamped = _step(
        force_error_n=1.0,
        dt_s=1.0,
        force_p_gain=0.001,
        force_i_gain=0.002,
        normal_velocity_limit_m_s=100.0,
    )
    assert authority_clamped.authority_clamped
    assert authority_clamped.integral_state_n_s == pytest.approx(0.25)
    assert abs(authority_clamped.i_term) == pytest.approx(0.5 * 0.001)

    unchanged = _step(integral_state_n_s=0.2, dt_s=0.0)
    assert unchanged.integral_state_n_s == pytest.approx(0.2)

    preexisting_violation = _step(
        integral_state_n_s=2.0,
        force_error_n=0.0,
        dt_s=0.0,
        force_p_gain=0.001,
        force_i_gain=0.0001,
    )
    assert preexisting_violation.state_clamped
    assert preexisting_violation.integral_state_n_s == pytest.approx(1.0)


def test_nonfinite_contact_loss_and_mode_transition_reset() -> None:
    with pytest.raises(ValueError, match="finite"):
        _step(force_error_n=math.nan)
    reset = _step(
        integral_state_n_s=0.8,
        integral_enabled=False,
        reset_reason="contact_loss",
    )
    assert reset.integral_state_n_s == 0.0
    assert reset.reset_reason == "contact_loss"

    state = BridgeState()
    state.step5d_outer_state = Step5dOuterLoopState(force_integral_n_s=0.8)
    state.step5d_integral_gain_signature = (0.001, 0.0001, 7.0)
    state.reset_line_contact()
    assert state.step5d_outer_state.force_integral_n_s == 0.0
    assert state.step5d_integral_gain_signature is None
    assert state.step5d_integral_reset_reason == "mode_exit_or_home"


@pytest.mark.parametrize(
    "reason",
    (
        "candidate_dispatch",
        "path_entry",
        "contact_loss_or_invalid_state",
        "contact_abort",
        "mode_exit_or_home",
        "path_exit",
        "abort_or_invalid_state",
        "path_gain_hot_switch",
    ),
)
def test_every_r013_boundary_reset_clears_state_and_trial_duty(reason: str) -> None:
    state = BridgeState()
    state.step5d_outer_state = Step5dOuterLoopState(force_integral_n_s=0.8)
    state.step5d_integral_gain_signature = (0.001, 0.0001, 7.0)
    state.step5d_integral_active_s = 1.0
    state.step5d_integral_saturated_s = 0.4
    state.step5d_integral_frozen_s = 0.2
    state.reset_conditional_integral(reason)
    assert state.step5d_outer_state.force_integral_n_s == 0.0
    assert state.step5d_integral_gain_signature is None
    assert state.step5d_integral_reset_reason == reason
    assert state.step5d_integral_active_s == 0.0
    assert state.step5d_integral_saturated_s == 0.0
    assert state.step5d_integral_frozen_s == 0.0


def test_legacy_default_is_unchanged_and_r013_is_opt_in() -> None:
    args = parse_args([])
    assert args.bridge_integral_policy == STEP5D_INTEGRAL_POLICY_LEGACY
    assert STEP5D_INTEGRAL_POLICY_R013 == "conditional-double-clamp-v1"


def test_six_dimensional_round_trip_lattice_and_sentinels() -> None:
    candidate = {**SEED, "force_i_gain": FIXED_KI_SEEDS[0], "i_off": False}
    unit = candidate_to_normalized(candidate)
    assert len(unit) == 6
    restored = normalized_to_candidate(unit)
    assert physical_candidate_key(restored) == physical_candidate_key(candidate)
    assert restored["i_off"] is False
    assert 0.0 < restored["force_i_gain"] <= KI_MAX
    for ki in SENTINEL_KI:
        admitted = {**candidate, "force_i_gain": ki}
        physical_candidate_key(admitted)
    with pytest.raises(ValueError, match="complete R013 6D domain"):
        physical_candidate_key({**candidate, "target_force_n": 6.0})


def test_seed_selection_fallback_and_fixed_warm_start() -> None:
    invalid_latest = {**SEED, "force_p_gain": 0.001, "force_damping": 224.0}
    source = {
        "confirmed_incumbent": {"candidate": invalid_latest, "confirmed": True},
        "confirmed_candidates": [
            {"candidate": SEED, "confirmed": True, "arithmetic_mean_sealed_mae_n": 0.7},
        ],
    }
    selected = select_seed_template(source)
    assert selected["force_p_gain"] == SEED["force_p_gain"]
    warm = warm_start_candidates(selected)
    assert len(warm) == MIN_EXACT_ROWS_FOR_BO
    assert [row["force_i_gain"] for row in warm[:6]] == [
        FIXED_KI_SEEDS[0], FIXED_KI_SEEDS[0], FIXED_KI_SEEDS[0],
        FIXED_KI_SEEDS[1], FIXED_KI_SEEDS[1], FIXED_KI_SEEDS[1],
    ]
    assert all(row["i_off"] is False for row in warm)


def test_fresh_ledger_warm_start_resume_noise_receipt_and_hard_guard(tmp_path: Path) -> None:
    path = tmp_path / "r013.jsonl"
    source = {"confirmed_incumbent": {"candidate": SEED, "confirmed": True}}
    campaign = Campaign.create(
        path,
        campaign_id="r013-test",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source=source,
    )
    assert campaign.ledger.header["schema"] == LEDGER_SCHEMA
    assert campaign.gp_config.noise_floor_n2 == pytest.approx(0.023)
    assert campaign.gp_config.as_dict()["sealed_mae_transform"] == "identity"
    with pytest.raises(R013LedgerError, match="already exists"):
        Campaign.create(
            path,
            campaign_id="r013-test",
            run_id="offline",
            attempt_id="a2",
            noise_floor_n2=0.023,
            r012_seed_source=source,
        )
    for index in range(6):
        dispatch, proposal = campaign.ask()
        assert proposal is None and not dispatch.abort_allowed
        campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=1.0 + index * 0.01),
            anti_windup_metrics=_trial_metrics(),
        )
    resumed = Campaign.resume(path)
    assert resumed.exact_row_count == 6
    for index in range(6, MIN_EXACT_ROWS_FOR_BO):
        dispatch, proposal = resumed.ask()
        assert proposal is None and dispatch.kind == "WARM_SOBOL"
        resumed.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=1.0 + index * 0.01),
            anti_windup_metrics=_trial_metrics(),
        )
    assert resumed.exact_row_count == MIN_EXACT_ROWS_FOR_BO
    assert len({row["dispatch_id"] for row in resumed.observations}) == 12

    guard_path = tmp_path / "guard.jsonl"
    guarded = Campaign.create(
        guard_path,
        campaign_id="r013-guard",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.01,
        r012_seed_source=source,
    )
    guarded.ask()
    receipt = guarded.tell_hard_guard(reason="force_norm_stop")
    assert receipt["action"] == "stop_no_retry"
    with pytest.raises(R013CampaignError, match="stopped"):
        guarded.ask()


def test_physical_admission_overlap_pool_and_confirmed_incumbent(tmp_path: Path) -> None:
    source = {"confirmed_incumbent": {"candidate": SEED, "confirmed": True}}
    campaign = Campaign.create(
        tmp_path / "admission.jsonl",
        campaign_id="r013-admission",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source=source,
    )
    dispatch, _ = campaign.ask()
    rejected = campaign.tell_exact(
        admission=_admission(
            dispatch,
            physical_eligible=False,
            timing_gate=False,
        ),
        anti_windup_metrics=_trial_metrics(),
    )
    assert rejected["status"] == "rejected_ineligible"
    assert campaign.exact_row_count == 0
    assert campaign.in_flight is None
    assert len(campaign.rejected_admissions) == 1

    dispatch, _ = campaign.ask()
    motion_rejected = campaign.tell_exact(
        admission=_admission(dispatch, motion_gate=False, sealed_mae_n=0.1),
        anti_windup_metrics=_trial_metrics(),
    )
    assert motion_rejected["status"] == "rejected_ineligible"
    assert motion_rejected["reasons"] == ["motion_gate=false"]
    assert campaign.exact_row_count == 0

    dispatch, _ = campaign.ask()
    campaign.tell_exact(
        admission=_admission(dispatch, sealed_mae_n=0.8),
        anti_windup_metrics=_trial_metrics(),
    )
    assert campaign.exact_row_count == 1
    assert campaign.observations[-1]["eligible"] is True

    first_pool = candidate_pool(round_index=0)
    excluded = tuple(physical_candidate_key(row) for row in first_pool[:5])
    filled_pool = candidate_pool(round_index=0, evaluated_keys=excluded)
    assert len(filled_pool) == 128
    assert len({physical_candidate_key(row) for row in filled_pool}) == 128
    assert not ({physical_candidate_key(row) for row in filled_pool} & set(excluded))
    assert filled_pool == candidate_pool(round_index=0, evaluated_keys=excluded)

    repeats = Campaign.create(
        tmp_path / "confirmation.jsonl",
        campaign_id="r013-confirmation",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source=source,
    )
    for value in (0.30, 0.34):
        dispatch, _ = repeats.ask()
        repeats.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=value),
            anti_windup_metrics=_trial_metrics(),
        )
    before_confirmation = repeats.confirmation_summary()
    assert before_confirmation.single_trial_minimum_sealed_mae_n == pytest.approx(0.30)
    assert before_confirmation.confirmed_incumbent is None

    dispatch, _ = repeats.ask()
    repeats.tell_exact(
        admission=_admission(dispatch, sealed_mae_n=0.36),
        anti_windup_metrics=_trial_metrics(),
    )
    incumbent = repeats.confirmation_summary().confirmed_incumbent
    assert incumbent is not None
    assert incumbent.admitted_exact_count == 3
    assert incumbent.confirmed is True
    assert incumbent.minimum_sealed_mae_n == pytest.approx(0.30)
    assert incumbent.arithmetic_mean_sealed_mae_n == pytest.approx(1.0 / 3.0)


def test_budgeted_floor_integrates_strict_admission_noise_and_resumable_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fingerprint = CampaignFingerprint(
        path_id="r013-integrated-path",
        metric_fingerprint="force-mae-v2-sealed|integrated-test",
        handoff_policy="freeze_carry_v1",
        correction_runtime_strategy_identity="disabled",
        source_identity="fresh-test-source",
        eoat_identity="new-eoat-v4",
        home_tare_identity="home-tare-test",
        controller_lineage="r013-test-lineage",
    )
    campaign_path = tmp_path / "budgeted-integrated.jsonl"
    campaign = Campaign.create(
        campaign_path,
        campaign_id="r013-budgeted-integrated",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.005,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
        completion_policy=BUDGETED_FLOOR_V1,
        handoff_policy="freeze_carry_v1",
        campaign_fingerprint=fingerprint,
    )

    def strict_admission(
        dispatch,
        *,
        mae: float,
        epoch: bool | None = True,
        trial: bool | None = True,
        receipt_fingerprint: CampaignFingerprint | None = fingerprint,
    ) -> PhysicalAdmissionReceipt:
        return PhysicalAdmissionReceipt(
            dispatch_id=dispatch.dispatch_id,
            candidate_token=dispatch.candidate_token,
            candidate_key=physical_candidate_key(dispatch.candidate),
            attempt_sequence=dispatch.ordinal,
            execution_id=f"strict-{dispatch.dispatch_id}",
            sealed_mae_n=mae,
            physical_eligible=False,
            timing_gate=True,
            motion_gate=True,
            qualification_passed=False,
            observation_uid=f"strict-observation-{dispatch.ordinal}",
            epoch_qualification_passed=epoch,
            trial_admission_passed=trial,
            campaign_fingerprint=receipt_fingerprint,
        )

    dispatch, _ = campaign.ask()
    wrong_fingerprint = replace(fingerprint, source_identity="wrong-source")
    with pytest.raises(R013CampaignError, match="fingerprint"):
        campaign.tell_exact(
            admission=strict_admission(dispatch, mae=0.30, receipt_fingerprint=wrong_fingerprint),
            anti_windup_metrics=_trial_metrics(),
        )
    with pytest.raises(R013CampaignError, match="ambiguous qualification"):
        campaign.tell_exact(
            admission=strict_admission(dispatch, mae=0.30, epoch=None, trial=None),
            anti_windup_metrics=_trial_metrics(),
        )

    checkpoint = campaign.tell_exact(
        admission=strict_admission(dispatch, mae=0.30),
        anti_windup_metrics=_trial_metrics(),
    )
    assert checkpoint["eligible"] is True
    assert checkpoint["physical_admission"]["physical_eligible"] is False
    assert campaign.completion_status["target_checkpoint"] is True
    assert campaign.completion_status["complete"] is False
    next_dispatch, _ = campaign.ask()
    assert next_dispatch.kind != CONFIRMATION_KIND
    rejected = campaign.tell_exact(
        admission=strict_admission(next_dispatch, mae=0.40, epoch=False, trial=False),
        anti_windup_metrics=_trial_metrics(),
    )
    assert rejected["reasons"] == [
        "epoch_qualification_passed=false",
        "trial_admission_passed=false",
    ]
    for value in (0.40, 0.40):
        repeat, _ = campaign.ask()
        assert repeat.kind != CONFIRMATION_KIND
        campaign.tell_exact(
            admission=strict_admission(repeat, mae=value),
            anti_windup_metrics=_trial_metrics(),
        )

    monkeypatch.setattr(
        campaign_module,
        "fit_production_gp",
        lambda _observations, *, config: SimpleNamespace(
            fit_receipt={"schema": "test-r013-fit-receipt-v1"}
        ),
    )
    monkeypatch.setattr(
        campaign_module,
        "ask_qlognei",
        lambda _fit, pool, *, evaluated_keys: SimpleNamespace(candidate=pool[0]),
    )
    while campaign.novel_count < 200:
        next_dispatch, _ = campaign.ask()
        campaign.tell_exact(
            admission=strict_admission(next_dispatch, mae=0.40),
            anti_windup_metrics=_trial_metrics(),
        )

    assert campaign.novel_count == 200
    assert campaign.target_achieved is True
    grouped = grouped_observation_noise(campaign.observations, config=campaign.gp_config)
    first_key = physical_candidate_key(checkpoint["candidate"])
    group = next(item for item in grouped if tuple(item["candidate_key"]) == first_key)
    values = [
        float(row["objective_n"])
        for row in campaign.observations
        if physical_candidate_key(row["candidate"]) == first_key
    ]
    assert group["n"] == len(values)
    assert group["mean_n"] == pytest.approx(math.fsum(values) / len(values))
    assert group["shrinkage_nu0"] == 2
    assert group["yvar_n2"] == pytest.approx(
        max(
            1e-4,
            min(
                2e-2,
                (
                    (len(values) - 1) * group["sample_variance_n2"]
                    + 2 * group["pooled_within_fingerprint_variance_n2"]
                )
                / (len(values) - 1 + 2)
                / len(values),
            ),
        )
    )

    fresh = candidate_pool(
        round_index=campaign.bo_trial_count,
        evaluated_keys=campaign.evaluated_keys,
    )
    fresh_keys = {physical_candidate_key(candidate) for candidate in fresh}
    assert len(fresh) == 128
    assert len(fresh_keys) == 128
    assert not fresh_keys.intersection(campaign.evaluated_keys)
    resumed = Campaign.resume(campaign_path)
    assert resumed.candidate_pool_state == campaign.candidate_pool_state
    assert candidate_pool(
        round_index=resumed.bo_trial_count,
        evaluated_keys=resumed.evaluated_keys,
    ) == fresh


def test_admitted_threshold_hit_prioritizes_same_candidate_confirmation(tmp_path: Path) -> None:
    campaign = Campaign.create(
        tmp_path / "trigger.jsonl",
        campaign_id="r013-trigger",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    dispatch, _ = campaign.ask()
    key = physical_candidate_key(dispatch.candidate)
    campaign.tell_exact(
        admission=_admission(dispatch, sealed_mae_n=CONFIRMATION_THRESHOLD_N),
        anti_windup_metrics=_trial_metrics(),
    )

    confirmation, proposal = campaign.ask()
    assert proposal is None
    assert confirmation.kind == CONFIRMATION_KIND
    assert confirmation.abort_allowed is False
    assert physical_candidate_key(confirmation.candidate) == key
    assert campaign.snapshot["confirmation_target"]["sealed_mae_threshold_n"] == pytest.approx(0.35)
    assert campaign.snapshot["confirmation_target"]["minimum_admitted_repeats"] == 3


def test_fixed_warm_confirmation_repeats_consume_remaining_planned_slots(tmp_path: Path) -> None:
    campaign = Campaign.create(
        tmp_path / "fixed-warm-confirmation.jsonl",
        campaign_id="r013-fixed-warm-confirmation",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    first_key = physical_candidate_key(campaign.warm[0])
    for value in (0.30, 0.40, 0.40):
        dispatch, proposal = campaign.ask()
        assert proposal is None
        assert physical_candidate_key(dispatch.candidate) == first_key
        campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=value),
            anti_windup_metrics=_trial_metrics(),
        )

    assert campaign.warm_slot_cursor == 3
    next_dispatch, proposal = campaign.ask()
    assert proposal is None
    assert next_dispatch.kind == "WARM_FIXED_KI"
    assert physical_candidate_key(next_dispatch.candidate) == physical_candidate_key(campaign.warm[3])


def test_unique_sobol_confirmation_does_not_skip_following_warm_slots(tmp_path: Path) -> None:
    campaign = Campaign.create(
        tmp_path / "sobol-warm-confirmation.jsonl",
        campaign_id="r013-sobol-warm-confirmation",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    for index in range(6):
        dispatch, proposal = campaign.ask()
        assert proposal is None
        assert physical_candidate_key(dispatch.candidate) == physical_candidate_key(campaign.warm[index])
        campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=1.0),
            anti_windup_metrics=_trial_metrics(),
        )

    trigger, proposal = campaign.ask()
    assert proposal is None
    assert trigger.kind == "WARM_SOBOL"
    trigger_key = physical_candidate_key(trigger.candidate)
    assert trigger_key == physical_candidate_key(campaign.warm[6])
    campaign.tell_exact(
        admission=_admission(trigger, sealed_mae_n=0.30),
        anti_windup_metrics=_trial_metrics(),
    )
    for value in (0.40, 0.40):
        confirmation, proposal = campaign.ask()
        assert proposal is None
        assert confirmation.kind == CONFIRMATION_KIND
        assert physical_candidate_key(confirmation.candidate) == trigger_key
        campaign.tell_exact(
            admission=_admission(confirmation, sealed_mae_n=value),
            anti_windup_metrics=_trial_metrics(),
        )

    assert campaign.confirmation_summary().target_achieved is False
    assert campaign.warm_slot_cursor == 7
    next_dispatch, proposal = campaign.ask()
    assert proposal is None
    assert next_dispatch.kind == "WARM_SOBOL"
    assert physical_candidate_key(next_dispatch.candidate) == physical_candidate_key(campaign.warm[7])
    assert physical_candidate_key(next_dispatch.candidate) != physical_candidate_key(campaign.warm[9])


def test_bo_round_ignores_confirmation_rows_and_gp_fits_all_admitted_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = Campaign.create(
        tmp_path / "bo-confirmation-round.jsonl",
        campaign_id="r013-bo-confirmation-round",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    for index in range(MIN_EXACT_ROWS_FOR_BO):
        dispatch, proposal = campaign.ask()
        assert proposal is None
        assert campaign.warm_slot_cursor == index
        campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=1.0),
            anti_windup_metrics=_trial_metrics(),
        )
    assert campaign.warm_plan_satisfied is True

    fit_row_kinds: list[tuple[str, ...]] = []
    round_indices: list[int] = []
    original_candidate_pool = campaign_module.candidate_pool

    def fake_fit(observations, *, config):
        fit_row_kinds.append(tuple(str(row["kind"]) for row in observations))
        return SimpleNamespace(fit_receipt={"schema": "test-r013-fit-receipt-v1"})

    def traced_candidate_pool(*, round_index, evaluated_keys=(), pending_keys=()):
        round_indices.append(round_index)
        return original_candidate_pool(
            round_index=round_index,
            evaluated_keys=evaluated_keys,
            pending_keys=pending_keys,
        )

    def fake_ask_qlognei(_fit, pool, *, evaluated_keys):
        return SimpleNamespace(candidate=pool[0])

    monkeypatch.setattr(campaign_module, "fit_production_gp", fake_fit)
    monkeypatch.setattr(campaign_module, "candidate_pool", traced_candidate_pool)
    monkeypatch.setattr(campaign_module, "ask_qlognei", fake_ask_qlognei)

    first_bo, proposal = campaign.ask()
    assert proposal is not None
    assert first_bo.kind == "BO_TRIAL"
    assert first_bo.abort_allowed is True
    first_bo_key = physical_candidate_key(first_bo.candidate)
    campaign.tell_exact(
        admission=_admission(first_bo, sealed_mae_n=0.30),
        anti_windup_metrics=_trial_metrics(),
    )
    for value in (0.40, 0.40):
        confirmation, proposal = campaign.ask()
        assert proposal is None
        assert confirmation.kind == CONFIRMATION_KIND
        assert physical_candidate_key(confirmation.candidate) == first_bo_key
        campaign.tell_exact(
            admission=_admission(confirmation, sealed_mae_n=value),
            anti_windup_metrics=_trial_metrics(),
        )

    assert campaign.confirmation_summary().target_achieved is False
    next_bo, proposal = campaign.ask()
    assert proposal is not None
    assert next_bo.kind == "BO_TRIAL"
    assert next_bo.abort_allowed is True
    assert round_indices == [0, 1]
    assert len(fit_row_kinds[0]) == MIN_EXACT_ROWS_FOR_BO
    assert len(fit_row_kinds[1]) == MIN_EXACT_ROWS_FOR_BO + 3
    assert fit_row_kinds[1][-2:] == (CONFIRMATION_KIND, CONFIRMATION_KIND)


def test_rejected_confirmation_does_not_count_and_replay_repeats_same_key(tmp_path: Path) -> None:
    path = tmp_path / "rejected-confirmation.jsonl"
    campaign = Campaign.create(
        path,
        campaign_id="r013-rejected-confirmation",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    first, _ = campaign.ask()
    key = physical_candidate_key(first.candidate)
    campaign.tell_exact(
        admission=_admission(first, sealed_mae_n=0.30),
        anti_windup_metrics=_trial_metrics(),
    )
    confirmation, _ = campaign.ask()
    rejected = campaign.tell_exact(
        admission=_admission(
            confirmation,
            physical_eligible=False,
            timing_gate=False,
        ),
        anti_windup_metrics=_trial_metrics(),
    )
    assert rejected["status"] == "rejected_ineligible"
    assert campaign.exact_row_count == 1
    assert campaign.confirmation_summary().pending_confirmation is not None

    resumed = Campaign.resume(path)
    repeated, proposal = resumed.ask()
    assert proposal is None
    assert repeated.kind == CONFIRMATION_KIND
    assert physical_candidate_key(repeated.candidate) == key


def test_three_admitted_repeats_mean_at_or_below_target_fail_closed(tmp_path: Path) -> None:
    campaign = Campaign.create(
        tmp_path / "achieved.jsonl",
        campaign_id="r013-achieved",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    dispatches = []
    for value in (0.30, 0.34, 0.35):
        dispatch, _ = campaign.ask()
        dispatches.append(dispatch)
        campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=value),
            anti_windup_metrics=_trial_metrics(),
        )

    summary = campaign.confirmation_summary()
    assert campaign.target_achieved is True
    assert summary.target_achieved is True
    assert summary.confirmed_incumbent is not None
    assert summary.confirmed_incumbent.admitted_exact_count == 3
    assert summary.confirmed_incumbent.arithmetic_mean_sealed_mae_n == pytest.approx(0.33)
    assert [dispatch.kind for dispatch in dispatches] == [
        "WARM_FIXED_KI", CONFIRMATION_KIND, CONFIRMATION_KIND,
    ]
    with pytest.raises(R013CampaignError, match="target_achieved"):
        campaign.ask()


def test_failed_confirmation_resumes_normal_warm_scheduling(tmp_path: Path) -> None:
    campaign = Campaign.create(
        tmp_path / "failed-confirmation.jsonl",
        campaign_id="r013-failed-confirmation",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    first_key = None
    for value in (0.30, 0.40, 0.40):
        dispatch, _ = campaign.ask()
        first_key = first_key or physical_candidate_key(dispatch.candidate)
        campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=value),
            anti_windup_metrics=_trial_metrics(),
        )

    summary = campaign.confirmation_summary()
    assert summary.target_achieved is False
    assert summary.confirmed_incumbent is None
    failed = next(row for row in summary.candidate_summaries if row.physical_key == first_key)
    assert failed.confirmation_failed is True
    next_dispatch, proposal = campaign.ask()
    assert proposal is None
    assert next_dispatch.kind == "WARM_FIXED_KI"
    assert physical_candidate_key(next_dispatch.candidate) != first_key


def test_resume_replay_preserves_pending_confirmation_deterministically(tmp_path: Path) -> None:
    path = tmp_path / "replay-confirmation.jsonl"
    campaign = Campaign.create(
        path,
        campaign_id="r013-replay-confirmation",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    first, _ = campaign.ask()
    campaign.tell_exact(
        admission=_admission(first, sealed_mae_n=0.30),
        anti_windup_metrics=_trial_metrics(),
    )
    resumed = Campaign.resume(path)
    confirmation, _ = resumed.ask()
    replayed = Campaign.resume(path)
    assert replayed.in_flight is not None
    assert replayed.in_flight.kind == CONFIRMATION_KIND
    assert physical_candidate_key(replayed.in_flight.candidate) == physical_candidate_key(first.candidate)
    replayed.tell_exact(
        admission=_admission(replayed.in_flight, sealed_mae_n=0.34),
        anti_windup_metrics=_trial_metrics(),
    )
    resumed_again = Campaign.resume(path)
    final_confirmation, _ = resumed_again.ask()
    assert final_confirmation.kind == CONFIRMATION_KIND
    assert physical_candidate_key(final_confirmation.candidate) == physical_candidate_key(first.candidate)
    resumed_again.tell_exact(
        admission=_admission(final_confirmation, sealed_mae_n=0.35),
        anti_windup_metrics=_trial_metrics(),
    )
    assert Campaign.resume(path).target_achieved is True


def test_runner_target_status_safe_stop_precedes_close_and_next_ask(tmp_path: Path) -> None:
    campaign = Campaign.create(
        tmp_path / "runner-target.jsonl",
        campaign_id="r013-runner-target",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    for value in (0.30, 0.34, 0.35):
        dispatch, _ = campaign.ask()
        campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=value),
            anti_windup_metrics=_trial_metrics(),
        )

    events: list[tuple[str, object]] = []

    class FakeContext:
        def stop(self, reason: str) -> None:
            events.append(("stop", reason))

        def close(self) -> None:
            events.append(("close", None))

    context = FakeContext()
    terminal = _handle_target_achieved(
        campaign=campaign,
        context=context,
        status={"state": "exact_sealed"},
        write_status=lambda value: events.append(("status", value)),
    )
    assert terminal is not None
    assert terminal["state"] == "target_achieved"
    assert terminal["target_evidence"]["admitted_exact_count"] == 3
    assert events[0][0] == "status"
    assert events[1] == ("stop", "target_achieved")
    context.close()
    assert [kind for kind, _value in events] == ["status", "stop", "close"]
    with pytest.raises(R013CampaignError, match="target_achieved"):
        campaign.ask()


def test_runner_resume_terminal_states_do_not_open_live_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import run_step5d_autotune_v4_r013_live as runner_module

    def must_not_open(**_kwargs: object) -> None:
        pytest.fail("R013 resume startup opened a live context")

    monkeypatch.setattr(runner_module, "build_r013_live_context", must_not_open)

    target_run = tmp_path / "target-run"
    target_run.mkdir()
    target_campaign = Campaign.create(
        target_run / "r013_ledger.jsonl",
        campaign_id="r013-startup-target",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    for value in (0.30, 0.34, 0.35):
        dispatch, _ = target_campaign.ask()
        target_campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=value),
            anti_windup_metrics=_trial_metrics(),
        )
    target_decision = _startup_resume_terminal(
        campaign=Campaign.resume(target_campaign.ledger.path),
        run_dir=target_run,
    )
    assert target_decision is not None
    target_exit, target_status = target_decision
    assert target_exit == 0
    assert target_status["state"] == "target_achieved"
    assert target_status["active"] is False
    assert target_status["terminal"] is True
    assert target_status["target_evidence"]["admitted_exact_count"] == 3
    monkeypatch.setattr(
        runner_module,
        "_parse_args",
        lambda: SimpleNamespace(
            run_dir=target_run,
            poll_s=0.25,
            controller_host="offline-controller",
            kunwei_host="offline-kunwei",
            kunwei_port=5152,
            launch_profile=tmp_path / "unused-launch-profile.json",
        ),
    )
    assert runner_module.main() == 0
    written_target_status = json.loads(
        (target_run / "r013_campaign_status.json").read_text(encoding="utf-8")
    )
    assert written_target_status["active"] is False
    assert written_target_status["state"] == "target_achieved"
    assert written_target_status["target_evidence"]["physical_key"] == target_status[
        "target_evidence"
    ]["physical_key"]

    recovery_run = tmp_path / "recovery-run"
    recovery_run.mkdir()
    inflight_campaign = Campaign.create(
        recovery_run / "r013_ledger.jsonl",
        campaign_id="r013-startup-inflight",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    pending, _ = inflight_campaign.ask()
    recovery_decision = _startup_resume_terminal(
        campaign=Campaign.resume(inflight_campaign.ledger.path),
        run_dir=recovery_run,
    )
    assert recovery_decision is not None
    recovery_exit, recovery_status = recovery_decision
    assert recovery_exit != 0
    assert recovery_status["state"] == "recovery_required"
    assert recovery_status["active"] is False
    assert recovery_status["terminal"] is True
    assert recovery_status["recovery_required"]["dispatch"]["dispatch_id"] == pending.dispatch_id
    monkeypatch.setattr(
        runner_module,
        "_parse_args",
        lambda: SimpleNamespace(
            run_dir=recovery_run,
            poll_s=0.25,
            controller_host="offline-controller",
            kunwei_host="offline-kunwei",
            kunwei_port=5152,
            launch_profile=tmp_path / "unused-launch-profile.json",
        ),
    )
    assert runner_module.main() == 1
    written_recovery_status = json.loads(
        (recovery_run / "r013_campaign_status.json").read_text(encoding="utf-8")
    )
    assert written_recovery_status["active"] is False
    assert written_recovery_status["state"] == "recovery_required"
    assert written_recovery_status["recovery_required"]["dispatch"]["dispatch_id"] == pending.dispatch_id


def test_confirmation_maps_to_mature_batch_a_without_opening_live_context() -> None:
    assert _r013_attempt_kind(CONFIRMATION_KIND) is AttemptKind.BATCH_A
    assert _r013_attempt_kind(ANCHOR_RETEST_KIND) is AttemptKind.RETEST
    assert _r013_attempt_kind(LOCAL_REFINEMENT_KIND) is AttemptKind.RETEST
    assert _r013_attempt_kind(STRATEGY_CANARY_KIND) is AttemptKind.RETEST
    assert _r013_attempt_kind("QUALIFICATION") is AttemptKind.QUALIFICATION


def test_live_owner_keeps_r008_qualification_timing_and_diagnostics() -> None:
    source = getsource(build_r013_live_context)
    assert "R008LiveWriterAdapter(mature" in source
    assert "with r008_timing_scope():" in source


def test_r013_qualification_timing_rescue_is_narrow_and_bounded() -> None:
    from step5d_autotune_v4_r005.runtime import AttemptResult
    from step5d_autotune_v4_r006.live_adapter import R006Candidate

    timing = {
        "duration_s": 18.0002,
        "feedback_age_p99_s": 0.0076,
        "max_fresh_gap_s": 0.0046,
        "layer_rates_hz": {
            "writer_publishes": 472.9,
            "rtde_frames": 472.9,
            "tp_consumed_packet_echoes": 450.4,
            "kunwei_frames": 1000.0,
        },
    }
    metrics = {
        "qualification_packet_timing_gate": True,
        "timing_evidence": timing,
        "r008_timing_decision": {
            "eligible": False,
            "failures": ["tp_consumption_ratio_below_0p98"],
        },
    }
    result = AttemptResult(
        epoch=1,
        attempt_sequence=1,
        kind="QUALIFICATION",
        candidate=R006Candidate.from_canonical(SEED),
        safe_return=True,
        binding_ok=True,
        safety_gate=True,
        contact_gate=True,
        return_gate=True,
        motion_gate=False,
        timing_gate=False,
        identity_gate=True,
        qualification_passed=True,
        duration_s=0.0,
        metrics=metrics,
        execution_id="qualification-1",
    )
    accepted = _apply_r013_qualification_timing_acceptance(result)
    assert accepted.timing_gate is True
    assert accepted.metrics["parent_timing_gate_passed"] is False

    low_tp = dict(metrics)
    low_tp["timing_evidence"] = {
        **timing,
        "layer_rates_hz": {
            **timing["layer_rates_hz"],
            "tp_consumed_packet_echoes": 439.9,
        },
    }
    assert _apply_r013_qualification_timing_acceptance(
        replace(result, metrics=low_tp)
    ).timing_gate is False

    extra_failure = dict(metrics)
    extra_failure["r008_timing_decision"] = {
        "eligible": False,
        "failures": [
            "tp_consumption_ratio_below_0p98",
            "max_fresh_gap_not_below_20ms",
        ],
    }
    assert _apply_r013_qualification_timing_acceptance(
        replace(result, metrics=extra_failure)
    ).timing_gate is False

    inconsistent_decision = dict(metrics)
    inconsistent_decision["r008_timing_decision"] = {
        "eligible": True,
        "failures": ["tp_consumption_ratio_below_0p98"],
    }
    assert _apply_r013_qualification_timing_acceptance(
        replace(result, metrics=inconsistent_decision)
    ).timing_gate is False


def test_resumed_epoch_qualifications_continue_global_attempt_sequence() -> None:
    def record(epoch: int, sequence: int, kind: str = "QUALIFICATION") -> SimpleNamespace:
        return SimpleNamespace(
            epoch=epoch,
            attempt_sequence=sequence,
            kind=kind,
            qualification_eligible=True,
        )

    previous = tuple(record(10, sequence, "BO_TRIAL") for sequence in range(1, 45))
    current = (record(11, 45), record(11, 46))
    current_rows, qualifications = _epoch_qualification_records(
        previous + current,
        epoch=11,
    )
    assert current_rows == current
    assert qualifications == current
    with pytest.raises(R013OwnerError, match="not contiguous in epoch"):
        _epoch_qualification_records(previous + (record(11, 46),), epoch=11)


def test_failed_live_context_build_closes_writer_before_register_transport() -> None:
    events: list[str] = []

    class Trace20:
        def close(self) -> None:
            events.append("trace20.close")

    class Trace25:
        def flush(self) -> None:
            events.append("trace25.flush")

    class Adapter:
        def revoke_authority(self, reason: str) -> None:
            assert reason == "r013_live_context_build_failed"
            events.append("adapter.stop_failed")

        def close(self) -> None:
            events.append("adapter.close")

    class RegisterWriter:
        def close(self) -> None:
            events.append("register.close")

    _cleanup_failed_live_build(
        adapter=Adapter(),
        state20_trace=Trace20(),
        state25_trace=Trace25(),
        register_writer=RegisterWriter(),
    )
    assert events == [
        "trace20.close",
        "trace25.flush",
        "adapter.stop_failed",
        "adapter.close",
        "register.close",
    ]


def test_production_controller_readback_uses_fresh_get_primitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import upload_ur_tp_package as upload_module

    calls: list[dict[str, object]] = []

    def fresh_get(
        files: dict[str, Path],
        program: str,
        controller: str,
        target_dir: str,
        readback_dir: Path,
        *,
        helper: Path,
        helper_sha256: str,
    ) -> dict[str, dict[str, str]]:
        assert not readback_dir.exists()
        readback_dir.mkdir()
        hashes = {extension: prepare_module._sha(path) for extension, path in files.items()}
        calls.append(
            {
                "program": program,
                "controller": controller,
                "target_dir": target_dir,
                "helper": helper,
                "helper_sha256": helper_sha256,
            }
        )
        return {family: dict(hashes) for family in ("local", "controller", "readback")}

    monkeypatch.setattr(upload_module, "readback_only_existing", fresh_get)
    helper = tmp_path / "controller-helper.py"
    helper.write_text("fixture", encoding="utf-8")
    result = prepare_module._capture_controller_triplet(
        ROOT,
        tmp_path / "fresh-controller-get",
        controller_helper=helper,
        controller_helper_sha256="a" * 64,
    )
    assert calls == [
        {
            "program": prepare_module.R013_PROGRAM,
            "controller": upload_module.DEFAULT_CONTROLLER,
            "target_dir": "/programs/andyl/kunwei/step5",
            "helper": helper,
            "helper_sha256": "a" * 64,
        }
    ]
    assert result["source"] == "fresh_controller_get"
    assert result["operation"] == "readback"
    assert result["byte_closure"]["local"] == result["byte_closure"]["controller"]
    assert result["byte_closure"]["controller"] == result["byte_closure"]["readback"]


def test_create_live_run_resolves_controller_binding_before_dashboard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import upload_ur_tp_package as upload_module

    helper = tmp_path / "controller-helper.py"
    attested_digest = "a" * 64
    binding = {
        "owner_id": "ur10e-controller-access",
        "path": str(helper),
        "sha256": attested_digest,
    }

    def invoke(
        run_name: str,
        *,
        controller_helper: Path | None = None,
        controller_helper_sha256: str | None = None,
    ) -> list[str]:
        events: list[str] = []

        def observe(_host: str) -> dict[str, str]:
            events.append("dashboard")
            raise RuntimeError("dashboard reached")

        with pytest.raises(RuntimeError):
            prepare_module.create_live_run(
                tmp_path / run_name,
                robot_host="fake-robot",
                kunwei_host="fake-kunwei",
                kunwei_port=5152,
                controller_readback_dir=tmp_path / f"{run_name}-readback",
                r012_ledger=tmp_path / f"{run_name}-r012.jsonl",
                root=ROOT,
                preparation_role=MANUAL_CANARY_ROLE,
                dashboard_observer=observe,
                controller_helper=controller_helper,
                controller_helper_sha256=controller_helper_sha256,
            )
        return events

    with monkeypatch.context() as isolated:
        isolated.setattr(upload_module, "_verified_owner_dependency", lambda _name: binding)
        assert invoke("auto") == ["dashboard"]
        assert invoke(
            "exact",
            controller_helper=helper,
            controller_helper_sha256=attested_digest,
        ) == ["dashboard"]
        assert invoke("path-only", controller_helper=helper) == []
        assert invoke(
            "mismatch",
            controller_helper=tmp_path / "other-helper.py",
            controller_helper_sha256=attested_digest,
        ) == []


def test_explicit_anchor_retest_plan_retries_until_admitted_then_resumes_bo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "anchor-retest.jsonl"
    campaign = Campaign.create(
        path,
        campaign_id="r013-anchor-retest",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    for _index in range(MIN_EXACT_ROWS_FOR_BO):
        dispatch, proposal = campaign.ask()
        assert proposal is None
        campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=1.0),
            anti_windup_metrics=_trial_metrics(),
        )

    assert campaign.anchor_retest_summary["installed"] is False
    campaign.install_anchor_retest_plan(anchor_retest_plan())
    first, proposal = campaign.ask()
    assert proposal is None
    assert first.kind == ANCHOR_RETEST_KIND
    assert first.abort_allowed is False
    assert physical_candidate_key(first.candidate) == physical_candidate_key(
        ANCHOR_RETEST_CANDIDATES[0]
    )
    campaign.tell_exact(
        admission=_admission(first, timing_gate=False, sealed_mae_n=0.2),
        anti_windup_metrics=_trial_metrics(),
    )
    retry, proposal = campaign.ask()
    assert proposal is None
    assert retry.kind == ANCHOR_RETEST_KIND
    assert physical_candidate_key(retry.candidate) == physical_candidate_key(first.candidate)
    campaign.tell_exact(
        admission=_admission(retry, sealed_mae_n=0.7),
        anti_windup_metrics=_trial_metrics(),
    )
    for expected in ANCHOR_RETEST_CANDIDATES[1:]:
        anchor, proposal = campaign.ask()
        assert proposal is None
        assert anchor.kind == ANCHOR_RETEST_KIND
        assert physical_candidate_key(anchor.candidate) == physical_candidate_key(expected)
        campaign.tell_exact(
            admission=_admission(anchor, sealed_mae_n=0.7),
            anti_windup_metrics=_trial_metrics(),
        )

    resumed = Campaign.resume(path)
    assert resumed.anchor_retest_summary == {
        "installed": True,
        "schema": "step5d.autotune-v4/r013-anchor-retest-plan-v1",
        "planned_count": 3,
        "admitted_count": 3,
        "complete": True,
        "historical_objectives_imported": False,
    }
    assert resumed.bo_trial_count == 0
    monkeypatch.setattr(
        campaign_module,
        "fit_production_gp",
        lambda observations, *, config: SimpleNamespace(
            fit_receipt={"schema": "test-r013-fit-receipt-v1"}
        ),
    )
    monkeypatch.setattr(
        campaign_module,
        "ask_qlognei",
        lambda _fit, pool, *, evaluated_keys: SimpleNamespace(candidate=pool[0]),
    )
    bo, proposal = resumed.ask()
    assert proposal is not None
    assert bo.kind == "BO_TRIAL"


def test_local_refinement_plan_retries_then_returns_to_same_bo_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "local-refinement.jsonl"
    campaign = Campaign.create(
        path,
        campaign_id="r013-local-refinement",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    for _index in range(MIN_EXACT_ROWS_FOR_BO):
        dispatch, _proposal = campaign.ask()
        campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=1.0),
            anti_windup_metrics=_trial_metrics(),
        )
    bo_before = campaign.bo_trial_count
    campaign.install_local_refinement_plan(local_refinement_plan())
    first, proposal = campaign.ask()
    assert proposal is None
    assert first.kind == LOCAL_REFINEMENT_KIND
    assert first.abort_allowed is False
    assert physical_candidate_key(first.candidate) == physical_candidate_key(
        LOCAL_REFINEMENT_CANDIDATES[0]
    )
    campaign.tell_exact(
        admission=_admission(first, timing_gate=False, sealed_mae_n=0.2),
        anti_windup_metrics=_trial_metrics(),
    )
    retry, proposal = campaign.ask()
    assert proposal is None
    assert retry.kind == LOCAL_REFINEMENT_KIND
    assert physical_candidate_key(retry.candidate) == physical_candidate_key(first.candidate)
    campaign.tell_exact(
        admission=_admission(retry, sealed_mae_n=0.7),
        anti_windup_metrics=_trial_metrics(),
    )
    resumed = Campaign.resume(path)
    assert resumed.bo_trial_count == bo_before
    assert resumed.local_refinement_summary["admitted_count"] == 1
    for expected in LOCAL_REFINEMENT_CANDIDATES[1:]:
        dispatch, proposal = resumed.ask()
        assert proposal is None
        assert dispatch.kind == LOCAL_REFINEMENT_KIND
        assert physical_candidate_key(dispatch.candidate) == physical_candidate_key(expected)
        resumed.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=0.7),
            anti_windup_metrics=_trial_metrics(),
        )
    assert resumed.local_refinement_summary["complete"] is True
    monkeypatch.setattr(campaign_module, "fit_production_gp", lambda *_args, **_kwargs: SimpleNamespace(fit_receipt={}))
    monkeypatch.setattr(
        campaign_module,
        "ask_qlognei",
        lambda *_args, **_kwargs: SimpleNamespace(candidate=candidate_pool(round_index=bo_before, evaluated_keys=resumed.evaluated_keys)[0]),
    )
    next_dispatch, proposal = resumed.ask()
    assert next_dispatch.kind == "BO_TRIAL"
    assert proposal is not None


def test_local_refinement_requires_fresh_post_plan_kind_matched_observation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "local-refinement-boundary.jsonl"
    campaign = Campaign.create(
        path,
        campaign_id="r013-local-refinement-boundary",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    for _index in range(MIN_EXACT_ROWS_FOR_BO):
        dispatch, _proposal = campaign.ask()
        campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=1.0),
            anti_windup_metrics=_trial_metrics(),
        )
    historical = campaign._persist_dispatch(  # noqa: SLF001 - ledger contract test
        LOCAL_REFINEMENT_CANDIDATES[0],
        kind="BO_TRIAL",
        abort_allowed=True,
    )
    campaign.tell_exact(
        admission=_admission(historical, sealed_mae_n=0.7),
        anti_windup_metrics=_trial_metrics(),
    )

    campaign.install_local_refinement_plan(local_refinement_plan())

    assert campaign.local_refinement_summary["admitted_count"] == 0
    first, proposal = campaign.ask()
    assert proposal is None
    assert first.kind == LOCAL_REFINEMENT_KIND
    assert physical_candidate_key(first.candidate) == physical_candidate_key(
        LOCAL_REFINEMENT_CANDIDATES[0]
    )


def test_replay_rejects_local_refinement_plan_with_in_flight_dispatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "local-refinement-in-flight.jsonl"
    campaign = Campaign.create(
        path,
        campaign_id="r013-local-refinement-in-flight",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    campaign.ask()
    campaign.ledger.append("local_refinement_plan", local_refinement_plan())

    with pytest.raises(R013CampaignError, match="in-flight dispatch"):
        Campaign.resume(path)


def test_replay_rejects_local_refinement_dispatch_without_plan(
    tmp_path: Path,
) -> None:
    path = tmp_path / "local-refinement-no-plan.jsonl"
    campaign = Campaign.create(
        path,
        campaign_id="r013-local-refinement-no-plan",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    campaign._persist_dispatch(  # noqa: SLF001 - malformed replay fixture
        LOCAL_REFINEMENT_CANDIDATES[0],
        kind=LOCAL_REFINEMENT_KIND,
        abort_allowed=False,
    )

    with pytest.raises(R013CampaignError, match="precedes its plan"):
        Campaign.resume(path)


def test_replay_binds_observation_kind_to_dispatch_kind(
    tmp_path: Path,
) -> None:
    path = tmp_path / "local-refinement-forged-kind.jsonl"
    campaign = Campaign.create(
        path,
        campaign_id="r013-local-refinement-forged-kind",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    campaign.install_local_refinement_plan(local_refinement_plan())
    dispatch = campaign._persist_dispatch(  # noqa: SLF001 - malformed replay fixture
        LOCAL_REFINEMENT_CANDIDATES[0],
        kind="BO_TRIAL",
        abort_allowed=True,
    )
    forged = campaign_module.exact_observation(
        dispatch,
        admission=_admission(dispatch, sealed_mae_n=0.7),
        observation_variance_n2=campaign.gp_config.noise_floor_n2,
        anti_windup_metrics=_trial_metrics(),
    )
    forged["kind"] = LOCAL_REFINEMENT_KIND
    campaign.ledger.append("observation", forged)

    with pytest.raises(R013CampaignError, match="observation kind differs"):
        Campaign.resume(path)


def test_high_ki_probe_is_bounded_logged_retriable_and_bo_neutral(
    tmp_path: Path,
) -> None:
    path = tmp_path / "high-ki-probe.jsonl"
    campaign = Campaign.create(
        path,
        campaign_id="r013-high-ki-probe",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    for _index in range(MIN_EXACT_ROWS_FOR_BO):
        dispatch, _proposal = campaign.ask()
        campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=1.0),
            anti_windup_metrics=_trial_metrics(),
        )
    bo_before = campaign.bo_trial_count
    plan = high_ki_probe_plan()
    assert plan["extended_ki_max"] == EXTENDED_KI_MAX
    assert max(candidate["force_i_gain"] for candidate in HIGH_KI_PROBE_CANDIDATES) == EXTENDED_KI_MAX
    assert all(
        candidate["force_i_gain"] <= 0.5 * candidate["force_p_gain"]
        for candidate in HIGH_KI_PROBE_CANDIDATES
    )
    campaign.install_high_ki_probe_plan(plan)

    first, proposal = campaign.ask()
    assert proposal is None
    assert first.kind == HIGH_KI_PROBE_KIND
    assert first.abort_allowed is False
    campaign.tell_exact(
        admission=_admission(first, timing_gate=False, sealed_mae_n=0.2),
        anti_windup_metrics=_trial_metrics(),
    )
    retry, proposal = campaign.ask()
    assert proposal is None
    assert retry.kind == HIGH_KI_PROBE_KIND
    assert physical_candidate_key(retry.candidate) == physical_candidate_key(first.candidate)
    campaign.tell_exact(
        admission=_admission(retry, sealed_mae_n=0.7),
        anti_windup_metrics=_trial_metrics(),
    )

    resumed = Campaign.resume(path)
    assert resumed.high_ki_probe_summary["admitted_count"] == 1
    assert resumed.bo_trial_count == bo_before


def test_high_ki_probe_plan_rejects_noncanonical_json_types_and_fields(
    tmp_path: Path,
) -> None:
    variants = []
    for field, value in (
        ("version", True),
        ("version", 1.0),
        ("historical_objectives_imported", 0),
    ):
        plan = high_ki_probe_plan()
        plan[field] = value
        variants.append(plan)
    extra_field = high_ki_probe_plan()
    extra_field["candidates"][0]["unexpected"] = "forbidden"
    variants.append(extra_field)
    numeric_string = high_ki_probe_plan()
    numeric_string["candidates"][0]["force_i_gain"] = str(
        numeric_string["candidates"][0]["force_i_gain"]
    )
    variants.append(numeric_string)

    for index, plan in enumerate(variants):
        campaign = Campaign.create(
            tmp_path / f"high-ki-noncanonical-{index}.jsonl",
            campaign_id=f"r013-high-ki-noncanonical-{index}",
            run_id="offline",
            attempt_id="a1",
            noise_floor_n2=0.023,
            r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
        )
        with pytest.raises(R013CampaignError, match="high-Ki probe"):
            campaign.install_high_ki_probe_plan(plan)


def test_normal_velocity_gain_probe_is_matched_retriable_and_bo_neutral(
    tmp_path: Path,
) -> None:
    path = tmp_path / "normal-velocity-probe.jsonl"
    campaign = Campaign.create(
        path,
        campaign_id="r013-normal-velocity-probe",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    for _index in range(MIN_EXACT_ROWS_FOR_BO):
        dispatch, _proposal = campaign.ask()
        campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=1.0),
            anti_windup_metrics=_trial_metrics(),
        )
    bo_before = campaign.bo_trial_count
    matched_i_over_d = [
        candidate["force_i_gain"] / candidate["force_damping"]
        for candidate in NORMAL_VELOCITY_GAIN_PROBE_CANDIDATES[:3]
    ]
    assert max(matched_i_over_d) - min(matched_i_over_d) < 1e-15
    campaign.install_normal_velocity_gain_probe_plan(normal_velocity_gain_probe_plan())

    first, proposal = campaign.ask()
    assert proposal is None
    assert first.kind == NORMAL_VELOCITY_GAIN_PROBE_KIND
    assert first.abort_allowed is False
    campaign.tell_exact(
        admission=_admission(first, timing_gate=False, sealed_mae_n=0.2),
        anti_windup_metrics=_trial_metrics(),
    )
    retry, proposal = campaign.ask()
    assert proposal is None
    assert retry.kind == NORMAL_VELOCITY_GAIN_PROBE_KIND
    assert physical_candidate_key(retry.candidate) == physical_candidate_key(first.candidate)
    campaign.tell_exact(
        admission=_admission(retry, sealed_mae_n=0.7),
        anti_windup_metrics=_trial_metrics(),
    )

    resumed = Campaign.resume(path)
    assert resumed.normal_velocity_gain_probe_summary["admitted_count"] == 1
    assert resumed.bo_trial_count == bo_before


def test_normal_velocity_gain_probe_plan_rejects_noncanonical_values(
    tmp_path: Path,
) -> None:
    plan = normal_velocity_gain_probe_plan()
    plan["version"] = True
    campaign = Campaign.create(
        tmp_path / "normal-velocity-noncanonical.jsonl",
        campaign_id="r013-normal-velocity-noncanonical",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    with pytest.raises(R013CampaignError, match="normal-velocity probe"):
        campaign.install_normal_velocity_gain_probe_plan(plan)


def test_normal_velocity_probe_stop_is_ledger_derived_and_returns_to_bo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "normal-velocity-stop.jsonl"
    campaign = Campaign.create(
        path,
        campaign_id="r013-normal-velocity-stop",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    for _index in range(MIN_EXACT_ROWS_FOR_BO):
        dispatch, _proposal = campaign.ask()
        campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=1.0),
            anti_windup_metrics=_trial_metrics(),
        )
    campaign.install_normal_velocity_gain_probe_plan(normal_velocity_gain_probe_plan())
    with pytest.raises(R013CampaignError, match="requires two admitted rows"):
        campaign.stop_normal_velocity_gain_probe()
    for objective in (0.7, 1.3):
        dispatch, _proposal = campaign.ask()
        campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=objective),
            anti_windup_metrics=_trial_metrics(),
        )
    stop = campaign.stop_normal_velocity_gain_probe()
    assert stop["admitted_count_at_stop"] == 2
    assert stop["minimum_last_minus_first_n"] == 0.5

    resumed = Campaign.resume(path)
    assert resumed.normal_velocity_gain_probe_summary["stopped"] is True
    assert resumed.normal_velocity_gain_probe_summary["complete"] is False
    monkeypatch.setattr(
        campaign_module,
        "fit_production_gp",
        lambda *_args, **_kwargs: SimpleNamespace(fit_receipt={}),
    )
    monkeypatch.setattr(
        campaign_module,
        "ask_qlognei",
        lambda *_args, **_kwargs: SimpleNamespace(
            candidate=candidate_pool(
                round_index=resumed.bo_trial_count,
                evaluated_keys=resumed.evaluated_keys,
            )[0]
        ),
    )
    dispatch, proposal = resumed.ask()
    assert dispatch.kind == "BO_TRIAL"
    assert proposal is not None


def test_normal_velocity_probe_stop_replay_rejects_nested_loose_json_types(
    tmp_path: Path,
) -> None:
    path = tmp_path / "normal-velocity-stop-nested-types.jsonl"
    campaign = Campaign.create(
        path,
        campaign_id="r013-normal-velocity-stop-nested-types",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    for _index in range(MIN_EXACT_ROWS_FOR_BO):
        dispatch, _proposal = campaign.ask()
        campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=1.0),
            anti_windup_metrics=_trial_metrics(),
        )
    campaign.install_normal_velocity_gain_probe_plan(normal_velocity_gain_probe_plan())
    for objective in (1.0, 2.0):
        dispatch, _proposal = campaign.ask()
        campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=objective),
            anti_windup_metrics=_trial_metrics(),
        )
    campaign.stop_normal_velocity_gain_probe()
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    records[-1]["payload"]["admitted_objective_n"][0] = True
    path.write_text(
        "\n".join(json.dumps(record, separators=(",", ":")) for record in records) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(R013CampaignError, match="stop differs"):
        Campaign.resume(path)


def test_lower_p_over_d_probe_is_matched_retriable_resumable_and_bo_neutral(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lower-p-over-d-probe.jsonl"
    campaign = Campaign.create(
        path,
        campaign_id="r013-lower-p-over-d-probe",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    for _index in range(MIN_EXACT_ROWS_FOR_BO):
        dispatch, _proposal = campaign.ask()
        campaign.tell_exact(
            admission=_admission(dispatch, sealed_mae_n=1.0),
            anti_windup_metrics=_trial_metrics(),
        )
    bo_before = campaign.bo_trial_count
    matched_i_over_d = [
        candidate["force_i_gain"] / candidate["force_damping"]
        for candidate in LOWER_P_OVER_D_PROBE_CANDIDATES[:2]
    ]
    assert max(matched_i_over_d) - min(matched_i_over_d) < 1e-15
    p_over_d = [
        candidate["force_p_gain"] / candidate["force_damping"]
        for candidate in LOWER_P_OVER_D_PROBE_CANDIDATES[:2]
    ]
    assert p_over_d[1] < p_over_d[0]
    assert all(
        0.0 <= coordinate <= 1.0
        for candidate in LOWER_P_OVER_D_PROBE_CANDIDATES
        for coordinate in candidate_to_normalized(candidate)
    )
    campaign.install_lower_p_over_d_probe_plan(lower_p_over_d_probe_plan())

    first, proposal = campaign.ask()
    assert proposal is None
    assert first.kind == LOWER_P_OVER_D_PROBE_KIND
    assert first.abort_allowed is False
    campaign.tell_exact(
        admission=_admission(first, timing_gate=False, sealed_mae_n=0.2),
        anti_windup_metrics=_trial_metrics(),
    )
    retry, proposal = campaign.ask()
    assert proposal is None
    assert retry.kind == LOWER_P_OVER_D_PROBE_KIND
    assert physical_candidate_key(retry.candidate) == physical_candidate_key(first.candidate)
    campaign.tell_exact(
        admission=_admission(retry, sealed_mae_n=0.6),
        anti_windup_metrics=_trial_metrics(),
    )

    resumed = Campaign.resume(path)
    assert resumed.lower_p_over_d_probe_summary["admitted_count"] == 1
    assert resumed.bo_trial_count == bo_before
    second, proposal = resumed.ask()
    assert proposal is None
    assert second.kind == LOWER_P_OVER_D_PROBE_KIND
    assert physical_candidate_key(second.candidate) == physical_candidate_key(
        LOWER_P_OVER_D_PROBE_CANDIDATES[1]
    )


def test_lower_p_over_d_probe_plan_rejects_noncanonical_values(tmp_path: Path) -> None:
    plan = lower_p_over_d_probe_plan()
    plan["version"] = True
    campaign = Campaign.create(
        tmp_path / "lower-p-over-d-noncanonical.jsonl",
        campaign_id="r013-lower-p-over-d-noncanonical",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.023,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    with pytest.raises(R013CampaignError, match="lower-P/D probe"):
        campaign.install_lower_p_over_d_probe_plan(plan)


def test_noise_config_has_no_python_default() -> None:
    with pytest.raises(TypeError):
        ProductionGPConfig()  # type: ignore[call-arg]
    with pytest.raises(R013GPError, match="lacks noise"):
        ProductionGPConfig.from_snapshot(
            {"schema": "step5d.autotune-v4/r013-optimizer-snapshot-v1"}
        )


def test_runtime_binding_explicitly_enables_fixed_r013_contract() -> None:
    candidate = {**SEED, "force_i_gain": FIXED_KI_SEEDS[0], "i_off": False}
    binding = BridgeBinding.from_candidate(candidate)
    payload = binding.as_dict()
    assert validate_bridge_binding(payload) == binding
    argv = binding.bridge_argv()
    assert argv[:4] == (
        "--bridge-integral-policy", "conditional-double-clamp-v1",
        "--bridge-integral-limit-n-s", "1.0",
    )
    assert all(enabled is True for enabled in payload["activation"].values())
    assert payload["activation_boundary"] == "live_owner_receipts_required"
    assert "--step5d-autotune-normal-filter-tau" in argv

    profile = json.loads(
        (ROOT / "config/step5/step5d_autotune_v4_r013_launch_profile.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = profile["r013_runtime"]
    assert tuple(runtime["soft_tube"]["semi_axes_m"]) == R012_SOFT_CBF_AXES_M
    assert tuple(runtime["hard_tube"]["axes_m"]) == R012_HARD_TUBE_AXES_M
    assert runtime["hard_tube"]["enabled"] is True
    assert runtime["hard_tube"]["independent"] is True
    assert runtime["hard_tube"]["checked_first"] is True

    class FakeInjection:
        def prepare_control(self, **_kwargs: object) -> object:
            return SimpleNamespace()

    writer = SimpleNamespace(injection=FakeInjection())
    stack = _bind_r012_guard_stack(writer)
    assert isinstance(stack, R012PathGuardStack)
    assert stack.soft_filter.config.tightened_axes_m == pytest.approx(R012_SOFT_CBF_AXES_M)
    assert stack.soft_filter.config.ellipse_axes_m == pytest.approx(R012_HARD_TUBE_AXES_M)
    control = writer.injection.prepare_control()
    assert control._r012_guard_stack is stack

    owner_source = getsource(build_r013_live_context)
    bind_call = "r012_guard_stack = _bind_r012_guard_stack(mature)"
    assert bind_call in owner_source
    assert owner_source.index(bind_call) < owner_source.index("adapter.open")
    assert owner_source.index(
        "_require_r012_guard_stack(mature, r012_guard_stack)"
    ) < owner_source.index("runtime.arm(attempt)")
    assert owner_source.index("state25_trace.begin_attempt(sequence)") < owner_source.index(
        "runtime.dispatch(attempt, dispatch)"
    )
    assert "runtime.sync_qualification_passes(3)" not in owner_source
    assert "physical_ledger.fresh_process_verify()" in owner_source
    assert "record.qualification_eligible" in getsource(_epoch_qualification_records)
    assert owner_source.index("physical_ledger.fresh_process_verify()") < owner_source.index(
        "runtime.sync_qualification_passes(len(qualification_records))"
    )


def test_fresh_prepare_ordering_receipts_stop_no_arm_and_synthetic_ready_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import upload_ur_tp_package as upload_module

    readback_dir = tmp_path / "controller-readback"
    readback_dir.mkdir()
    # The generator owns the current R013 VERSION stamp; use this freshly
    # generated local triplet for the synthetic read-back fixture rather than
    # a historical package whose stamp is intentionally stale.
    source_readback = ROOT / "programs/step5/step5d"
    for role in ("script", "txt", "urp"):
        shutil.copyfile(
            source_readback / f"step5d_strict_rnn_autotune_v4_r013.{role}",
            readback_dir / f"step5d_strict_rnn_autotune_v4_r013.{role}",
        )

    seed_source = {
        "schema": "step5d.autotune-v4/r013-r012-seed-source-v1",
        "source_ledger": "offline-test",
        "confirmed_incumbent": {
            "candidate": dict(SEED),
            "confirmed": True,
            "exact_count": 3,
            "arithmetic_mean_sealed_mae_n": 0.8,
        },
        "confirmed_candidates": [],
        "observation_rows_exported_to_r013": 0,
    }
    monkeypatch.setattr(prepare_module, "r012_seed_source_from_ledger", lambda _path: seed_source)

    def run_once(
        run_dir: Path,
        events: list[str],
        runtime_observer: Callable[..., object],
        *,
        resume_existing: bool = False,
        runtime_strategy_profile: Path | None = None,
        campaign_config: Path | None = None,
        feedforward_mode: str = "on",
        preparation_role: str = "formal",
        production_readback: bool = False,
        controller_helper: Path | None = None,
        controller_helper_sha256: str | None = None,
    ) -> dict[str, object]:
        state = {
            "target": "/programs/old.urp",
            "running": True,
            "program_state": "PLAYING",
        }

        def observe(_host: str) -> dict[str, str]:
            events.append("dashboard")
            return {
                "is in remote control": "true",
                "safetymode": "Safetymode: NORMAL",
                "robotmode": "Robotmode: RUNNING",
                "running": f"Program running: {str(state['running']).lower()}",
                "programState": str(state["program_state"]),
                "get loaded program": f"Loaded program: {state['target']}",
            }

        class FakeWriter:
            def __init__(self, _host: str, *, load_target: str, timeout_s: float) -> None:
                self.load_target = load_target
                self.timeout_s = timeout_s

            def write(self, command: str) -> object:
                events.append(f"write:{self.load_target}:{command}")
                if command == "stop":
                    state["running"] = False
                    state["program_state"] = "STOPPED"
                elif command.startswith("load "):
                    state["target"] = command.removeprefix("load ")
                    state["running"] = False
                    state["program_state"] = "STOPPED"
                elif command == "play":
                    if self.load_target == prepare_module.R013_TARGET:
                        state["running"] = True
                        state["program_state"] = "PLAYING"
                    else:
                        state["running"] = False
                        state["program_state"] = "STOPPED"
                else:
                    raise AssertionError(f"unexpected Dashboard command: {command}")
                return SimpleNamespace(command_sent=True, response=f"ok:{command}")

        def home(_host: str) -> dict[str, object]:
            events.append("home")
            return {
                "observed_at_s": 101.0,
                "actual_TCP_pose": list(SCRIPT1_TARGET_POSE),
                "actual_TCP_speed": [0.0] * 6,
                "actual_q": [0.1] * 6,
                "actual_qd": [0.0] * 6,
                "safety_mode": 1,
                "runtime_state": "STOPPED",
            }

        def baseline(_host: str, _port: int) -> dict[str, object]:
            events.append("baseline")
            return {
                "schema": "step5d.autotune-v4/r005-software-baseline-v1",
                "observation_source": "fresh_kunwei_live_stream",
                "observed_at_s": 103.0,
                "capture_duration_s": 1.2,
                "sample_count": 900,
                "mean_wrench_n_nm": [0.1] * 6,
                "stdev_wrench_n_nm": [0.01] * 6,
                "parse_errors": 0,
                "dropped_bytes": 0,
                "zero_tare_config_write": False,
            }

        def readback(root: Path, directory: Path, *, observed_at_s: float) -> dict[str, object]:
            events.append("readback")
            return prepare_module._read_controller_triplet(
                root, directory, observed_at_s=observed_at_s
            )

        def neutral_hold(_host: str) -> dict[str, object]:
            events.append("neutral_hold")
            return {
                "schema": "step5d.autotune-v4/r013-neutral-hold-v1",
                "observation_source": "fresh_rtde_input_write",
                "observed_at_s": 103.5,
                "layout_tag": 606.0,
                "session_command": "HOLD",
                "session_command_value": 0,
                "arm_dispatched": False,
                "trial_dispatched": False,
            }

        runtime_observation_count = 0

        def stateful_runtime(*args: object, **kwargs: object) -> object:
            nonlocal runtime_observation_count
            events.append("runtime")
            result = runtime_observer(**kwargs)
            runtime_observation_count += 1
            if hasattr(result, "observed_at_s"):
                result.observed_at_s = float(result.observed_at_s) + 0.1 * (
                    runtime_observation_count - 1
                )
            return result

        return prepare_module.create_live_run(
            run_dir,
            robot_host="fake-robot",
            kunwei_host="fake-kunwei",
            kunwei_port=5152,
            controller_readback_dir=readback_dir,
            r012_ledger=tmp_path / "offline-r012.jsonl",
            root=ROOT,
            dashboard_observer=observe,
            dashboard_writer_factory=FakeWriter,
            state_sampler=home,
            baseline_capture=baseline,
            neutral_hold_publisher=neutral_hold,
            readback_loader=None if production_readback else readback,
            controller_helper=controller_helper,
            controller_helper_sha256=controller_helper_sha256,
            runtime_observer=stateful_runtime,
            sleeper=lambda _seconds: None,
            now=lambda: 102.0,
            resume_existing=resume_existing,
            runtime_strategy_profile=runtime_strategy_profile,
            campaign_config=campaign_config,
            feedforward_mode=feedforward_mode,
            preparation_role=preparation_role,
        )

    def unreachable_runtime(**_kwargs: object) -> object:
        raise AssertionError("R013 offline readiness gate allowed an external callback")

    handoff_plan = HandoffABPlanV1()
    handoff_evidence = {
        "sealed": True,
        "gaps": [],
        "fmin_n": 0.0,
        "fmax_n": 5.0,
        "pose_error_m": 0.0,
        "orientation_error_rad": 0.0,
        "carry_reset_receipt_ids": ["carry-test"],
        "tmae5_n": 1.0,
        "settling_time_s": 1.0,
        "raw_evidence_refs": ["raw-test"],
    }
    for _ in range(8):
        arm = handoff_plan.next_arm()
        assert arm is not None
        row = dict(handoff_evidence)
        row["sealed_mae_n"] = 1.0 if arm == "A" else 1.03
        handoff_plan.record_evidence(arm, row)
    offline_config = materialize_r013_handoff_selection(
        load_r013_budgeted_floor_config(), handoff_plan
    )
    materialized_config_path = tmp_path / "handoff-materialized-offline-r013-config.json"
    prepare_module._write_json(materialized_config_path, offline_config.as_dict())

    blocked_cases = (
        ("default-template-blocked", None),
        ("handoff-materialized-blocked", materialized_config_path),
    )
    for name, campaign_config in blocked_cases:
        blocked_run_dir = tmp_path / name
        blocked_events: list[str] = []
        with pytest.raises(RuntimeError, match="launch_ready=false"):
            run_once(
                blocked_run_dir,
                blocked_events,
                unreachable_runtime,
                campaign_config=campaign_config,
            )
        assert blocked_events == []
        assert not blocked_run_dir.exists()

    def canary_runtime(**_kwargs: object) -> object:
        return SimpleNamespace(
            observed_at_s=104.0,
            integer_echoes={32: 606006, 33: 13, 34: 613013, 26: 78},
            safety_normal=True,
            stationary=True,
            program_running=True,
            tcp_pose_m_rad=[0.0] * 6,
            q_rad=[0.1] * 6,
            tcp_speed_m_s_rad_s=[0.0] * 6,
            payload_kg=0.0,
            payload_cog_m=[0.0] * 3,
            tcp_offset_m_rad=[0.0] * 6,
        )

    canary_run_dir = tmp_path / "manual-canary-off"
    canary_events: list[str] = []

    def reject_live_resolver(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("injected readback must skip live helper resolution")

    with monkeypatch.context() as isolated:
        isolated.setattr(
            upload_module,
            "resolve_live_controller_helper",
            reject_live_resolver,
        )
        canary_preparation = run_once(
            canary_run_dir,
            canary_events,
            canary_runtime,
            feedforward_mode="off",
            preparation_role=MANUAL_CANARY_ROLE,
        )
    canary_campaign = Campaign.resume(canary_run_dir / "r013_ledger.jsonl")
    canary_role = R013ManualCanaryPreparationProfileV1.from_value("off")
    assert canary_campaign.manual_canary is True
    assert canary_campaign.snapshot["campaign_role"] == canary_role.as_dict()
    assert canary_campaign.snapshot["formal_campaign_tell_exact"] is False
    assert canary_campaign.snapshot["launch_ready"] is False
    assert canary_campaign.completion_policy.policy != BUDGETED_FLOOR_V1
    assert canary_campaign.observations == []
    assert canary_campaign.rejected_admissions == []
    assert canary_campaign.campaign_fingerprint.feedforward_profile_identity == (
        canary_role.feedforward_profile.profile_id
    )
    assert canary_campaign.campaign_fingerprint.motion_admission_profile_identity != (
        "legacy-v4-default"
    )
    assert canary_preparation["preparation_role"] == MANUAL_CANARY_ROLE
    assert canary_preparation["formal_campaign_tell_exact"] is False
    assert canary_preparation["launch_ready"] is False
    assert canary_preparation["budgeted_floor_config"] is None
    assert canary_events
    with pytest.raises(R013CampaignError, match="cannot enter optimizer ask/tell"):
        canary_campaign.ask()

    drift_helper = tmp_path / "drift-controller-helper.py"
    drift_digest = "b" * 64
    drift_events: list[str] = []
    binding_calls: list[tuple[Path | None, str | None]] = []

    def resolve_with_pointer_drift(
        requested_helper: Path | None,
        requested_sha256: str | None,
    ) -> tuple[Path, str]:
        binding_calls.append((requested_helper, requested_sha256))
        drift_events.append("resolve")
        if len(binding_calls) == 1:
            return drift_helper, drift_digest
        raise RuntimeError("controller helper binding changed during preparation")

    with monkeypatch.context() as isolated:
        isolated.setattr(
            upload_module,
            "resolve_live_controller_helper",
            resolve_with_pointer_drift,
        )
        with pytest.raises(RuntimeError, match="binding changed during preparation"):
            run_once(
                tmp_path / "pointer-drift",
                drift_events,
                canary_runtime,
                preparation_role=MANUAL_CANARY_ROLE,
                production_readback=True,
            )
    assert binding_calls == [(None, None), (drift_helper, drift_digest)]
    assert drift_events[0] == "resolve"
    assert "readback" not in drift_events

    mismatch_events: list[str] = []
    with pytest.raises(RuntimeError, match="role or mode differs"):
        run_once(
            canary_run_dir,
            mismatch_events,
            unreachable_runtime,
            resume_existing=True,
            feedforward_mode="on",
            preparation_role=MANUAL_CANARY_ROLE,
        )
    assert mismatch_events == []

    synthetic = tmp_path / "synthetic-only"
    synthetic.mkdir()
    triplet = {role: "a" * 64 for role in ("script", "txt", "urp")}
    ready = {
        "status": "resident_ready_no_arm",
        "program": prepare_module.R013_PROGRAM,
        "controller_target": prepare_module.R013_TARGET,
        "route_id": "route",
        "attempt_id": "attempt",
            "resident_session_id": "session",
            "session_epoch": 1,
            "runtime_protocol": 606006,
            "triplet": triplet,
        "resident_ready_evidence": str(synthetic / "resident_ready_evidence.json"),
    }
    launch = {
        "campaign_id": "campaign",
        "run_id": "run",
        "attempt_id": "attempt",
        "contract_sha256": prepare_module._r013_identity()[1],
        "campaign_fingerprint": prepare_module._r013_identity()[2],
        "route_id": "route",
        "session_id": "session",
        "session_epoch": 1,
        "program": prepare_module.R013_PROGRAM,
        "triplet": triplet,
    }
    (synthetic / "r013_live_owner_ready.json").write_text(json.dumps(ready))
    (synthetic / "launch_context.json").write_text(json.dumps(launch))
    (synthetic / "resident_ready_evidence.json").write_text(
        json.dumps(
            {
                "schema": "step5d.autotune-v4/r013-resident-ready-evidence-v2",
                "status": "resident_ready_no_arm",
                "observation_source": "synthetic_projection_only",
            }
        )
    )
    with pytest.raises(R013OwnerError, match="fresh RTDE observation"):
        build_r013_live_context(
            run_dir=synthetic,
            controller_host="offline-controller",
            kunwei_host="offline-kunwei",
            kunwei_port=5152,
            launch_profile=tmp_path / "unused-launch-profile.json",
            campaign_id="campaign",
            run_id="run",
            attempt_id="attempt",
        )


def test_gp_rejects_exact_row_without_valid_anti_windup_evidence() -> None:
    candidate = {**SEED, "force_i_gain": FIXED_KI_SEEDS[0], "i_off": False}
    row = {
        "schema": "step5d.autotune-v4/r013-exact-observation-v1",
        "candidate": candidate,
        "objective_n": 1.0,
        "observation_variance_n2": 0.01,
        "completed": True,
        "sealed": True,
        "full_observation": True,
        "eligible": True,
        "censored": False,
        "anti_windup": {**_trial_metrics(), "invariant_violation_count": 1},
    }
    with pytest.raises(R013GPError, match="anti-windup evidence differs"):
        _exact_row(row)


def test_budgeted_floor_outcome3_coordinator_replay_and_ab_contract(
    tmp_path: Path,
) -> None:
    policy = FloorDiscoveryPolicyV1()

    def fake_core_provider(pool: Sequence[dict[str, Any]]) -> FloorCandidateProposal:
        candidate = pool[0]
        return FloorCandidateProposal(
            candidate=candidate,
            block_input=tuple(candidate_to_log_features(candidate)[index] for index in (0, 1, 2, 5)),
            contract=CORE_PROPOSAL_CONTRACT,
            acquisition_value=0.125,
            posterior_beating_probability=0.3,
            fit_receipt={"backend": "fake-test-provider", "model_dimensions": 4},
        )

    def fake_correction_provider(pool: Sequence[tuple[float, ...]]) -> FloorCandidateProposal:
        return FloorCandidateProposal(
            candidate={},
            block_input=tuple(pool[0]),
            contract=BlockProposalContract("correction", 6),
            acquisition_value=0.25,
            posterior_beating_probability=0.3,
            fit_receipt={"backend": "fake-test-provider", "model_dimensions": 6},
        )

    coordinator = FloorDiscoveryCoordinator(
        policy,
        fingerprint="floor-fingerprint-a",
        frozen_incumbent_n=1.1,
        core_proposal_provider=fake_core_provider,
        correction_proposal_provider=fake_correction_provider,
    )
    requested_roles: list[str] = []
    checkpoint_counts = {99, 100, 199}
    while not coordinator.complete:
        request = coordinator.next_request()
        requested_roles.append(request.role)
        assert request.trial.canonical_token == request.canonical_token
        if request.role in {CORE_SOBOL_NOVEL, CORE_BO_NOVEL, POLISH_CORE_NOVEL}:
            assert request.proposal_contract.dimension_count == 4
            assert len(request.trial.controller_core.coordinates) == 4
        if request.role in {CORRECTION_SOBOL_NOVEL, CORRECTION_BO_NOVEL, POLISH_CORRECTION_NOVEL}:
            assert request.proposal_contract.dimension_count == 6
            assert len(request.trial.correction.coordinates) == 6
        if request.role == SENTINEL:
            assert request.novel_index % 10 == 0
        posterior = 0.3 if request.novel_index == 4 and request.role == CORE_SOBOL_NOVEL else 0.0
        coordinator.record_result(
            admitted=True,
            sealed_mae_n=0.5 if posterior > 0.0 else 0.9,
            posterior_beating_probability=posterior,
            guardrails_passed=True,
            receipt_id=f"guardrail-{request.request_id}",
        )
        if coordinator.novel_count in checkpoint_counts:
            replayed = FloorDiscoveryCoordinator.from_records(
                policy,
                coordinator.event_log,
                fingerprint="floor-fingerprint-a",
                frozen_incumbent_n=1.1,
                core_proposal_provider=fake_core_provider,
                correction_proposal_provider=fake_correction_provider,
            )
            assert replayed.snapshot()["novel_count"] == coordinator.novel_count
            assert replayed.snapshot()["sobol_state"] == coordinator.snapshot()["sobol_state"]
            assert replayed.candidate_pool() == coordinator.candidate_pool()

    expected_novel_roles = {
        BOUNDARY_NOVEL: 3,
        CORE_SOBOL_NOVEL: 29,
        CORE_BO_NOVEL: 68,
        CORRECTION_SOBOL_NOVEL: 12,
        CORRECTION_BO_NOVEL: 48,
        POLISH_CORE_NOVEL: 20,
        POLISH_CORRECTION_NOVEL: 20,
    }
    assert {
        role: requested_roles.count(role)
        for role in expected_novel_roles
    } == expected_novel_roles
    assert requested_roles.count(SENTINEL) == 20
    assert requested_roles.count(REPEAT) >= 18
    assert coordinator.complete is True
    assert coordinator.boundary_decisions[0]["expanded"] is True
    final_top3 = [row for row in coordinator.stats.values() if row.get("final_top3")]
    assert len(final_top3) == 3
    assert all(len(row["values"]) == 5 for row in final_top3)
    fresh_pool = coordinator.candidate_pool()
    assert len(fresh_pool) == 128
    fresh_keys = {physical_candidate_key(candidate) for candidate in fresh_pool}
    assert len(fresh_keys) == 128
    assert not fresh_keys.intersection(coordinator.runtime_catalog_key_set())

    paused = FloorDiscoveryCoordinator(policy, fingerprint="pause-a")
    for _ in range(3):
        paused.next_request()
        paused.record_result(
            admitted=False,
            complete=False,
            sealed=False,
            failure_signature="same-failure",
        )
    with pytest.raises(FloorCoordinatorError, match="new fingerprint restart"):
        paused.next_request()
    paused.restart_with_new_fingerprint("pause-b")
    assert paused.next_request().role == BOUNDARY_NOVEL

    drift = FloorDiscoveryCoordinator(
        policy,
        fingerprint="drift-a",
        core_proposal_provider=fake_core_provider,
    )
    while len(drift.sentinel_values) < 3:
        request = drift.next_request()
        if request.role == SENTINEL:
            value = 1.0 + 0.2 * len(drift.sentinel_values)
            drift.record_result(admitted=True, sealed_mae_n=value, sigma_pool=0.0)
        else:
            drift.record_result(admitted=True, sealed_mae_n=1.0)
    assert drift.state == "diagnostic_pause"

    handoff = HandoffABPlanV1()
    evidence = {
        "sealed": True,
        "sealed_mae_n": 1.0,
        "gaps": [],
        "fmin_n": 0.0,
        "fmax_n": 5.0,
        "pose_error_m": 0.0,
        "orientation_error_rad": 0.0,
        "carry_reset_receipt_ids": ["carry-reset-1"],
        "tmae5_n": 1.0,
        "settling_time_s": 1.0,
        "raw_evidence_refs": ["sealed-receipt-1"],
    }
    for _ in range(6):
        arm = handoff.next_arm()
        assert arm is not None
        row = dict(evidence)
        row["sealed_mae_n"] = 1.0 if arm == "A" else 1.03
        handoff.record_evidence(arm, row)
    assert handoff.selected_policy is None
    assert handoff.provisional_policy == "blind_reset_v0"
    assert handoff.complete_initial is True
    with pytest.raises(FloorCoordinatorError, match="winner lacks"):
        handoff.materialize_handoff_policy()
    handoff.record_evidence("A", evidence)
    handoff.record_evidence("A", evidence)
    assert handoff.materialize_handoff_policy() == "blind_reset_v0"

    config = load_r013_budgeted_floor_config()
    materialized = replace(
        config.campaign_fingerprint,
        handoff_policy="freeze_carry_v1",
        correction_runtime_strategy_identity="a" * 64,
        source_identity="b" * 64,
        eoat_identity="c" * 64,
        home_tare_identity=(
            "step5d.autotune-v4/r013-home-tare-procedure-v1|version=1|"
            "script1_sha256=" + "d" * 64
        ),
    )
    campaign_path = tmp_path / "floor-campaign.jsonl"
    campaign = Campaign.create(
        campaign_path,
        campaign_id="r013-floor-outcome3",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.005,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
        completion_policy=config.completion_policy,
        handoff_policy="freeze_carry_v1",
        campaign_fingerprint=materialized,
        budgeted_floor_config=config.as_dict(),
    )
    assert campaign.snapshot["floor_discovery_policy"] == config.floor_discovery_policy.as_dict()
    assert campaign.snapshot["floor_discovery_state"]["candidate_pool_size"] == 128
    with pytest.raises(R013CampaignError, match="runtime_primitive_not_installed"):
        campaign.ask()
    assert campaign.dispatches == []
    assert campaign.observations == []
    assert campaign.floor_discovery_status["state"] == "runtime_primitive_not_installed"
    assert campaign.target_achieved is False
    resumed = Campaign.resume(campaign_path)
    assert resumed.floor_discovery_status is not None
    assert resumed.novel_count == 0
    with pytest.raises(R013CampaignError, match="runtime_primitive_not_installed"):
        resumed.ask()

    legacy = Campaign.create(
        tmp_path / "legacy-floor-compat.jsonl",
        campaign_id="r013-legacy-compat",
        run_id="offline",
        attempt_id="a1",
        noise_floor_n2=0.005,
        r012_seed_source={"confirmed_incumbent": {"candidate": SEED, "confirmed": True}},
    )
    assert legacy.ask()[0].kind == "WARM_FIXED_KI"


def test_floor_outward_boundary_is_typed_but_not_projected_or_dispatchable() -> None:
    coordinator = FloorDiscoveryCoordinator(FloorDiscoveryPolicyV1(), fingerprint="boundary")
    request = coordinator.next_request()
    assert request.role == BOUNDARY_NOVEL
    assert request.runtime_candidate is None
    assert request.executable is False
    assert request.trial.boundary_name == "tau"
    assert request.trial.controller_core.log2_tau == pytest.approx(math.log2(0.03094))
    assert request.trial.orientation_ko == pytest.approx(0.05)
    assert request.trial.canonical_token != ""
    coordinator.mark_runtime_primitive_not_installed(request)
    with pytest.raises(RuntimePrimitiveNotInstalled):
        coordinator.next_request()
    assert coordinator.state == "runtime_primitive_not_installed"
    assert not coordinator.runtime_catalog_key_set()


def test_floor_core_provider_receipt_is_four_dimensional_and_no_provider_fails_closed() -> None:
    def provider(pool: Sequence[dict[str, Any]]) -> FloorCandidateProposal:
        candidate = pool[0]
        return FloorCandidateProposal(
            candidate=candidate,
            block_input=tuple(candidate_to_log_features(candidate)[index] for index in (0, 1, 2, 5)),
            contract=CORE_PROPOSAL_CONTRACT,
            acquisition_value=0.75,
            posterior_beating_probability=0.4,
            fit_receipt={
                "backend": "botorch.SingleTaskGP",
                "model_dimensions": 4,
                "acquisition": "qLogNoisyExpectedImprovement",
                "device": "cuda",
                "lengthscales_unit_fitted": [0.2, 0.3, 0.4, 0.5],
            },
        )

    coordinator = FloorDiscoveryCoordinator(FloorDiscoveryPolicyV1(), fingerprint="provider")
    while coordinator.novel_count < 16:
        request = coordinator.next_request()
        if request.role == SENTINEL:
            coordinator.record_result(admitted=True, sealed_mae_n=1.0)
            continue
        coordinator.record_result(admitted=True, sealed_mae_n=1.0)
    with pytest.raises(RuntimePrimitiveNotInstalled, match="core qLogNEI proposal provider"):
        coordinator.next_request()

    supplied = FloorDiscoveryCoordinator(
        FloorDiscoveryPolicyV1(),
        fingerprint="provider-installed",
        core_proposal_provider=provider,
    )
    while supplied.novel_count < 16:
        request = supplied.next_request()
        supplied.record_result(admitted=True, sealed_mae_n=1.0)
    request = supplied.next_request()
    assert request.role == CORE_BO_NOVEL
    assert request.proposal_receipt is not None
    assert request.proposal_receipt["contract"]["dimension_count"] == 4
    assert request.proposal_receipt["acquisition"] == "qLogNEI"
    assert request.proposal_receipt["acquisition_value"] == pytest.approx(0.75)
    assert request.posterior_beating_probability == pytest.approx(0.4)


def test_core_production_gp_has_no_cpu_or_deterministic_qlognei_fallback() -> None:
    try:
        import torch
    except ImportError:
        torch = None
    if torch is not None and torch.cuda.is_available():
        pytest.skip("CUDA production path is exercised by the live-compatible GPU environment")
    candidate = {**SEED, "force_i_gain": FIXED_KI_SEEDS[0], "i_off": False}
    row = {
        "schema": "step5d.autotune-v4/r013-exact-observation-v1",
        "candidate": candidate,
        "objective_n": 1.0,
        "observation_variance_n2": 0.01,
        "completed": True,
        "sealed": True,
        "full_observation": True,
        "eligible": True,
        "censored": False,
        "anti_windup": _trial_metrics(),
    }
    config = ProductionGPConfig(noise_floor_n2=0.005, noise_snapshot_sha256="a" * 64)
    with pytest.raises(R013GPError, match="(core production GP requires CUDA|core production BoTorch runtime is unavailable)"):
        fit_core_production_gp([row], config=config)


def test_floor_correction_qlognei_requires_six_dimensional_provider() -> None:
    def core_provider(pool: Sequence[dict[str, Any]]) -> FloorCandidateProposal:
        candidate = pool[0]
        return FloorCandidateProposal(
            candidate=candidate,
            block_input=tuple(candidate_to_log_features(candidate)[index] for index in (0, 1, 2, 5)),
            contract=CORE_PROPOSAL_CONTRACT,
        )

    coordinator = FloorDiscoveryCoordinator(
        FloorDiscoveryPolicyV1(),
        fingerprint="correction-provider",
        core_proposal_provider=core_provider,
    )
    qualified_core_token: str | None = None
    while coordinator.novel_count < 100:
        request = coordinator.next_request()
        if request.role == CORE_SOBOL_NOVEL and qualified_core_token is None:
            qualified_core_token = request.canonical_token
        coordinator.record_result(
            admitted=True,
            sealed_mae_n=(0.5 if request.canonical_token == qualified_core_token else 1.0),
            posterior_beating_probability=(
                0.3 if request.canonical_token == qualified_core_token else 0.0
            ),
            guardrails_passed=True,
            receipt_id=f"guardrail-{request.request_id}",
        )
    while coordinator.novel_count < 112:
        request = coordinator.next_request()
        if request.role == SENTINEL:
            coordinator.record_result(
                admitted=True,
                sealed_mae_n=1.0,
                guardrails_passed=True,
                receipt_id=f"guardrail-{request.request_id}",
            )
            continue
        assert request.role == CORRECTION_SOBOL_NOVEL
        assert request.runtime_candidate is None
        assert request.trial.proposal_kind == "fresh_correction_sobol"
        coordinator.record_result(
            admitted=True,
            sealed_mae_n=1.0,
            guardrails_passed=True,
            receipt_id=f"guardrail-{request.request_id}",
        )
    with pytest.raises(RuntimePrimitiveNotInstalled, match="correction 6D qLogNEI proposal provider"):
        coordinator.next_request()
    assert coordinator.state == "runtime_primitive_not_installed"


def test_floor_adaptive_repeat_recomputes_retrospective_top5_and_keeps_probability() -> None:
    def provider(pool: Sequence[dict[str, Any]]) -> FloorCandidateProposal:
        candidate = pool[0]
        return FloorCandidateProposal(
            candidate=candidate,
            block_input=tuple(candidate_to_log_features(candidate)[index] for index in (0, 1, 2, 5)),
            contract=CORE_PROPOSAL_CONTRACT,
            posterior_beating_probability=0.0,
            acquisition_value=0.1,
        )

    coordinator = FloorDiscoveryCoordinator(
        FloorDiscoveryPolicyV1(), fingerprint="adaptive", core_proposal_provider=provider
    )
    # Reach a 20-novel rank window and retain two non-boundary proposals.
    while coordinator.novel_count < 20:
        request = coordinator.next_request()
        if request.role == SENTINEL:
            coordinator.record_result(admitted=True, sealed_mae_n=0.9)
        else:
            coordinator.record_result(admitted=True, sealed_mae_n=0.9)
    candidates = [
        token for token, stat in coordinator.stats.items()
        if stat.get("role") == CORE_BO_NOVEL
    ]
    assert candidates
    # Recompute against all stored novel rows, with the best row deliberately
    # not being the coordinator's most recent request.
    for token in candidates:
        coordinator.stats[token]["posterior_beating_probability"] = 0.0
    retrospective_token = candidates[0]
    coordinator.stats[retrospective_token]["posterior_beating_probability"] = 0.3
    coordinator.stats[retrospective_token]["values"] = [0.1]
    for token in candidates[1:]:
        coordinator.stats[token]["values"] = [0.9]
    coordinator._maybe_schedule_adaptive_repeats()
    queued = {item["canonical_token"] for item in coordinator.repeat_queue}
    assert retrospective_token in queued
    assert coordinator.stats[retrospective_token]["posterior_beating_probability"] == pytest.approx(0.3)


def test_floor_boundary_expansion_requires_all_three_guardrail_receipts() -> None:
    coordinator = FloorDiscoveryCoordinator(
        FloorDiscoveryPolicyV1(), fingerprint="boundary-guardrails", frozen_incumbent_n=1.0
    )
    request = coordinator.next_request()
    coordinator.record_result(admitted=True, sealed_mae_n=0.9, guardrails_passed=True, receipt_id="r0")
    for passed, receipt_id in ((True, "r1"), (False, "r2")):
        repeat = coordinator.next_request()
        assert repeat.role == REPEAT
        coordinator.record_result(
            admitted=True, sealed_mae_n=0.9, guardrails_passed=passed, receipt_id=receipt_id
        )
    decision = coordinator.boundary_decisions[0]
    assert decision["improvement_n"] >= 0.01
    assert decision["guardrails_passed"] is False
    assert [row["receipt_id"] for row in decision["guardrail_evidence"]] == ["r0", "r1", "r2"]
    assert decision["expanded"] is False


def test_floor_target_checkpoint_is_not_terminal_or_confirmation() -> None:
    coordinator = FloorDiscoveryCoordinator(FloorDiscoveryPolicyV1(), fingerprint="checkpoint")
    coordinator.next_request()
    coordinator.record_result(admitted=True, sealed_mae_n=0.35)
    assert coordinator.target_checkpoint is True
    assert coordinator.complete is False
    assert coordinator.next_request().role == REPEAT


def test_handoff_ab_flip_is_provisional_until_selected_arm_reaches_n5() -> None:
    plan = HandoffABPlanV1()

    def evidence(arm: str, mae: float) -> dict[str, Any]:
        return {
            "sealed": True,
            "sealed_mae_n": mae,
            "gaps": [],
            "fmin_n": 0.0,
            "fmax_n": 5.0,
            "pose_error_m": 0.0,
            "orientation_error_rad": 0.0,
            "carry_reset_receipt_ids": [f"carry-{arm}"],
            "tmae5_n": mae,
            "settling_time_s": 1.0,
            "raw_evidence_refs": [f"raw-{arm}"],
        }

    for _ in range(6):
        arm = plan.next_arm()
        assert arm is not None
        plan.record_evidence(arm, evidence(arm, 1.0 if arm == "A" else 1.03))
    assert plan.provisional_policy == "blind_reset_v0"
    assert plan.selected_policy is None
    # A fourth A row raises its variance; tie-breaks then flip to B.
    plan.record_evidence("A", evidence("A", 1.10))
    assert plan.provisional_policy == "freeze_carry_v1"
    assert plan.selected_policy is None
    assert plan.next_arm() == "B"
    plan.record_evidence("B", evidence("B", 1.03))
    assert plan.selected_policy is None
    plan.record_evidence("B", evidence("B", 1.03))
    assert plan.selected_policy == "freeze_carry_v1"
    assert len(plan._arm_rows("B")) == 5
    assert plan.materialize_handoff_policy() == "freeze_carry_v1"


def test_floor_persisted_pool_is_fresh_for_205_rounds_and_cold_replay() -> None:
    coordinator = FloorDiscoveryCoordinator(FloorDiscoveryPolicyV1(), fingerprint="pool-205")
    previous_cursor = -1
    for index in range(205):
        pool = coordinator.next_candidate_pool()
        keys = {physical_candidate_key(candidate) for candidate in pool}
        assert len(keys) == 128
        assert coordinator.sobol_cursor > previous_cursor
        previous_cursor = coordinator.sobol_cursor
        if index in {98, 99, 198}:
            replayed = FloorDiscoveryCoordinator.from_records(
                FloorDiscoveryPolicyV1(), coordinator.event_log, fingerprint="pool-205"
            )
            assert replayed.snapshot()["sobol_state"] == coordinator.snapshot()["sobol_state"]
            assert replayed.candidate_pool() == coordinator.candidate_pool()
    assert coordinator.sobol_round == 205


def test_outcome4_path_providers_match_frozen_geometry_and_signed_derivatives() -> None:
    cycloid = CycloidPathProviderV1()
    start = cycloid.sample(0.0)
    assert start.path_id == CYCLOID_PATH_ID
    assert start.normalized_progress == pytest.approx(0.0)
    assert start.degenerate_speed is True
    assert start.scalar_speed_m_s == pytest.approx(0.0)
    assert start.signed_planar_curvature_m_inv == pytest.approx(0.0)
    assert start.desired_twist_base_m_s == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    interior = cycloid.sample(10.0)
    theta = 1.0
    assert interior.desired_pose_base_m[:2] == pytest.approx(
        (0.015 * (theta - math.sin(theta)), 0.015 * (1.0 - math.cos(theta)))
    )
    assert interior.desired_twist_base_m_s[:2] == pytest.approx(
        (0.0015 * (1.0 - math.cos(theta)), 0.0015 * math.sin(theta))
    )
    assert interior.normalized_progress == pytest.approx(1.0 / 6.0)

    figure8 = FigureEightPathProviderV1()
    sample = figure8.sample(2.0)
    vx, vy = sample.desired_twist_base_m_s[:2]
    ax = -0.04 * 0.1 * 0.1 * math.sin(0.2)
    ay = -0.01 * 0.2 * 0.2 * math.sin(0.4)
    speed = math.hypot(vx, vy)
    expected_curvature = (vx * ay - vy * ax) / speed**3
    assert sample.signed_planar_curvature_m_inv == pytest.approx(expected_curvature)
    assert sample.identity_receipt.geometry_status == FIGURE8_GEOMETRY_STATUS
    assert sample.identity_receipt.frame_id == "base_frame_along_lateral_orientation_v1"
    assert PathContextError
    with pytest.raises(PathContextError):
        figure8.sample(60.001)


def test_outcome4_correction_is_bidirectional_target_only_and_replay_safe() -> None:
    path = CycloidPathProviderV1()
    scales = (1.0, 0.01, 0.002, 100.0, 1.0, 1.0)
    positive = ForceCorrectionPolicyV1(
        weights=CorrectionBlockV1((0.5, 0.0, 0.0, 0.0, 0.0, 0.0)),
        normalization_scales=scales,
    )
    negative = ForceCorrectionPolicyV1(
        weights=CorrectionBlockV1((-0.5, 0.0, 0.0, 0.0, 0.0, 0.0)),
        normalization_scales=scales,
    )
    context = path.sample(1.0)
    pos_state, pos_receipt = positive.apply(
        positive.initial_state("fp"), context,
        existing_phase_correction_n=0.0, fingerprint="fp",
        normal_load_n=5.0, signed_load_error_n=0.0,
    )
    _neg_state, neg_receipt = negative.apply(
        negative.initial_state("fp"), context,
        existing_phase_correction_n=0.0, fingerprint="fp",
    )
    assert pos_receipt.effective_target_n == pytest.approx(4.5)
    assert neg_receipt.effective_target_n == pytest.approx(5.5)
    assert pos_receipt.raw_features[0] == 1.0
    assert len(pos_receipt.normalized_features) == 6
    assert pos_receipt.diagnostics["force_frame_semantics"] == FORCE_FRAME_SEMANTICS
    assert pos_receipt.diagnostics["force_integrator_touched"] is False
    assert pos_receipt.zero_violations == ()
    # The diagnostic load values are not correction features.
    _, changed_diagnostic = positive.apply(
        positive.initial_state("fp"), context,
        existing_phase_correction_n=0.0, fingerprint="fp", normal_load_n=40.0,
    )
    assert changed_diagnostic.context_residual_n == pytest.approx(pos_receipt.context_residual_n)
    assert pos_state.last_applied_correction_n == pytest.approx(pos_receipt.applied_combined_correction_n)

    clipped = ForceCorrectionPolicyV1(
        weights=CorrectionBlockV1((0.0,) * 6), normalization_scales=scales,
    )
    clip_state, clip_receipt = clipped.apply(
        clipped.initial_state("clip"), path.sample(3.0),
        existing_phase_correction_n=2.0, fingerprint="clip",
    )
    assert clip_receipt.combined_clipped_n == pytest.approx(1.25)
    assert clip_receipt.applied_combined_correction_n == pytest.approx(1.25)
    assert clip_receipt.clip_applied is True
    assert clip_receipt.slew_limited is False
    assert clip_receipt.effective_target_n == pytest.approx(3.75)
    _slew_state, slew_receipt = clipped.apply(
        clipped.initial_state("slew"), path.sample(0.1),
        existing_phase_correction_n=2.0, fingerprint="slew",
    )
    assert slew_receipt.slew_limit_n == pytest.approx(0.05)
    assert slew_receipt.applied_combined_correction_n == pytest.approx(0.05)
    assert slew_receipt.slew_limited is True
    with pytest.raises(ForceCorrectionError, match="non-monotonic"):
        clipped.apply(
            clip_state, path.sample(2.0), existing_phase_correction_n=0.0, fingerprint="clip"
        )
    reset = clipped.reset(clip_state, boundary="home", fingerprint="clip")
    assert reset.generation == clip_state.generation + 1
    assert reset.last_applied_correction_n == 0.0
    with pytest.raises(ForceCorrectionError, match="fingerprint"):
        clipped.apply(reset, path.sample(1.0), existing_phase_correction_n=0.0, fingerprint="other")
    replayed = ForceCorrectionPolicyV1.from_mapping(clipped.as_dict())
    replay_state = ForceCorrectionStateV1.from_mapping(reset.as_dict())
    assert replayed.as_dict() == clipped.as_dict()
    assert replay_state.as_dict() == reset.as_dict()
    assert CORRECTION_FEATURE_NAMES == (
        "bias", "normalized_speed", "normalized_signed_acceleration",
        "normalized_signed_curvature", "sin_phase", "cos_phase",
    )


def test_outcome4_correction_block_migration_and_weight_only_floor_candidates() -> None:
    legacy = CorrectionBlockV1.from_mapping({
        "schema": "step5d.autotune-v4/r013-correction-block-placeholder-v1",
        "version": 1,
        "coordinates": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
    })
    assert legacy.is_legacy_migration is True
    assert legacy.weights == (0.0,) * 6
    assert legacy.as_dict()["schema"].endswith("placeholder-v1")
    base = ControllerCoreBlockV1(-10.0, 5.0, -4.0, 2.0)
    old_spec = FloorTrialSpec(
        role=CORRECTION_SOBOL_NOVEL,
        ordinal=1,
        active_block="correction",
        controller_core=base,
        correction=legacy,
        orientation_ko=0.05,
        motion_kp=1.5,
        target_force_n=5.0,
        proposal_kind="legacy_placeholder",
    )
    assert FloorTrialSpec.from_mapping(old_spec.as_dict()).canonical_key == old_spec.canonical_key

    def core_provider(pool: Sequence[dict[str, Any]]) -> FloorCandidateProposal:
        candidate = pool[0]
        return FloorCandidateProposal(
            candidate=candidate,
            block_input=tuple(candidate_to_log_features(candidate)[index] for index in (0, 1, 2, 5)),
            contract=CORE_PROPOSAL_CONTRACT,
        )

    coordinator = FloorDiscoveryCoordinator(
        FloorDiscoveryPolicyV1(), fingerprint="weights", core_proposal_provider=core_provider
    )
    qualified_core_token: str | None = None
    while coordinator.novel_count < 100:
        request = coordinator.next_request()
        if request.role == CORE_SOBOL_NOVEL and qualified_core_token is None:
            qualified_core_token = request.canonical_token
        coordinator.record_result(
            admitted=True,
            sealed_mae_n=(0.5 if request.canonical_token == qualified_core_token else 1.0),
            posterior_beating_probability=(
                0.3 if request.canonical_token == qualified_core_token else 0.0
            ),
            guardrails_passed=True,
            receipt_id=f"guardrail-{request.request_id}",
        )
    first = coordinator.next_request()
    if first.role == SENTINEL:
        coordinator.record_result(admitted=True, sealed_mae_n=1.0)
        first = coordinator.next_request()
    assert first.role == CORRECTION_SOBOL_NOVEL
    coordinator.record_result(admitted=True, sealed_mae_n=1.0)
    second = coordinator.next_request()
    assert second.role == CORRECTION_SOBOL_NOVEL
    assert first.trial.controller_core == second.trial.controller_core
    assert first.trial.orientation_ko == second.trial.orientation_ko == 0.05
    assert first.trial.motion_kp == second.trial.motion_kp == 1.5
    assert first.trial.correction.weights != second.trial.correction.weights
    assert all(-0.5 <= value <= 0.5 for value in first.trial.correction.weights)
    assert first.runtime_candidate is None and second.runtime_candidate is None


def _fill_metric(metric: MetricFingerprintV1, *, load_n: float = 5.2, skip_index: int | None = None) -> GapPreservingMetricAccumulatorV1:
    accumulator = GapPreservingMetricAccumulatorV1(metric)
    total_bins = int(round(metric.formal_end_s / metric.bin_width_s))
    for index in range(total_bins):
        if index == skip_index:
            continue
        accumulator.add_sample(
            time_s=(index + 0.5) * metric.bin_width_s,
            normal_load_n=load_n,
        )
    return accumulator


def test_outcome4_metric_fingerprints_have_exact_bins_gaps_and_no_pooling() -> None:
    cycloid = MetricFingerprintV1.cycloid()
    figure8 = MetricFingerprintV1.figure8()
    assert cycloid.metric_id == CYCLOID_METRIC and cycloid.required_bin_count == 550
    assert figure8.metric_id == FIGURE8_METRIC and figure8.required_bin_count == 550
    result = _fill_metric(cycloid).seal()
    assert result.exact is True and result.sealed is True
    assert result.formal_mae_n == pytest.approx(0.2)
    assert result.transient_mae_n == pytest.approx(0.2)
    assert result.full_curve_mae_n == pytest.approx(0.2)
    assert _fill_metric(figure8).seal().formal_mae_n == pytest.approx(0.2)
    with pytest.raises(MetricGapError, match="formal gaps"):
        _fill_metric(cycloid, skip_index=50).seal()
    assert cycloid != figure8
    with pytest.raises(MetricError):
        GapPreservingMetricAccumulatorV1(cycloid).add_sample(time_s=60.0, normal_load_n=5.0)


def test_outcome4_figure8_transfer_is_nonlaunchable_and_convergence_is_exact() -> None:
    package = load_figure8_transfer_template()
    assert package.status == AWAITING_CYCLOID_STATUS
    assert package.launch_ready is False
    assert package.sin_cos_weights == (0.0, 0.0)
    assert "motion_Kp" in package.path_specific_unset
    assert "anti_windup" in package.transferable
    assert package.convergence_met(
        novel_count=80, fresh_valid_proposal_count=25,
        max_posterior_probability_of_improvement=0.049,
        improvement_threshold_n=0.01, top3_repeat_evidence=(5, 5, 5),
    ) is True
    assert package.convergence_met(
        novel_count=80, fresh_valid_proposal_count=25,
        max_posterior_probability_of_improvement=0.05,
        improvement_threshold_n=0.01, top3_repeat_evidence=(5, 5, 5),
    ) is False
    with pytest.raises(ValueError, match="threshold must equal 0.01"):
        package.convergence_met(
            novel_count=80, fresh_valid_proposal_count=25,
            max_posterior_probability_of_improvement=0.049,
            improvement_threshold_n=0.0101, top3_repeat_evidence=(5, 5, 5),
        )
    assert package.budget_stop(novel_count=160) is True
    assert package.posterior_converged(
        novel_count=160, fresh_valid_proposal_count=25,
        max_posterior_probability_of_improvement=0.049,
        improvement_threshold_n=0.01, top3_repeat_evidence=(5, 5, 5),
    ) is True
    assert package.completion_status(
        novel_count=160, fresh_valid_proposal_count=25,
        max_posterior_probability_of_improvement=0.2,
        improvement_threshold_n=0.01, top3_repeat_evidence=(5, 5, 5),
    ) == {"posterior_converged": False, "budget_stop": True, "stop": True}
    with pytest.raises(ValueError):
        FigureEightTransferPackageV1.from_mapping({**package.as_dict(), "launch_ready": True})


@pytest.mark.parametrize("pre_enabled", [True, False])
@pytest.mark.parametrize("raise_during_window", [False, True])
def test_r013_gc_window_disables_before_arm_and_restores_after_timing_release(
    monkeypatch: pytest.MonkeyPatch,
    pre_enabled: bool,
    raise_during_window: bool,
) -> None:
    state = {"enabled": pre_enabled}
    events: list[str] = []

    def isenabled() -> bool:
        return state["enabled"]

    def disable() -> None:
        events.append("gc.disable")
        state["enabled"] = False

    def enable() -> None:
        events.append("gc.enable")
        state["enabled"] = True

    monkeypatch.setattr(live_owner_module.gc, "isenabled", isenabled)
    monkeypatch.setattr(live_owner_module.gc, "disable", disable)
    monkeypatch.setattr(live_owner_module.gc, "enable", enable)

    receipt = live_owner_module.R013GCWindowReceipt.capture()
    receipt.enter()
    for phase in ("runtime.arm", "runtime.execute_motion_contact", "safe_return.home"):
        events.append(phase)
        assert state["enabled"] is False
    try:
        if raise_during_window:
            raise RuntimeError("injected R013 window failure")
    except RuntimeError:
        pass
    finally:
        events.append("timing_lease.release")
        receipt.restore_after_timing_lease()

    assert events[:2] == ["gc.disable", "runtime.arm"]
    assert events.index("timing_lease.release") < (
        events.index("gc.enable") if pre_enabled else len(events)
    )
    assert state["enabled"] is pre_enabled
    assert receipt.as_dict() == {
        "schema": "step5d.autotune-v4/r013-gc-window-receipt-v1",
        "scope": "ARM_EXECUTE_MOTION_CONTACT_SAFE_RETURN_HOME",
        "pre_enabled": pre_enabled,
        "entered": True,
        "restored": True,
        "post_enabled": pre_enabled,
        "restored_after_timing_lease": True,
    }
