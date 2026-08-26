from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


HISTORICAL_COORDINATE = {
    "force_p_gain": 0.019027313840405524,
    "force_damping": 188.36079701683204,
    "force_i_gain": 0.0,
    "i_off": True,
    "normal_filter_tau_s": 0.04375,
    "orientation_ko": 0.05,
    "motion_kp": 2.5226892457611436,
    "target_force_n": 5.0,
}


def _config():
    from step5d_autotune_v4_r013.v4_two_stage_campaign import load_two_stage_config

    raw = json.loads(
        (ROOT / "config/step5d/v4_two_stage_campaign_v1.json").read_text(
            encoding="utf-8"
        )
    )
    return load_two_stage_config(raw)


def _config3():
    from step5d_autotune_v4_r013.v4_two_stage_campaign import load_three_stage_config

    raw = json.loads(
        (ROOT / "config/step5d/v4_two_stage_campaign_v1.json").read_text(
            encoding="utf-8"
        )
    )
    return load_three_stage_config(raw)


def test_two_stage_config_has_exact_ff_and_5d_6d_contract() -> None:
    from step5d_autotune_v4_r013.v4_two_stage_campaign import V4Stage

    stage_a, stage_b = _config()
    assert stage_a.stage is V4Stage.FF_IOFF_100
    assert stage_b.stage is V4Stage.FF_ION_6D_100
    assert len(stage_a.feature_names) == 5
    assert len(stage_b.feature_names) == 6
    assert stage_a.ff_bundle.phase_force_reference is True
    assert stage_a.ff_bundle.curvature is False
    assert stage_b.kernel == "matern52_ard"
    assert stage_a.fingerprint_sha256 != stage_b.fingerprint_sha256


def test_three_stage_config_adds_limit_aware_seven_dimensional_branch() -> None:
    from step5d_autotune_v4_r013.v4_two_stage_campaign import V4Stage

    stage_a, stage_b1, stage_b2 = _config3()
    assert stage_a.stage is V4Stage.FF_IOFF_100
    assert stage_b1.stage is V4Stage.FF_ION_6D_100
    assert stage_b2.stage is V4Stage.FF_ION_LIMIT_7D_100
    assert len(stage_b1.feature_names) == 6
    assert len(stage_b2.feature_names) == 7
    assert stage_b2.integral_state_limit_bounds == pytest.approx((0.5, 5.0))
    assert len({stage_a.fingerprint_sha256, stage_b1.fingerprint_sha256, stage_b2.fingerprint_sha256}) == 3


def test_stage_a_candidate_keeps_ko_and_forces_i_off() -> None:
    from step5d_autotune_v4_r013.v4_two_stage_campaign import (
        V4Stage,
        candidate_features,
        validate_candidate,
    )

    parsed = validate_candidate(HISTORICAL_COORDINATE, V4Stage.FF_IOFF_100)
    assert parsed["i_off"] is True
    assert parsed["force_i_gain"] == 0.0
    assert len(candidate_features(parsed, V4Stage.FF_IOFF_100)) == 5
    assert parsed["orientation_ko"] == pytest.approx(0.05)


def test_stage_b_accepts_full_six_dimensional_i_on_candidate() -> None:
    from step5d_autotune_v4_r013.v4_two_stage_campaign import V4Stage, candidate_features, validate_candidate

    candidate = {**HISTORICAL_COORDINATE, "force_i_gain": 0.008610779292198037, "i_off": False}
    parsed = validate_candidate(candidate, V4Stage.FF_ION_6D_100)
    assert parsed["i_off"] is False
    assert len(candidate_features(parsed, V4Stage.FF_ION_6D_100)) == 6


def test_stage_b2_accepts_feasible_limit_and_rejects_authority_masked_limit() -> None:
    from step5d_autotune_v4_r013.v4_two_stage_campaign import (
        V4Stage,
        candidate_features,
        validate_candidate,
        V4StageError,
    )

    candidate = {
        **HISTORICAL_COORDINATE,
        "force_i_gain": 0.001810193359837562,
        "i_off": False,
        "integral_state_limit_n_s": 5.0,
    }
    parsed = validate_candidate(candidate, V4Stage.FF_ION_LIMIT_7D_100)
    assert parsed["integral_state_limit_n_s"] == pytest.approx(5.0)
    assert len(candidate_features(parsed, V4Stage.FF_ION_LIMIT_7D_100)) == 7

    masked = {**parsed, "force_i_gain": 0.008610779292198037, "integral_state_limit_n_s": 2.0}
    with pytest.raises(V4StageError, match="masked by the authority clamp"):
        validate_candidate(masked, V4Stage.FF_ION_LIMIT_7D_100)


