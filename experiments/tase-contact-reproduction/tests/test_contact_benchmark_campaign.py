from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from contact_benchmark_campaign import CampaignConfig, run_equal_budget_campaign  # noqa: E402
from contact_benchmark_protocol import CONTROLLERS  # noqa: E402


def test_equal_budget_campaign_freezes_all_six_and_keeps_holdout_out_of_ledger(tmp_path):
    result = run_equal_budget_campaign(
        experiment_root=ROOT,
        output_dir=tmp_path / "campaign",
        config=CampaignConfig(horizon_ticks=32, seed=20260918),
    )
    assert result["controllers"] == list(CONTROLLERS)
    assert result["rpsfc_selectable"] is False
    assert result["freeze_sha256"]
    assert all(
        result["progress"][controller] == {
            "used_units": 24,
            "completed_trials": 48,
            "failed_trials": 0,
        }
        for controller in CONTROLLERS
    )
    assert all(result["holdout"][controller]["trial_count"] == 25 for controller in CONTROLLERS)
    assert all(result["holdout"][controller]["complete_count"] == 25 for controller in CONTROLLERS)
    sensitivity = (tmp_path / "campaign" / "freshness-sensitivity.json").read_text(encoding="utf-8")
    assert '"cutoff_s": 0.02' in sensitivity
