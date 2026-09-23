from pathlib import Path
import gzip
import json
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import tase_research_campaign as research  # noqa: E402
import tase_sfc_dsfc_comparison as comparison  # noqa: E402
import tase_sfc_dsfc_replay as command_replay  # noqa: E402
from contact_method_registry import RegistryError, default_registry  # noqa: E402
from contact_yield_method_registry import load_frozen_native_tangent_composition_records  # noqa: E402
from tase_sfc_fusion import NATIVE_DSFC_COMPOSITION_ID, NATIVE_SFC_COMPOSITION_ID, FusionError, TaseSfcFusion  # noqa: E402


def test_frozen_native_profiles_use_a_incumbent_and_distinct_law_identities():
    profile = comparison._load_frozen_profiles()
    outer = profile["normal_outer_config"]
    assert outer["Md_scalar"] == pytest.approx(9.565272137974492)
    assert outer["Bd_scalar"] == pytest.approx(693.6559295653944)
    assert outer["force_integral_limit_n_s"] == 1.0
    assert outer["force_target_n"] == 5.0
    assert comparison.METHODS == (
        "TASE_RNN_MATURE+SFC_YIELD_V1",
        "TASE_RNN_MATURE+DSFC_YIELD_V1",
    )
    assert comparison._candidate(profile, "SFC")["tangential_parameters"] == profile["tangential_parameters"]["SFC"]
    assert comparison._candidate(profile, "DSFC")["tangential_parameters"] == profile["tangential_parameters"]["DSFC"]
    records = load_frozen_native_tangent_composition_records()
    assert set(records) == set(comparison.METHODS)
    assert all(record.live_eligible is False for record in records.values())
    assert records[comparison.METHODS[0]].tangential_controller == "SFC_YIELD_V1"
    assert records[comparison.METHODS[1]].tangential_controller == "DSFC_YIELD_V1"


@pytest.mark.parametrize("scenario", comparison.SCENARIOS)
def test_sfc_and_dsfc_share_normal_inputs_bounds_and_one_final_realizer(scenario):
    profile = comparison._load_frozen_profiles()
    config = research.CampaignConfig(
        horizon_ticks=16,
        seed=131,
        initial_units=1,
        bo_units=1,
        repeat_units=1,
        holdout_rounds=1,
    )
    paired = []
    trial_key = f"same-seed:{scenario}"
    for method, law in zip(comparison.METHODS, ("SFC", "DSFC")):
        result = research.run_attempt(
            method=method,
            candidate=comparison._candidate(profile, law),
            case_name=scenario,
            attempt_id=f"focused-{method}-{scenario}",
            config=config,
            qp_library=Path("."),
            trial_key=trial_key,
            include_trace=True,
        )
        assert result["metrics"]["failed"] is False
        assert result["proxy_only"] is True
        assert result["surface_geometry_provided_to_controller"] is False
        assert len(result["trace_rows"]) == config.horizon_ticks
        composition = result["composition_summary"]
        assert composition["normal_solver"] == "TASE_RNN_MATURE"
        assert composition["final_realizer"] == "rnn"
        assert composition["final_realization_calls_per_tick"] == [1]
        assert composition["tangential_normal_leak_max_m_s"] < 1e-12
        assert composition["tangential_law"] == law
        assert composition["tangential_parameters"] == profile["tangential_parameters"][law]
        paired.append(result)

    # Same initial measured sample, observer settings and reference; later
    # closed-loop samples may diverge as each method changes the proxy state.
    assert paired[0]["trace_rows"][0]["inputs"] == paired[1]["trace_rows"][0]["inputs"]
    assert paired[0]["normal_observer"]["profile_id"] == paired[1]["normal_observer"]["profile_id"]
    assert paired[0]["normal_observer"]["parameters"] == paired[1]["normal_observer"]["parameters"]


def test_registry_rejects_unfrozen_native_parameter_values():
    profile = comparison._load_frozen_profiles()
    candidate = comparison._candidate(profile, "DSFC")
    candidate["tangential_parameters"]["g"] += 0.01
    with pytest.raises(RegistryError, match="frozen profile"):
        default_registry().initialize(
            comparison.METHODS[1],
            config=candidate,
            outer_config=profile["normal_outer_config"],
        )