def test_stage_b2_warm_prefix_contains_explicit_one_two_five_state_limit_sentinels(
    tmp_path: Path,
) -> None:
    from step5d_autotune_v4_r013.v4_two_stage_campaign import V4Stage, V4StageCampaignV1

    _stage_a, _stage_b1, stage_b2 = _config3()
    seed = {**HISTORICAL_COORDINATE, "force_i_gain": 0.008610779292198037, "i_off": False}
    campaign = V4StageCampaignV1(
        config=stage_b2,
        seed_candidate=seed,
        fingerprint_sha256=stage_b2.fingerprint_sha256,
        ledger_path=tmp_path / "b2.jsonl",
    )
    limits = []
    for _index in range(6):
        attempt = campaign.ask()
        limits.append(float(attempt.candidate["integral_state_limit_n_s"]))
        campaign.record_ineligible("offline-sentinel")
    assert 1.0 in limits and 2.0 in limits and 5.0 in limits


def test_stage_a_attempt_budget_censor_and_milestones() -> None:
    from step5d_autotune_v4_r013.v4_two_stage_campaign import (
        AttemptOutcome,
        ProposalMethod,
        V4Stage,
        V4StageCampaignV1,
    )

    config, _stage_b = _config()
    campaign = V4StageCampaignV1(
        config=config,
        seed_candidate=HISTORICAL_COORDINATE,
        fingerprint_sha256=config.fingerprint_sha256,
    )
    for value in (0.45, 0.40, 0.35):
        attempt = campaign.ask()
        assert attempt.method is ProposalMethod.ANCHOR
        result = campaign.record_exact(value)
        assert result.outcome is AttemptOutcome.EXACT
    assert campaign.confirmed_incumbent is not None

    for _ in range(9):
        campaign.ask()
        campaign.record_exact(0.34)
    attempt = campaign.ask()
    assert attempt.method in {
        ProposalMethod.QLOGNEI_LOCAL,
        ProposalMethod.QLOGNEI_GLOBAL,
    }
    censored = campaign.record_censored(1.0, closed_bin_count=25)
    assert censored.outcome is AttemptOutcome.CENSORED
    assert censored.trainable is False
    assert campaign.attempt_count == 13
    assert campaign.confirmed_incumbent is not None

    campaign.ask()
    exact = campaign.record_exact(0.09)
    assert exact.milestone_thresholds_n == (0.3, 0.2, 0.1)
    assert campaign.status()["announced_milestones_n"] == [0.3, 0.2, 0.1]


def test_warm_attempts_cannot_be_censored_and_budget_counts_censored(tmp_path: Path) -> None:
    from step5d_autotune_v4_r013.v4_two_stage_campaign import V4StageCampaignV1, V4StageError

    config, _stage_b = _config()
    campaign = V4StageCampaignV1(
        config=config,
        seed_candidate=HISTORICAL_COORDINATE,
        fingerprint_sha256=config.fingerprint_sha256,
        ledger_path=tmp_path / "stage.jsonl",
    )
    campaign.ask()
    with pytest.raises(V4StageError, match="warm/anchor"):
        campaign.record_censored(2.0, closed_bin_count=25, incumbent_mean_n=0.2)
    assert campaign.attempt_count == 0


def test_resume_ledger_keeps_header_and_dispatch_result_records(tmp_path: Path) -> None:
    from step5d_autotune_v4_r013.v4_two_stage_campaign import V4StageCampaignV1

    config, _stage_b = _config()
    path = tmp_path / "stage.jsonl"
    campaign = V4StageCampaignV1(
        config=config,
        seed_candidate=HISTORICAL_COORDINATE,
        fingerprint_sha256=config.fingerprint_sha256,
        ledger_path=path,
    )
    campaign.ask()
    campaign.record_exact(0.4)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in rows] == ["header", "dispatch", "result"]
    assert rows[-1]["attempt"]["outcome"] == "exact"
    resumed = V4StageCampaignV1.resume(
        config=config,
        seed_candidate=HISTORICAL_COORDINATE,
        fingerprint_sha256=config.fingerprint_sha256,
        ledger_path=path,
    )
    assert resumed.attempt_count == 1
    assert resumed.best_exact is not None


