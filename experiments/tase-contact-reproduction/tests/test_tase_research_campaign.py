from pathlib import Path
import random
import sys
import hashlib

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_contact_qp import build  # noqa: E402
import tase_research_campaign as campaign  # noqa: E402


@pytest.fixture(scope="module")
def qp_library(tmp_path_factory):
    return build(tmp_path_factory.mktemp("tase-research-qp"))


def test_equal_budget_campaign_preserves_failures_and_paired_ci(tmp_path, qp_library):
    config = campaign.CampaignConfig(
        horizon_ticks=8,
        initial_units=1,
        bo_units=1,
        repeat_units=1,
        holdout_rounds=1,
    )
    summary = campaign.run_campaign(
        output_dir=tmp_path / "campaign",
        qp_library=qp_library,
        config=config,
    )
    assert summary["schema"] == campaign.SCHEMA
    assert summary["protocol"]["budget"] == {
        "initial": 1,
        "bo": 1,
        "repeat": 1,
        "holdout_rounds": 1,
    }
    assert summary["attempt_denominators"]["tuning"] == len(campaign.METHODS) * 3 * len(campaign.CASES)
    assert summary["attempt_denominators"]["holdout"] == len(campaign.METHODS) * 4
    unavailable_compositions = 2  # LAC/NAC tangential adapters have no common-realizer implementation.
    assert summary["attempt_denominators"]["failed_tuning"] >= unavailable_compositions * 3 * len(campaign.CASES)
    assert summary["attempt_denominators"]["failed_holdout"] >= unavailable_compositions * 4
    assert (tmp_path / "campaign" / "attempts.jsonl").is_file()
    assert (tmp_path / "campaign" / "holdout.jsonl").is_file()
    assert (tmp_path / "campaign" / "summary.json").is_file()
    for name in ("attempts.jsonl", "holdout.jsonl"):
        digest = hashlib.sha256((tmp_path / "campaign" / name).read_bytes()).hexdigest()
        assert summary["artifacts"][name.removesuffix(".jsonl") + "_sha256"] == digest
    for method, ci in summary["paired_vs_primary"].items():
        assert ci["improvement_threshold_n"] == pytest.approx(0.10)
        assert ci["supported_improvement"] is False or ci["upper"] <= -0.10
    assert summary["protocol"]["bo_acquisition"].startswith("history-dependent")
    assert summary["protocol"]["paired_holdout_seed_contract"] == "holdout:block:case shared across methods"


def test_qp_and_improved_have_same_input_contract(qp_library):
    config = campaign.CampaignConfig(horizon_ticks=8)
    for method in ("TASE_QP", "TASE_IMPROVED"):
        result = campaign.run_attempt(
            method=method,
            candidate=campaign._candidate(method, 0),
            case_name="plane",
            attempt_id=f"contract-{method}",
            config=config,
            qp_library=qp_library,
        )
        assert result["surface_geometry_provided_to_controller"] is False
        assert result["metrics"]["failed"] is False
        assert result["metrics"]["timing_dt_s"]["min"] == pytest.approx(0.002)
        assert result["metrics"]["realization_residual_max"] >= 0.0


def test_tase_research_uses_rnn_best_outer_profile_instead_of_12_550():
    profile = campaign._candidate("TASE_QP", 0)["normal_outer_config"]
    assert profile["Md_scalar"] == pytest.approx(9.565272137974492)
    assert profile["Bd_scalar"] == pytest.approx(693.6559295653944)
    assert profile["force_integral_limit_n_s"] == pytest.approx(0.1)
    assert profile["force_integral_policy"] == "legacy-clamp-v1"
    assert profile["Md_scalar"] != 12.0
    assert profile["Bd_scalar"] != 550.0


