import json
from pathlib import Path

from tase_autotuner import (
    DEFAULT_CONFIG,
    _extract_mae,
    _gp_posterior,
    _next_bo_candidate,
    run_campaign,
)


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


def test_gp_posterior_uses_observation_noise_and_returns_finite_variance():
    config = json.loads(DEFAULT_CONFIG.read_text())
    import numpy as np
    x = np.array([[12.0, 550.0], [8.0, 400.0]], dtype=float)
    y = np.array([1.0, 2.0], dtype=float)
    q = np.array([[12.0, 550.0], [16.0, 700.0]], dtype=float)
    mean, variance = _gp_posterior(config, x, y, q)
    assert mean.shape == (2,)
    assert variance.shape == (2,)
    assert np.isfinite(mean).all()
    assert (np.isfinite(variance) & (variance > 0.0)).all()


def test_bo_proposal_deduplicates_complete_and_failed_records():
    config = json.loads(DEFAULT_CONFIG.read_text())
    records = [
        {"status": "complete", "mae_n": 1.0, "Md_scalar": 12.0, "Bd_scalar": 550.0},
        {"status": "failed", "mae_n": None, "Md_scalar": 12.0, "Bd_scalar": 550.0},
    ]
    candidate = _next_bo_candidate(config, records, index=0)
    assert (candidate.Md_scalar, candidate.Bd_scalar) != (12.0, 550.0)


def test_extract_mae_accepts_sealed_live_full_path_receipt(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    metrics = {
        "full_path_bin_count": 629,
        "required_full_path_bin_count": 629,
        "path_duration_s": 62.8319999999,
        "full_force_metric_duration_s": 62.8318530718,
        "full_force_mae_n": 1.25,
        "timing_gate_passed": True,
        "timing_evidence": {"successful": True},
    }
    receipt = {
        "command": "pilot",
        "evidence_eligible": True,
        "live_path": {"kind": "full_period"},
        "evidence_metrics": metrics,
        "stop": {"observed_stationary": True},
        "attempts": [{
            "phase": "pilot",
            "evidence": {
                "return_gate_passed": True,
                "home_proof": {"fixed_home_route": True, "stationary": True},
                "metrics": metrics,
            },
        }],
    }
    (run_dir / "dispatch_receipt.json").write_text(json.dumps(receipt))

    mae, evidence = _extract_mae(run_dir)

    assert mae == 1.25
    assert evidence["complete_path"] is True
    assert evidence["home_verified"] is True


def test_extract_mae_rejects_short_live_path_even_with_mae(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    metrics = {
        "full_path_bin_count": 628,
        "required_full_path_bin_count": 629,
        "path_duration_s": 62.7,
        "full_force_metric_duration_s": 62.7,
        "full_force_mae_n": 1.25,
        "timing_gate_passed": True,
        "timing_evidence": {"successful": True},
    }
    receipt = {
        "command": "pilot",
        "evidence_eligible": True,
        "live_path": {"kind": "full_period"},
        "evidence_metrics": metrics,
        "stop": {"observed_stationary": True},
        "attempts": [{"phase": "pilot", "evidence": {"metrics": metrics}}],
    }
    (run_dir / "dispatch_receipt.json").write_text(json.dumps(receipt))

    mae, evidence = _extract_mae(run_dir)

    assert mae is None
    assert evidence["complete_path"] is False


def test_extract_mae_reads_home_recovery_from_supervisor_receipt(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "dispatch_receipt.json").write_text(json.dumps({
        "command": "pilot",
        "evidence_eligible": False,
        "live_path": {"kind": "full_period"},
        "error": "path failed",
        "stop": {"observed_stationary": True},
    }))
    (run_dir / "supervisor-result.json").write_text(json.dumps({
        "success": False,
        "autonomous_home_recovery": {"success": True, "state": "HOME_RECOVERED"},
    }))

    _, evidence = _extract_mae(run_dir)

    assert evidence["home_verified"] is True
    assert evidence["recovery"]["state"] == "HOME_RECOVERED"