def test_timing_ineligible_receipt_persists_diagnostics_without_gp_admission(
    tmp_path: Path,
) -> None:
    from step5d_autotune_v4_r013.v4_two_stage_campaign import (
        AttemptOutcome,
        V4StageCampaignV1,
        V4StageError,
    )

    config, _stage_b = _config()
    path = tmp_path / "timing-ineligible.jsonl"
    campaign = V4StageCampaignV1(
        config=config,
        seed_candidate=HISTORICAL_COORDINATE,
        fingerprint_sha256=config.fingerprint_sha256,
        ledger_path=path,
    )
    timing_diagnostics = {
        "schema": "step5d.autotune-v4/r013-coalescing-aware-trial-timing-v1",
        "accepted": False,
        "legacy_timing_gate": True,
        "writer_hz": 397.0,
        "rtde_hz": 396.0,
        "tp_hz": 395.0,
        "tp_writer_ratio": 0.95,
        "feedback_p99_s": 0.005,
        "max_fresh_gap_s": 0.006,
    }
    campaign.ask()
    attempt = campaign.record_ineligible(
        "legacy_timing_gate_failed_without_coalescing_contract",
        partial_receipt_path="trial.r013life.json",
        home_receipt_path="trial.r013life.json",
        timing_diagnostics=timing_diagnostics,
        diagnostic_mae_n=0.734,
    )

    assert attempt.outcome is AttemptOutcome.INELIGIBLE
    assert attempt.trainable is False
    assert attempt.timing_diagnostics == timing_diagnostics
    assert attempt.diagnostic_mae_n == pytest.approx(0.734)
    assert campaign.exact_observations == ()
    result_row = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert result_row["attempt"]["timing_diagnostics"] == timing_diagnostics
    assert result_row["attempt"]["diagnostic_mae_n"] == pytest.approx(0.734)

    resumed = V4StageCampaignV1.resume(
        config=config,
        seed_candidate=HISTORICAL_COORDINATE,
        fingerprint_sha256=config.fingerprint_sha256,
        ledger_path=path,
    )
    resumed_attempt = resumed.attempts[0]
    assert resumed_attempt.timing_diagnostics == timing_diagnostics
    assert resumed_attempt.diagnostic_mae_n == pytest.approx(0.734)
    assert resumed.exact_observations == ()

    resumed.ask()
    with pytest.raises(V4StageError, match="diagnostic MAE cannot be negative"):
        resumed.record_ineligible(
            "legacy_timing_gate_failed_without_coalescing_contract",
            timing_diagnostics={"reason": "invalid_per_trial_timing_evidence"},
            diagnostic_mae_n=-0.001,
        )
    assert resumed.attempt_count == 1


def test_coalescing_gate_fails_closed_on_missing_values_and_legacy_gate() -> None:
    from run_v4_stage_live import _coalescing_trial_gate

    common = {
        "physical_admission": {"sealed": True, "motion_gate": True},
        "home": True,
        "safe_return": True,
        "legacy_timing_gate": True,
    }
    missing = {
        **common,
        "timing_evidence": {
            "layer_rates_hz": {
                "writer_publishes": 500.0,
                "rtde_frames": 500.0,
                "tp_consumed_packet_echoes": 480.0,
            },
            "feedback_age_p99_s": 0.005,
        },
    }
    accepted, missing_receipt = _coalescing_trial_gate(missing)
    assert accepted is False
    assert missing_receipt == {"reason": "invalid_per_trial_timing_evidence"}

    legacy = {
        **common,
        "timing_evidence": {
            "layer_rates_hz": {
                "writer_publishes": 500.0,
                "rtde_frames": 500.0,
                "tp_consumed_packet_echoes": 480.0,
            },
            "feedback_age_p99_s": 0.005,
            "max_fresh_gap_s": 0.006,
        },
    }
    accepted, legacy_receipt = _coalescing_trial_gate(legacy)
    assert accepted is False
    assert legacy_receipt["legacy_timing_gate"] is True
    assert legacy_receipt["writer_hz"] == pytest.approx(500.0)
    assert legacy_receipt["rtde_hz"] == pytest.approx(500.0)
    assert legacy_receipt["tp_hz"] == pytest.approx(480.0)
    assert legacy_receipt["tp_writer_ratio"] == pytest.approx(0.96)
    assert legacy_receipt["feedback_p99_s"] == pytest.approx(0.005)
    assert legacy_receipt["max_fresh_gap_s"] == pytest.approx(0.006)


def test_winner_confirmation_is_outside_search_budget(tmp_path: Path) -> None:
    from step5d_autotune_v4_r013.v4_two_stage_campaign import V4StageCampaignV1

    config, _stage_b = _config()
    campaign = V4StageCampaignV1(
        config=config,
        seed_candidate=HISTORICAL_COORDINATE,
        fingerprint_sha256=config.fingerprint_sha256,
        ledger_path=tmp_path / "stage.jsonl",
    )
    for _ in range(100):
        campaign.ask()
        campaign.record_exact(0.4)
    confirmation = campaign.ask_confirmation()
    assert confirmation.ordinal == 101
    campaign.record_exact(0.39)
    assert campaign.search_attempt_count == 100
    assert campaign.confirmation_attempt_count == 1


