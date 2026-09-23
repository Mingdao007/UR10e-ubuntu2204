from __future__ import annotations

import json

import pytest

from tase_integral_db_confirmation import build_schedule, prepare_campaign, score_campaign


def test_schedule_is_reproducible_balanced_and_matches_effective_caps():
    first = build_schedule()
    second = build_schedule()
    assert first == second
    assert build_schedule(seed=20260924)["seed"] == 20260924
    assert len(first["rows"]) == 10
    assert first["effective_integral_cap_n_s"] == 0.5
    assert first["old_d_rows_included"] is False
    assert first["blocks_starting_with_b"] in (2, 3)
    for block in range(5):
        rows = [row for row in first["rows"] if row["block"] == block]
        assert {row["arm_id"] for row in rows} == {"B", "D"}
        assert len({row["position"] for row in rows}) == 2
        b = next(row for row in rows if row["arm_id"] == "B")
        d = next(row for row in rows if row["arm_id"] == "D")
        assert b["frozen"]["force_integral_limit_n_s"] == 0.5
        assert b["frozen"]["force_integral_policy"] == "legacy-clamp-v1"
        assert d["frozen"]["force_integral_limit_n_s"] == 1.0
        assert d["frozen"]["force_integral_authority_error_n"] == 0.5
        assert d["frozen"]["force_integral_policy"] == "conditional-double-clamp-v1"


def _write_complete_attempt(path, row, mae, *, eligible=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    policy = row["frozen"]["force_integral_policy"]
    limit = row["frozen"]["force_integral_limit_n_s"]
    authority = row["frozen"]["force_integral_authority_error_n"]
    path.with_name("seal.json").write_text("{}\n", encoding="utf-8")
    path.write_text(json.dumps({
        "physical_dispatched": True,
        "evidence_eligible": eligible,
        "parameter_binding": {
            "candidate_id": row["candidate_id"],
            "protocol_id": row["protocol_id"],
            "duration_token": row["duration_token"],
            "Md_scalar": row["Md_scalar"],
            "Bd_scalar": row["Bd_scalar"],
            "force_integral_policy": policy,
            "force_integral_limit_n_s": limit,
        },
        "applied_runtime_parameters": {
            "Md_scalar": row["Md_scalar"],
            "Bd_scalar": row["Bd_scalar"],
            "force_integral_policy": policy,
            "force_integral_limit_n_s": limit,
            "force_integral_authority_error_n": authority,
        },
        "lifecycle": {"path_complete": True, "home_verified": True, "ready_for_next": True},
        "evidence": {"metrics": {
            "complete_bins": 550,
            "path_duration_s": 60.0,
            "protocol_id": row["protocol_id"],
            "formal_window_s": [5.0, 60.0],
            "timing_gate_passed": eligible,
            "timing_evidence": {"acceptance_protocol_id": row["protocol_id"],
                                "layer_rates_hz": {"tp_consumed_packet_echoes": 475.0}},
            "normal_force_mae_n": mae,
        }},
    }, indent=2) + "\n", encoding="utf-8")


def test_prepare_and_score_retains_failures_and_computes_corrected_pairs(tmp_path):
    campaign = prepare_campaign(tmp_path / "db-followup")
    schedule = json.loads((campaign / "screening-schedule.json").read_text())
    assert len(schedule["candidate_files"]) == 10
    session = campaign / "session-01"
    all_rows = schedule["rows"]
    # One B cell is deliberately ineligible; it must remain in the denominator
    # without becoming a successful MAE or contaminating the paired estimate.
    for row in all_rows:
        mae = 1.4 if row["arm_id"] == "B" else 1.2
        path = session / "attempts" / f'{row["ordinal"]:04d}' / "attempt-result.json"
        _write_complete_attempt(path, row, mae, eligible=not (row["block"] == 0 and row["arm_id"] == "B"))
    (session / "dispatch_receipt.json").write_text(json.dumps({
        "attempts": [{} for _ in all_rows],
        "stop": {"home_verified": True, "program_stopped": True},
    }), encoding="utf-8")
    result = score_campaign(campaign)
    assert result["attempted"] == 10
    assert result["complete"] == 9
    assert result["failed"] == 1
    assert result["complete_pairs"] == 4
    assert result["mean_D_improvement_vs_B_n"] == pytest.approx(0.2)
    assert result["improvement_supported"] is True
    assert result["status"] == "complete"


def test_prepare_rejects_existing_campaign_directory(tmp_path):
    campaign = tmp_path / "existing"
    campaign.mkdir()
    try:
        prepare_campaign(campaign)
    except FileExistsError:
        pass
    else:
        raise AssertionError("campaign path collision must not overwrite prior evidence")
