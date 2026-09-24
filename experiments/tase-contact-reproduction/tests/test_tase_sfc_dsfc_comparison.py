from pathlib import Path
import gzip
import json
import sys

import numpy as np
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
    assert replay["collision_policy"]["candidate_coverage_complete"] is True
    assert replay["collision_policy"]["collision_intents_dispatched_as_qdot"] is False
    assert replay["collision_policy"]["candidate_scenario_replay_counts"] == {
        method: {scenario: 1 for scenario in comparison.SCENARIOS}
        for method in comparison.METHODS
    }
    for offset in range(0, len(replay["results"]), 2):
        pair = replay["results"][offset:offset + 2]
        assert pair[0]["common_input_sha256"] == pair[1]["common_input_sha256"]
        assert {item["method"] for item in pair} == set(comparison.METHODS)
        assert pair[0]["scenario"] == pair[1]["scenario"]
        for result in pair:
            assert result["final_realization_calls"] == [1]
            assert result["tangential_normal_leak_max_m_s"] < 1e-12
            assert result["joint_velocity_bound_violation_ticks"] == 0
            assert result["max_abs_joint_velocity_rad_s"] <= 0.05 + 1e-8
            assert result["max_final_jqdot_residual_norm_m_s_rad_s"] >= 0.0
            intents = result["collision_intents"]
            assert intents["tool_contact"]["tangent_frozen"] is True
            assert intents["qualified_link_contact_fixture"]["normal_intent_error_m_s"] < 1e-12
            assert intents["qualified_link_contact_fixture"]["orientation_intent_error_rad_s"] < 1e-12
            assert intents["qualified_link_contact_fixture"]["joint_yield_preference_rad_s"] is not None
            assert intents["qualified_link_contact_fixture"]["command_authority"] == "offline_intent_only"
            assert intents["loaded_out_of_corridor_fixture"]["recovery_requested"] is True
            assert intents["loaded_out_of_corridor_fixture"]["recovery_owner_required"] is True
    report_path = command_replay.refresh_final_report(comparison_dir=tmp_path / "sfc-dsfc")
    final_report = report_path.read_text(encoding="utf-8")
    assert "joint-bound-hit ticks (%)" in final_report
    assert "Same-input command replay and dual-space intent" in final_report
    assert "Historical trace replay boundary" in final_report


def test_fixed_home_jacobian_transform_preserves_model_feedback_and_is_separate():
    row = {
        "time_s": 0.0,
        "dt_s": 0.002,
        "inputs": {
            "state_age_s": 0.01,
            "tcp_position_base_m": [0.0, 0.0, 0.0],
            "reference_position_base_m": [0.01, -0.02, 0.0],
            "reference_velocity_base_m_s": [0.004, 0.002, 0.0],
            "force_target_n": 5.0,
            "jacobian": np.eye(6).tolist(),
            "joint_velocity_lower_rad_s": [-0.05] * 6,
            "joint_velocity_upper_rad_s": [0.05] * 6,
            "raw_force_base_n": [0.0, 0.0, 5.0],
            "estimated_outward_normal_base": [0.0, 0.0, 1.0],
        },
        "output": {"realized_jqdot_m_s_rad_s": [0.0] * 6},
    }
    source_digest = command_replay._replay_input_digest([row])
    profile = {
        "profile_id": "test-fixed-home-jacobian",
        "home_q_rad": [0.1] * 6,
        "home_position_m": [0.4, 0.2, 0.03],
        "home_rotation_base": np.diag([1.0, -1.0, -1.0]).tolist(),
        "tcp_jacobian_base": (2.0 * np.eye(6)).tolist(),
    }

    transformed_left = command_replay._transform_trace_fixed_home_jacobian([row], profile)
    transformed_right = command_replay._transform_trace_fixed_home_jacobian([row], profile)

    assert row["inputs"]["jacobian"] == np.eye(6).tolist()
    assert transformed_left == transformed_right
    inputs = transformed_left[0]["inputs"]
    assert inputs["jacobian"] == (2.0 * np.eye(6)).tolist()
    assert inputs["joint_position_rad"] == [0.1] * 6
    assert inputs["tcp_position_base_m"] == pytest.approx([0.4, 0.2, 0.03])
    assert inputs["reference_position_base_m"] == pytest.approx([0.41, 0.18, 0.03])
    assert inputs["raw_force_base_n"] == [0.0, 0.0, 5.0]
    assert inputs["reference_velocity_base_m_s"] == [0.004, 0.002, 0.0]
    assert command_replay._replay_input_digest(transformed_left) != source_digest
    assert command_replay._replay_input_digest(transformed_left) == command_replay._replay_input_digest(transformed_right)
    observation, _reference = command_replay._observation(transformed_left[0], np.zeros(6))
    assert np.asarray(observation["jacobian"]) == pytest.approx(2.0 * np.eye(6))
    assert np.asarray(observation["rotation"]) == pytest.approx(np.diag([1.0, -1.0, -1.0]))


def test_historical_audit_covers_all_selected_stream_families_and_keeps_reconstruction_separate(tmp_path):
    runs = tmp_path / "runs"
    comparison_dir = runs / "sfc-dsfc"
    attempt = runs / "tase-resident-sample" / "attempts" / "0001"
    attempt.mkdir(parents=True)
    (attempt / "raw_sensor.jsonl").write_text(
        json.dumps({"corrected_wrench_n_nm": [0] * 6, "packet_sequence": 1}) + "\n",
        encoding="utf-8",
    )
    (attempt / "robot_frames.jsonl").write_text(
        json.dumps({"q_rad": [0] * 6, "tcp_pose_m_rad": [0] * 6, "consumed_packet_sequence": 1}) + "\n",
        encoding="utf-8",
    )
    (attempt / "published_packets.jsonl").write_text(
        json.dumps([1.0, {"sequence": 1, "double_values": [0] * 24, "integer_values": [0] * 9}]) + "\n",
        encoding="utf-8",
    )
    (attempt / "command_timeline.jsonl").write_text("", encoding="utf-8")
    (attempt / "attempt-result.json").write_text("{}\n", encoding="utf-8")
    (attempt / "seal.json").write_text("{}\n", encoding="utf-8")
    unrelated = runs / "unrelated-run" / "attempts" / "0001"
    unrelated.mkdir(parents=True)
    (unrelated / "raw_sensor.jsonl").write_text(
        json.dumps({"jacobian": [[0] * 6] * 6}) + "\n", encoding="utf-8"
    )

    audit = command_replay._audit_historical_recordings(comparison_dir)
    assert audit["run_directories"] == 1
    assert audit["stream_counts"] == {
        "raw_sensor": 1,
        "robot_frames": 1,
        "published_packets": 1,
        "command_timeline": 1,
        "attempt_results": 1,
        "seals": 1,
    }
    assert audit["same_input_alternate_controller_replay_eligible"] is False
    missing = set(audit["fields_not_stored_under_required_names_in_sensor_or_rtde_first_records"])
    assert {"jacobian", "reference_position_base_m", "reference_velocity_base_m_s"} <= missing
    rendered = command_replay._render_historical_audit(audit)
    assert "1 raw-sensor, 1 RTDE robot-frame, and 1 published-packet streams" in rendered
    assert "reconstructed inputs" in rendered