def test_bounded_or_stop_reason_never_falls_through_to_confirmation() -> None:
    from run_v4_stage_live import _bounded_stop_reason

    assert _bounded_stop_reason(
        target_hit=True,
        attempt_count=1,
        stop_after_attempts=100,
        campaign_complete=False,
    ) == "target_achieved"
    assert _bounded_stop_reason(
        target_hit=False,
        attempt_count=100,
        stop_after_attempts=100,
        campaign_complete=True,
    ) == "epoch_budget_exhausted"
    # A caller-supplied budget larger than the stage's own search budget still
    # cannot enter confirmation after the stage closes.
    assert _bounded_stop_reason(
        target_hit=False,
        attempt_count=100,
        stop_after_attempts=101,
        campaign_complete=True,
    ) == "search_budget_exhausted"
    # The unbounded legacy path keeps its normal confirmation behavior.
    assert _bounded_stop_reason(
        target_hit=False,
        attempt_count=100,
        stop_after_attempts=None,
        campaign_complete=True,
    ) is None


def test_stage_optimizer_builds_5d_and_6d_pools_and_within_candidate_yvar() -> None:
    from step5d_autotune_v4_r013.v4_stage_optimizer import (
        fresh_pool,
        grouped_observation_noise,
        propose_qlognei,
    )
    from step5d_autotune_v4_r013.v4_two_stage_campaign import V4Stage

    stage_a, stage_b = _config()
    pool_a = fresh_pool(stage=V4Stage.FF_IOFF_100, count=8)
    pool_b = fresh_pool(stage=V4Stage.FF_ION_6D_100, count=8)
    assert len(pool_a) == len(pool_b) == 8
    assert all(item["i_off"] is True for item in pool_a)
    assert all(item["i_off"] is False for item in pool_b)
    observations = [
        {"candidate": HISTORICAL_COORDINATE, "mae_n": 0.5},
        {"candidate": HISTORICAL_COORDINATE, "mae_n": 0.4},
    ]
    groups = grouped_observation_noise(observations, stage=V4Stage.FF_IOFF_100)
    assert len(groups) == 1
    assert groups[0]["ddof"] == 1
    proposal = propose_qlognei(
        stage=V4Stage.FF_IOFF_100,
        observations=observations,
        pool=pool_a,
        local=True,
    )
    assert proposal["kernel"] == "matern52_ard"
    assert proposal["fit_receipt"]["backend"] == "offline_deterministic_fallback"


def test_stage_optimizer_builds_feasible_seven_dimensional_limit_pool() -> None:
    from step5d_autotune_v4_r013.v4_stage_optimizer import fresh_pool
    from step5d_autotune_v4_r013.v4_two_stage_campaign import V4Stage

    _stage_a, _stage_b1, stage_b2 = _config3()
    pool = fresh_pool(stage=V4Stage.FF_ION_LIMIT_7D_100, count=16)
    assert len(pool) == 16
    assert all(len(item) == 9 for item in pool)
    assert all(0.5 <= item["integral_state_limit_n_s"] <= 5.0 for item in pool)
    assert all(
        item["integral_state_limit_n_s"]
        <= min(5.0, 0.5 / (item["force_i_gain"] / item["force_p_gain"])) + 1e-12
        for item in pool
    )
    assert len(stage_b2.feature_names) == 7


def test_stage_optimizer_worker_cpu_fallback_is_explicit() -> None:
    from step5d_autotune_v4_r013.v4_stage_optimizer import fresh_pool
    from step5d_autotune_v4_r013.v4_stage_optimizer_worker import propose
    from step5d_autotune_v4_r013.v4_two_stage_campaign import V4Stage

    pool = fresh_pool(stage=V4Stage.FF_IOFF_100, count=8)
    response = propose({
        "stage": V4Stage.FF_IOFF_100.value,
        "require_cuda": False,
        "local_refinement": True,
        "pool": pool,
        "observations": [
            {
                "candidate": HISTORICAL_COORDINATE,
                "mean_n": 0.4,
                "yvar_n2": 0.01,
                "fingerprint_sha256": "a" * 64,
            }
        ],
    })
    assert response["ok"] is True
    assert response["worker_schema"].endswith("worker-v1")
    assert response["fit_receipt"]["backend"] == "offline_deterministic_fallback"


def test_i_off_stage_materializes_to_r013_zero_i_without_identity_alias() -> None:
    from step5d_autotune_v4_r013.v4_stage_live_adapter import materialize_runtime_candidate
    from step5d_autotune_v4_r013.v4_two_stage_campaign import V4Stage

    runtime, receipt = materialize_runtime_candidate(HISTORICAL_COORDINATE, V4Stage.FF_IOFF_100)
    assert runtime["i_off"] is True
    assert runtime["force_i_gain"] == 0.0
    assert receipt["stage"] == V4Stage.FF_IOFF_100.value
    assert receipt["stage_candidate_token"] != receipt["runtime_candidate_token"]


