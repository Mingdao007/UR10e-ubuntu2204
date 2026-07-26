import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_current_review_policy_defers_one_2_plus_1_gate_until_final_contact() -> None:
    policy = json.loads(
        (ROOT / "config" / "tacdiffusion_review_policy_v3.json").read_text(
            encoding="utf-8"
        )
    )
    assert policy["schema"] == "ur10e_tacdiffusion_review_policy/v3"
    assert policy["effective"] is True
    assert policy["historical_artifacts_are_not_rewritten"] is True
    assert policy["before_first_no_contact_torque_canary"]["review"].startswith(
        "0+0"
    )
    final_gate = policy[
        "before_first_expert_data_collection_contact_or_tacdiffusion_active_run"
    ]
    assert final_gate["review"] == "2+1"
    assert final_gate["timing"].startswith("once after")


def test_unknown_surface_contract_does_not_require_cad_trajectory_feedforward() -> None:
    surface = json.loads(
        (
            ROOT / "config" / "tacdiffusion_surface_input_20260726.json"
        ).read_text(encoding="utf-8")
    )
    status = surface["status"]
    assert status["curved_trajectory_backend_required_for_data_collection"] is False
    assert status["unknown_surface_episode_reference_backend_ready"] is True
    assert status["hard_tcp_tube_guard_ready"] is True
    assert status["durable_expert_episode_artifact_writer_ready"] is True
    assert status["durable_expert_episode_recorder_ready"] is False
    assert status["live_bridge_recorder_integration_ready"] is False
    contract = " ".join(
        surface["preprocessing_and_gates"][
            "unknown_surface_data_collection_contract"
        ]
    )
    assert "keep CAD height and CAD surface normals out" in contract
