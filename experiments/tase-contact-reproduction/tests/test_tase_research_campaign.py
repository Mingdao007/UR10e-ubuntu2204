from pathlib import Path
import sys
import hashlib

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
    assert summary["attempt_denominators"]["failed_tuning"] >= len(campaign.COMPOSITION_METHODS) * 3 * len(campaign.CASES)
    assert summary["attempt_denominators"]["failed_holdout"] >= len(campaign.COMPOSITION_METHODS) * 4
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