def test_limit_aware_stage_materializes_limit_into_mature_candidate() -> None:
    from step5d_autotune_v4_r006.live_adapter import R006Candidate
    from step5d_autotune_v4_r013.v4_stage_live_adapter import materialize_runtime_candidate
    from step5d_autotune_v4_r013.v4_two_stage_campaign import V4Stage

    candidate = {
        **HISTORICAL_COORDINATE,
        "force_i_gain": 0.001810193359837562,
        "i_off": False,
        "integral_state_limit_n_s": 5.0,
    }
    runtime, receipt = materialize_runtime_candidate(candidate, V4Stage.FF_ION_LIMIT_7D_100)
    mature = R006Candidate.from_canonical(runtime)
    assert mature.integral_state_limit_n_s == pytest.approx(5.0)
    assert receipt["stage_candidate"]["integral_state_limit_n_s"] == pytest.approx(5.0)


def test_streaming_stage_censor_requests_after_25_closed_bins() -> None:
    from step5d_autotune_v4_r013.v4_stage_censor import V4StageCensorObserverV1
    from step5d_autotune_v4_r013.v4_two_stage_campaign import V4StageCampaignV1

    config, _stage_b = _config()
    campaign = V4StageCampaignV1(
        config=config,
        seed_candidate=HISTORICAL_COORDINATE,
        fingerprint_sha256=config.fingerprint_sha256,
    )
    for value in (0.40, 0.42, 0.41):
        campaign.ask()
        campaign.record_exact(value)
    for _ in range(9):
        campaign.ask()
        campaign.record_exact(0.39)
    attempt = campaign.ask()
    observer = V4StageCensorObserverV1(campaign, attempt.ordinal)
    for index in range(25):
        start = 5.0 + index * 0.1
        observer.observe(state=25, path_time_s=start, filtered_normal_n=10.0)
        observer.observe(state=25, path_time_s=start + 0.099, filtered_normal_n=10.0)
    observer.observe(state=25, path_time_s=7.5, filtered_normal_n=5.0)
    assert observer.requested is True
    assert observer.closed_bin_count >= 25
    assert observer.trigger_watermark_s is not None


def test_stage_b_uses_cross_stage_confirmed_mean_before_local_n3(tmp_path: Path) -> None:
    from step5d_autotune_v4_r013.v4_two_stage_campaign import (
        V4CrossStageBaselineV1,
        V4Stage,
        V4StageCampaignV1,
    )

    _stage_a, stage_b = _config()
    baseline = V4CrossStageBaselineV1(
        target_stage=V4Stage.FF_ION_6D_100.value,
        source_stage=V4Stage.FF_IOFF_100.value,
        candidate_token="a" * 64,
        mean_n=0.5212097187097937,
        n=3,
        source_fingerprint_sha256="b" * 64,
        source_identity="c" * 64,
        confirmation_receipt_sha256="d" * 64,
    )
    seed = {**HISTORICAL_COORDINATE, "force_i_gain": 0.008610779292198037, "i_off": False}
    campaign = V4StageCampaignV1(
        config=stage_b,
        seed_candidate=seed,
        fingerprint_sha256=stage_b.fingerprint_sha256,
        ledger_path=tmp_path / "stage-b.jsonl",
        cross_stage_baseline=baseline,
    )
    # Consume anchor/warm rows without creating a local confirmed incumbent.
    for _ in range(12):
        campaign.ask()
        campaign.record_ineligible("warm-only-test")
    assert campaign.confirmed_incumbent is None
    assert campaign.censor_incumbent["baseline_kind"] == "cross_stage_confirmed"
    assert campaign.censor_incumbent["mean_n"] == pytest.approx(0.5212097187097937)
    attempt = campaign.ask()
    censored = campaign.record_censored(
        1.1,
        closed_bin_count=25,
        censor_baseline=campaign.censor_incumbent,
    )
    assert attempt.method.value.startswith("qlognei")
    assert censored.outcome.value == "censored"
    assert censored.censor_baseline["source_stage"] == V4Stage.FF_IOFF_100.value
    assert campaign.attempt_count == 13


