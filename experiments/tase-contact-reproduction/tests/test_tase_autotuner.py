import json
from pathlib import Path

from tase_autotuner import DEFAULT_CONFIG, run_campaign


def test_plan_is_complete_and_does_not_execute_hardware(tmp_path: Path):
    summary = run_campaign(DEFAULT_CONFIG, tmp_path / "campaign", execute=False)
    assert summary["attempts"] == 24
    assert summary["complete_paths"] == 0
    candidates = sorted((tmp_path / "campaign" / "candidates").glob("*.json"))
    assert len(candidates) == 24
    first = json.loads(candidates[0].read_text())
    assert first["schema"] == "tase.outer-parameters-v1"
    assert first["Md_scalar"] == 12.0
    assert first["Bd_scalar"] == 550.0


def test_config_is_frozen_to_outer_mass_and_damping():
    payload = json.loads(DEFAULT_CONFIG.read_text())
    assert payload["tuned_parameters"] == ["Md_scalar", "Bd_scalar"]
    assert payload["protection_parameters_frozen"] is True
    assert payload["integral_policy_frozen"] is True