@pytest.mark.parametrize(
    ("law", "composition_id"),
    (("SFC", NATIVE_SFC_COMPOSITION_ID), ("DSFC", NATIVE_DSFC_COMPOSITION_ID)),
)
def test_nested_fusion_snapshot_keeps_the_native_law_identity_distinct(law, composition_id):
    profile = comparison._load_frozen_profiles()
    method = comparison.METHODS[0] if law == "SFC" else comparison.METHODS[1]
    handle = default_registry().initialize(
        method,
        config=comparison._candidate(profile, law),
        outer_config=profile["normal_outer_config"],
    )
    state = handle.backend.snapshot()
    assert state["composition_id"] == composition_id
    assert state["fusion"]["composition_id"] == composition_id
    other = TaseSfcFusion((0.0, 0.0, 1.0), composition_id=composition_id)
    other.restore(state["fusion"])
    mismatched = TaseSfcFusion((0.0, 0.0, 1.0))
    with pytest.raises(FusionError, match="identity"):
        mismatched.restore(state["fusion"])
    handle.close()


def test_small_campaign_seals_model_only_traces_and_denominators(tmp_path):
    summary = comparison.run_comparison(
        output_dir=tmp_path / "sfc-dsfc",
        blocks=1,
        horizon_ticks=8,
        seed=82,
    )
    assert summary["physical_evidence"] is False
    assert summary["attempts"] == summary["planned_attempts"] == 6
    assert summary["failed_attempts"] == 0
    assert summary["protocol"]["simulated_duration_s"] == pytest.approx(0.016)
    assert summary["protocol"]["note"].startswith("closed-loop simulated")
    report = (tmp_path / "sfc-dsfc" / "report.md").read_text(encoding="utf-8")
    assert "offline closed-loop model proxy only" in report
    assert "does not qualify either combination for live use" in report
    attempts = [
        json.loads(line)
        for line in (tmp_path / "sfc-dsfc" / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(attempts) == 6
    for attempt in attempts:
        trace_path = tmp_path / "sfc-dsfc" / attempt["trace_path"]
        with gzip.open(trace_path, "rt", encoding="utf-8") as stream:
            trace = [json.loads(line) for line in stream]
        assert len(trace) == 8
        assert "model_true_normal_base" in trace[0]["scoring_only_model_state"]
        assert "raw_force_base_n" in trace[0]["inputs"]

    replay = command_replay.run_replay(comparison_dir=tmp_path / "sfc-dsfc")
    assert replay["physical_evidence"] is False
    assert replay["same_input_replay_units"] == 3
    assert replay["controller_replays"] == 6
    assert replay["collision_policy"]["physical_observer_qualified"] is False
    assert replay["collision_policy"]["visual_corridor_qualified"] is False
    for offset in range(0, len(replay["results"]), 2):
        pair = replay["results"][offset:offset + 2]
        assert pair[0]["common_input_sha256"] == pair[1]["common_input_sha256"]
        for result in pair:
            assert result["final_realization_calls"] == [1]
            assert result["tangential_normal_leak_max_m_s"] < 1e-12
            intents = result["collision_intents"]
            assert intents["tool_contact"]["tangent_frozen"] is True
            assert intents["qualified_link_contact_fixture"]["normal_intent_error_m_s"] < 1e-12
            assert intents["qualified_link_contact_fixture"]["orientation_intent_error_rad_s"] < 1e-12
            assert intents["loaded_out_of_corridor_fixture"]["recovery_requested"] is True
            assert intents["loaded_out_of_corridor_fixture"]["recovery_owner_required"] is True
    report_path = command_replay.refresh_final_report(comparison_dir=tmp_path / "sfc-dsfc")
    final_report = report_path.read_text(encoding="utf-8")
    assert "joint-bound-hit ticks (%)" in final_report
    assert "Same-input command replay and dual-space intent" in final_report
    assert "Historical trace replay boundary" in final_report