@pytest.mark.parametrize("case_name", campaign.CASES)
def test_rnn_sfc_composition_runs_all_unknown_surface_proxies(case_name, qp_library):
    method = "TASE_RNN_MATURE+SFC"
    config = campaign.CampaignConfig(horizon_ticks=16)
    result = campaign.run_attempt(
        method=method,
        candidate=campaign._candidate(method, 0),
        case_name=case_name,
        attempt_id=f"contract-{method}",
        config=config,
        qp_library=qp_library,
    )
    assert result["proxy_only"] is True
    assert result["surface_geometry_provided_to_controller"] is False
    assert result["metrics"]["failed"] is False
    summary = result["composition_summary"]
    assert summary["composition_id"] == "TASE_NORMAL_ORIENTATION+SFC_TANGENTIAL_V1"
    assert summary["final_realization_calls_per_tick"] == [1]
    assert summary["sfc_active_ticks"] == 16
    assert summary["sfc_normal_leak_max_m_s"] < 1e-12
    assert summary["sfc_parameters"] == result["candidate"]["tangential_parameters"]
    assert summary["normal_solver"] == method.split("+", 1)[0]
    assert summary["final_realizer"] == "rnn"
    assert result["normal_observer"]["profile_id"] == "yield_normal_observer_v3"
    if case_name == "incline":
        assert summary["tangential_force_input_max_n"] > 0.1
    assert result["candidate"]["outer_profile_id"] == "figure8-rate400-integral-0p1-rnn-incumbent"
    assert result["candidate"]["normal_outer_config"]["Md_scalar"] == pytest.approx(9.565272137974492)


@pytest.mark.parametrize("case_name", campaign.CASES)
def test_qp_sfc_runs_all_unknown_surface_proxies_with_shared_online_normal(case_name, qp_library):
    config = campaign.CampaignConfig(horizon_ticks=16)
    results = {}
    for method in ("TASE_QP", "TASE_QP+SFC"):
        results[method] = campaign.run_attempt(
            method=method,
            candidate=campaign._candidate(method, 0),
            case_name=case_name,
            attempt_id=f"qp-case-{case_name}-{method}",
            trial_key=f"paired-qp-sfc:{case_name}",
            config=config,
            qp_library=qp_library,
        )
    base = results["TASE_QP"]["metrics"]
    fused = results["TASE_QP+SFC"]["metrics"]
    assert base["failed"] is False
    assert fused["failed"] is False
    composition = results["TASE_QP+SFC"]["composition_summary"]
    assert composition["final_realizer"] == "qp"
    assert composition["final_realization_calls_per_tick"] == [1]
    assert composition["sfc_active_ticks"] == 16
    base_observer = results["TASE_QP"]["normal_observer"]
    fused_observer = results["TASE_QP+SFC"]["normal_observer"]
    assert fused_observer["profile_id"] == base_observer["profile_id"] == "yield_normal_observer_v3"
    assert fused_observer["parameters"] == base_observer["parameters"]
    if case_name == "incline":
        assert composition["tangential_force_input_max_n"] > 0.1


def test_sfc_composition_calls_selected_solver_once_and_rolls_back_all_state(qp_library, monkeypatch):
    from contact_method_registry import default_registry

    method = "TASE_QP+SFC"
    handle = default_registry().initialize(
        method,
        config=campaign._candidate(method, 0),
        qp_library=qp_library,
    )
    backend = handle.backend
    original_step = backend.tase.solver.step
    call_count = 0

    def counted_step(sample):
        nonlocal call_count
        call_count += 1
        return original_step(sample)

    monkeypatch.setattr(backend.tase.solver, "step", counted_step)
    observation, reference = campaign._make_observation(
        case=campaign.SURFACE_CASES["plane"],
        position=np.zeros(3),
        normal=np.asarray((0.0, 0.0, 1.0)),
        force_n=5.0,
        index=0,
        horizon=8,
        dt_s=campaign.DT_S,
        stiffness=120.0,
        rng=random.Random(17),
    )
    reference["phase"] = "path"
    result = handle.step(observation, reference, campaign.DT_S)
    assert call_count == 1
    assert result["diagnostics"]["composition"]["final_realization_calls"] == 1

    before_failure = handle.snapshot()

    def reject_after_composition(_sample):
        raise RuntimeError("injected solver rejection")

    monkeypatch.setattr(backend.tase.solver, "step", reject_after_composition)
    observation["time_s"] = campaign.DT_S
    with pytest.raises(RuntimeError, match="injected solver rejection"):
        handle.step(observation, reference, campaign.DT_S)
    assert handle.snapshot() == before_failure
    handle.close()