def test_cross_stage_loader_requires_promotion_and_sealed_serialization_repair(
    tmp_path: Path,
) -> None:
    from step5d_autotune_v4_r013.v4_two_stage_campaign import (
        V4CrossStageBaselineV1,
        V4Stage,
        V4StageError,
    )

    source_report = ROOT / "reports/v4_stage_a_confirmation_20260818.json"
    raw = json.loads(source_report.read_text(encoding="utf-8"))
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    correction = ROOT / "reports/v4_fixed_confirmation_serialization_correction_05.json"
    correction_raw = json.loads(correction.read_text(encoding="utf-8"))
    correction_body = {
        key: value for key, value in correction_raw.items() if key != "receipt_sha256"
    }
    correction_raw["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            correction_body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    (report_dir / correction.name).write_text(
        json.dumps(correction_raw, sort_keys=True) + "\n", encoding="utf-8"
    )
    report = report_dir / "confirmation.json"

    # The historical report deliberately declined promotion and must not be
    # imported merely because it contains three physical confirmation rows.
    raw["promotion_to_stage_b"] = True
    body = {key: value for key, value in raw.items() if key != "receipt_sha256"}
    raw["receipt_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    report.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
    baseline = V4CrossStageBaselineV1.from_confirmation_report(
        report,
        target_stage=V4Stage.FF_ION_6D_100,
    )
    assert baseline.n == 3
    assert baseline.mean_n == pytest.approx(raw["summary"]["mean_n"])

    raw["summary"]["mean_n"] = 0.1
    report.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(V4StageError, match="receipt hash"):
        V4CrossStageBaselineV1.from_confirmation_report(
            report,
            target_stage=V4Stage.FF_ION_6D_100,
        )

    original = json.loads(source_report.read_text(encoding="utf-8"))
    original_report = report_dir / "not-promoted.json"
    original_report.write_text(json.dumps(original, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(V4StageError, match="not authorized"):
        V4CrossStageBaselineV1.from_confirmation_report(
            original_report,
            target_stage=V4Stage.FF_ION_6D_100,
        )


def test_v4_live_censor_uses_25_bin_guard_without_mutating_r012_guard() -> None:
    from types import SimpleNamespace

    from step5d_autotune_v4_r012.scheduler import ScheduledCandidate
    from step5d_autotune_v4_r013.v4_stage_censor import V4ActiveCensorRuntime, V4_MIN_CLOSED_BINS
    from step5d_autotune_v4_r012.censor import GUARD_BINS

    class Controller:
        def __init__(self) -> None:
            self.requested: list[int] = []

        def arm(self, sequence: int) -> None:
            self.sequence = sequence

        def request_early_end(self, sequence: int) -> bool:
            self.requested.append(sequence)
            return True

    controller = Controller()
    runtime = V4ActiveCensorRuntime(
        controller=controller, campaign_id="c", run_id="r", attempt_id="a"
    )
    runtime.arm(
        scheduled=ScheduledCandidate(
            candidate=HISTORICAL_COORDINATE,
            kind="BO_TRIAL",
            ordinal=13,
            abort_allowed=True,
        ),
        attempt_sequence=13,
        incumbent_mean_n=0.5212097187097937,
        baseline={"baseline_kind": "cross_stage_confirmed", "mean_n": 0.5212097187097937},
    )
    for index in range(V4_MIN_CLOSED_BINS):
        start = 5.0 + index * 0.1
        runtime.observe_path_sample(SimpleNamespace(state=25, path_time_s=start, filtered_normal_n=10.0))
        runtime.observe_path_sample(SimpleNamespace(state=25, path_time_s=start + 0.099, filtered_normal_n=10.0))
    runtime.observe_path_sample(SimpleNamespace(state=25, path_time_s=7.5, filtered_normal_n=5.0))
    assert runtime.requested is True
    assert controller.requested == [13]
    assert V4_MIN_CLOSED_BINS == 25
    assert GUARD_BINS == 55


def test_v4_live_censor_seals_against_live_register_ack_without_fake_rtde() -> None:
    from types import SimpleNamespace

    from step5d_autotune_v4_r012.scheduler import ScheduledCandidate
    from step5d_autotune_v4_r013.v4_stage_censor import V4ActiveCensorRuntime

    class LiveController:
        writer = object()

        def arm(self, sequence: int) -> None:
            self.sequence = sequence

        def request_early_end(self, sequence: int) -> bool:
            self.requested = sequence
            return True

        def read_completion_registers(self) -> tuple[int, int, int]:
            return (self.sequence, 0, 0)

    controller = LiveController()
    runtime = V4ActiveCensorRuntime(
        controller=controller, campaign_id="c", run_id="r", attempt_id="a"
    )
    candidate = {**HISTORICAL_COORDINATE, "force_i_gain": 0.008610779292198037, "i_off": False}
    runtime.arm(
        scheduled=ScheduledCandidate(candidate=candidate, kind="BO_TRIAL", ordinal=13, abort_allowed=True),
        attempt_sequence=13,
        incumbent_mean_n=0.5,
        baseline={"baseline_kind": "cross_stage_confirmed", "mean_n": 0.5},
    )
    for index in range(25):
        start = 5.0 + index * 0.1
        runtime.observe_path_sample(SimpleNamespace(state=25, path_time_s=start, filtered_normal_n=10.0))
        runtime.observe_path_sample(SimpleNamespace(state=25, path_time_s=start + 0.099, filtered_normal_n=10.0))
    runtime.observe_path_sample(SimpleNamespace(state=25, path_time_s=7.5, filtered_normal_n=5.0))
    row = runtime.finalize(
        dispatch_id="d", return_guard=True, home=True, safe_return=True
    )
    assert row is not None
    assert row.closed_bin_count == 25
    assert row.baseline["baseline_kind"] == "cross_stage_confirmed"


def test_v4_censor_binds_physical_sequence_after_stage_ordinal() -> None:
    from step5d_autotune_v4_r012.scheduler import ScheduledCandidate
    from step5d_autotune_v4_r013.v4_stage_censor import V4ActiveCensorRuntime

    class Controller:
        def __init__(self) -> None:
            self.armed: list[int] = []

        def arm(self, sequence: int) -> None:
            self.armed.append(sequence)

    controller = Controller()
    runtime = V4ActiveCensorRuntime(
        controller=controller, campaign_id="c", run_id="r", attempt_id="a"
    )
    runtime.arm(
        scheduled=ScheduledCandidate(
            candidate=HISTORICAL_COORDINATE,
            kind="BO_TRIAL",
            ordinal=1,
            abort_allowed=True,
        ),
        attempt_sequence=None,
        incumbent_mean_n=0.5,
    )
    assert controller.armed == []
    runtime.bind_physical_attempt(4)
    assert runtime.attempt_sequence == 4
    assert controller.armed == [4]


def test_recoverable_home_then_resume_uses_next_append_only_ordinal(tmp_path: Path) -> None:
    from step5d_autotune_v4_r013.recovery import (
        safe_home_then_resume_for_recoverable_failures_only,
    )
    from step5d_autotune_v4_r013.v4_two_stage_campaign import (
        V4FailureClass,
        V4FailureDisposition,
        V4FailureEvidenceV1,
        V4RecoveryStatus,
        V4StageCampaignV1,
    )

    config, _stage_b = _config()
    campaign = V4StageCampaignV1(
        config=config,
        seed_candidate=HISTORICAL_COORDINATE,
        fingerprint_sha256=config.fingerprint_sha256,
        ledger_path=tmp_path / "recovery.jsonl",
    )
    attempt = campaign.ask()
    home_calls: list[str] = []
    receipt = safe_home_then_resume_for_recoverable_failures_only(
        failure=V4FailureEvidenceV1(
            failure_class=V4FailureClass.TIMING_BOUNDARY,
            reason="timing gap at host boundary",
            home_permitted=True,
        ),
        current_attempt_ordinal=attempt.ordinal,
        attempt_count=1,
        prior_epoch=1,
        fresh_epoch=2,
        fresh_readiness=True,
        dispatch_in_flight=False,
        home_action=lambda: home_calls.append("home") or {
            "home_verified": True,
            "receipt_path": "recovery-home.json",
        },
    )
    assert receipt.disposition is V4FailureDisposition.RECOVERABLE_RESUME
    assert receipt.status is V4RecoveryStatus.RESUME_READY
    assert receipt.home_attempted is True
    assert home_calls == ["home"]
    campaign.record_recoverable_failure(receipt)
    restored = V4StageCampaignV1.resume(
        config=config,
        seed_candidate=HISTORICAL_COORDINATE,
        fingerprint_sha256=config.fingerprint_sha256,
        ledger_path=tmp_path / "recovery.jsonl",
    )
    assert restored.terminal_failure is None
    resumed = restored.resume_after_recovery(receipt)
    assert resumed.ordinal == 2
    assert campaign.terminal_failure is None
    assert [json.loads(line)["event"] for line in (tmp_path / "recovery.jsonl").read_text().splitlines()] == [
        "header", "dispatch", "result", "recovery", "dispatch"
    ]


def test_force_invariant_homes_when_permitted_but_is_terminal(tmp_path: Path) -> None:
    from step5d_autotune_v4_r013.recovery import (
        safe_home_then_resume_for_recoverable_failures_only,
    )
    from step5d_autotune_v4_r013.v4_two_stage_campaign import (
        V4FailureClass,
        V4FailureDisposition,
        V4FailureEvidenceV1,
        V4RecoveryStatus,
    )

    home_calls: list[str] = []
    receipt = safe_home_then_resume_for_recoverable_failures_only(
        failure=V4FailureEvidenceV1(
            failure_class=V4FailureClass.FORCE_INVARIANT,
            reason="provider output exceeded invariant envelope",
            home_permitted=True,
            force_fault=True,
        ),
        current_attempt_ordinal=54,
        attempt_count=54,
        prior_epoch=7,
        fresh_epoch=8,
        fresh_readiness=True,
        dispatch_in_flight=False,
        home_action=lambda: home_calls.append("home") or {"home_verified": True},
    )
    assert home_calls == ["home"]
    assert receipt.disposition is V4FailureDisposition.HARD_TERMINAL
    assert receipt.status is V4RecoveryStatus.TERMINAL
    assert receipt.auto_dispatch_permitted is False


def test_protective_stop_records_home_blocked_without_home_attempt() -> None:
    from step5d_autotune_v4_r013.recovery import (
        safe_home_then_resume_for_recoverable_failures_only,
    )
    from step5d_autotune_v4_r013.v4_two_stage_campaign import (
        V4FailureClass,
        V4FailureDisposition,
        V4FailureEvidenceV1,
        V4HomeStatus,
        V4RecoveryStatus,
    )

    home_calls: list[str] = []
    receipt = safe_home_then_resume_for_recoverable_failures_only(
        failure=V4FailureEvidenceV1(
            failure_class=V4FailureClass.PROTECTIVE_STOP,
            reason="protective stop asserted",
            home_permitted=False,
            protective_stop=True,
            safety_fault=True,
        ),
        current_attempt_ordinal=3,
        attempt_count=3,
        prior_epoch=2,
        fresh_epoch=None,
        fresh_readiness=False,
        dispatch_in_flight=False,
        home_action=lambda: home_calls.append("must-not-run") or {"home_verified": True},
    )
    assert home_calls == []
    assert receipt.disposition is V4FailureDisposition.HARD_TERMINAL
    assert receipt.status is V4RecoveryStatus.HOME_BLOCKED
    assert receipt.home_status is V4HomeStatus.BLOCKED


def test_recovery_rejects_stale_resident_epoch_before_resume() -> None:
    from step5d_autotune_v4_r013.recovery import (
        safe_home_then_resume_for_recoverable_failures_only,
    )
    from step5d_autotune_v4_r013.v4_two_stage_campaign import (
        V4FailureClass,
        V4FailureDisposition,
        V4FailureEvidenceV1,
        V4RecoveryStatus,
    )

    receipt = safe_home_then_resume_for_recoverable_failures_only(
        failure=V4FailureEvidenceV1(
            failure_class=V4FailureClass.TRANSPORT,
            reason="transport boundary closed",
            home_permitted=False,
            home_verified=True,
        ),
        current_attempt_ordinal=4,
        attempt_count=4,
        prior_epoch=9,
        fresh_epoch=9,
        fresh_readiness=True,
        dispatch_in_flight=False,
    )
    assert receipt.disposition is V4FailureDisposition.RECOVERABLE_RESUME
    assert receipt.status is V4RecoveryStatus.STALE_EPOCH
    assert receipt.auto_dispatch_permitted is False
    assert receipt.next_attempt_ordinal == 5


def test_recovery_rejects_duplicate_dispatch_identity() -> None:
    from step5d_autotune_v4_r013.recovery import (
        safe_home_then_resume_for_recoverable_failures_only,
    )
    from step5d_autotune_v4_r013.v4_two_stage_campaign import (
        V4FailureClass,
        V4FailureEvidenceV1,
        V4RecoveryStatus,
    )

    receipt = safe_home_then_resume_for_recoverable_failures_only(
        failure=V4FailureEvidenceV1(
            failure_class=V4FailureClass.HOST_BOUNDARY,
            reason="host boundary failure",
            home_permitted=False,
            home_verified=True,
        ),
        current_attempt_ordinal=5,
        attempt_count=5,
        prior_epoch=3,
        fresh_epoch=4,
        fresh_readiness=True,
        dispatch_in_flight=False,
        dispatch_id="dispatch-5",
        dispatched_ids=("dispatch-5",),
    )
    assert receipt.status is V4RecoveryStatus.DUPLICATE_DISPATCH
    assert receipt.auto_dispatch_permitted is False


def test_fixture_stage_ledger_continues_from_attempt_count_not_ordinal_one(
    tmp_path: Path,
) -> None:
    from step5d_autotune_v4_r013.v4_two_stage_campaign import V4StageCampaignV1

    config, _stage_b = _config()
    path = tmp_path / "fixture-stage.jsonl"
    fixture = V4StageCampaignV1(
        config=config,
        seed_candidate=HISTORICAL_COORDINATE,
        fingerprint_sha256=config.fingerprint_sha256,
        ledger_path=path,
    )
    for _ in range(4):
        fixture.ask()
        fixture.record_ineligible("fixture-closed")
    prefix = path.read_bytes()
    resumed = V4StageCampaignV1.resume(
        config=config,
        seed_candidate=HISTORICAL_COORDINATE,
        fingerprint_sha256=config.fingerprint_sha256,
        ledger_path=path,
    )
    next_attempt = resumed.ask()
    assert next_attempt.ordinal == 5
    assert path.read_bytes().startswith(prefix)
