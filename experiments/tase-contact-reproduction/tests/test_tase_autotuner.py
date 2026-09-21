import json
from pathlib import Path

import pytest

from tase_autotuner import (
    DEFAULT_CONFIG,
    _extract_mae,
    _expected_improvement,
    _gp_posterior,
    _is_preflight_only_failure,
    _needs_recovery_pause,
    _next_bo_candidate,
    run_campaign,
    run_confirmation,
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


def test_gp_posterior_restores_observation_mean_and_ei_is_nonnegative():
    config = json.loads(DEFAULT_CONFIG.read_text())
    import numpy as np
    x = np.array([[12.0, 550.0], [12.0, 550.0]], dtype=float)
    y = np.array([1.0, 1.2], dtype=float)
    q = np.array([[0.0, 0.0]], dtype=float)
    mean, variance = _gp_posterior(config, x, y, q)
    assert 1.0 <= mean[0] <= 1.2
    ei = _expected_improvement(mean, variance, incumbent=1.0)
    assert ei.shape == (1,)
    assert ei[0] >= 0.0


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


def test_extract_mae_accepts_only_explicit_r013_60_receipt(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    metrics = {
        "protocol_id": "figure8_window60_r013_compat_v1",
        "complete_bins": 550,
        "required_bins": 550,
        "path_duration_s": 60.0,
        "formal_metric_duration_s": 55.0,
        "normal_force_mae_n": 0.8,
        "complete": True,
        "objective_eligible": True,
        "coverage_complete": True,
        "interrupted": False,
        "timing_gate_passed": True,
        "timing_evidence": {"successful": True},
    }
    receipt = {
        "command": "pilot",
        "evidence_eligible": True,
        "live_path": {
            "kind": "r013_compat_60",
            "path_duration_s": 60.0,
            "protocol_id": "figure8_window60_r013_compat_v1",
        },
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
    assert mae == 0.8
    assert evidence["complete_path"] is True
    assert evidence["protocol_id"] == "figure8_window60_r013_compat_v1"


def test_campaign_rejects_full_period_rows_in_r013_ledger(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    (campaign / "ledger.jsonl").write_text(json.dumps({
        "ordinal": 0,
        "status": "complete",
        "protocol_id": "contact_yield_full_period_v1",
        "duration_token": "full",
        "candidate_id": "old",
        "stage": "initial",
        "index": 0,
        "Md_scalar": 12.0,
        "Bd_scalar": 550.0,
        "mae_n": 0.2,
    }) + "\n")
    with pytest.raises(ValueError, match="non-R013"):
        run_campaign(DEFAULT_CONFIG, campaign, execute=False)


def test_interrupted_started_row_pauses_resume_before_new_candidate(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    (campaign / "ledger.jsonl").write_text(json.dumps({
        "ordinal": 0,
        "status": "started",
        "protocol_id": "figure8_window60_r013_compat_v1",
        "duration_token": "r013_60",
        "candidate_id": "initial-00",
        "stage": "initial",
        "index": 0,
        "Md_scalar": 12.0,
        "Bd_scalar": 550.0,
        "run_dir": str(campaign / "run-00"),
    }) + "\n")
    summary = run_campaign(DEFAULT_CONFIG, campaign, execute=True, resume=True)
    assert summary["state"] == "PAUSED_RECOVERY_BLOCKED"
    assert summary["attempts"] == 1


def test_confirmation_does_not_count_failed_rows_with_mae(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    confirmation = campaign / "confirmation"
    confirmation.mkdir(parents=True)
    (campaign / "summary.json").write_text(json.dumps({
        "state": "COMPLETE",
        "protocol_id": "figure8_window60_r013_compat_v1",
        "best": {"Md_scalar": 12.0, "Bd_scalar": 550.0, "mae_n": 1.0},
    }))
    rows = []
    for round_index in range(5):
        for arm in ("baseline", "incumbent"):
            rows.append({
                "round": round_index,
                "arm": arm,
                "status": "failed",
                "protocol_id": "figure8_window60_r013_compat_v1",
                "duration_token": "r013_60",
                "mae_n": 0.5 if arm == "incumbent" else 1.0,
                "complete_path": True,
                "home_verified": True,
            })
    (confirmation / "ledger.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )
    summary = run_confirmation(DEFAULT_CONFIG, campaign, execute=False)
    assert summary["state"] == "INCOMPLETE"
    assert summary["paired_complete"] == 0
    assert summary["improvement_supported"] is False


def test_confirmation_restart_pauses_failed_row_without_home(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    confirmation = campaign / "confirmation"
    confirmation.mkdir(parents=True)
    (campaign / "summary.json").write_text(json.dumps({
        "state": "COMPLETE",
        "protocol_id": "figure8_window60_r013_compat_v1",
        "best": {"Md_scalar": 12.0, "Bd_scalar": 550.0, "mae_n": 1.0},
    }))
    (confirmation / "ledger.jsonl").write_text(json.dumps({
        "round": 0,
        "arm": "baseline",
        "status": "failed",
        "protocol_id": "figure8_window60_r013_compat_v1",
        "duration_token": "r013_60",
        "home_verified": False,
        "failure": "interrupted_confirmation_recovered_as_failed",
    }) + "\n")
    summary = run_confirmation(DEFAULT_CONFIG, campaign, execute=False)
    assert summary["state"] == "PAUSED_RECOVERY_BLOCKED"


def test_missing_terminal_preflight_proof_is_unknown_recovery_state(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "neutral-hold-receipt.json").write_text(
        json.dumps({"attempt_dispatched": False})
    )
    row = {
        "status": "failed",
        "failure": "owner_failed_or_missing_receipt",
        "preflight_failed": True,
        "run_dir": str(run_dir),
    }
    assert not _is_preflight_only_failure(row)
    assert _needs_recovery_pause(row)
    assert _needs_recovery_pause({
        "status": "failed",
        "failure": "receipt_parse:ValueError",
    })
