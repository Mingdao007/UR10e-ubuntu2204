from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step6_figure8_autotune_v1.v5_composition_contract import (  # noqa: E402
    AutotunerV5CompositionContractV2,
    BaseOutputOverlayV2,
    RolloverCommand,
    RolloverOutputOverlayV2,
    V5AttemptKind,
    V5RolloverInput,
    V5TPState,
    decode_output_overlay,
)
from step6_figure8_autotune_v1.source_identity import SOURCE_PATHS  # noqa: E402


def test_layout607_uses_only_controller_accepted_output_registers() -> None:
    contract = AutotunerV5CompositionContractV2()
    contract.assert_register_recipes(
        input_double_registers=range(24, 48),
        input_integer_registers=range(24, 40),
        output_double_registers=(24,),
        output_integer_registers=range(24, 35),
    )
    receipt = contract.as_dict()
    assert receipt["layout_tag"] == 607.0
    assert receipt["register_recipe"]["output_integer"] == list(range(24, 35))
    assert all(register < 35 for register in receipt["register_recipe"]["output_integer"])
    assert receipt["performance_force_windows_blocking"] is False


def test_checked_in_contract_is_the_typed_contract() -> None:
    checked_in = json.loads(
        (ROOT / "config/step6/autotuner_v5_composition_contract_v2.json").read_text(
            encoding="utf-8"
        )
    )
    assert checked_in == AutotunerV5CompositionContractV2().as_dict()


def test_v5_campaign_config_separates_primary_correction_and_has_no_qualification() -> None:
    config = json.loads(
        (ROOT / "config/step6/autotuner_v5_campaign_v2.json").read_text(
            encoding="utf-8"
        )
    )
    assert config["identity"]["old_v5_state_resume_eligible"] is False
    assert config["wire"]["forbidden_output_integer_registers"] == [35, 36, 37, 38, 39]
    assert config["metric"]["signal"] == "filtered_normal_n"
    assert config["primary_campaign"]["exact_novel_target"] == 200
    assert config["primary_campaign"]["correction_weights"] == [0.0] * 6
    assert config["correction_campaign"]["exact_novel_target"] == 60
    assert config["correction_campaign"]["matched_order"] == [
        "Z", "C", "C", "Z", "Z", "C", "C", "Z", "Z", "C"
    ]
    assert config["home_calibration"]["role"] == "geometry_only"
    assert config["capability_acceptance"]["campaign_qualification"] is False
    assert config["capability_acceptance"]["epoch_qualification"] is False
    assert config["capability_acceptance"]["three_contact_qualification"] is False
    assert config["switch_admission"]["performance_force_windows_blocking"] is False
    serialized = json.dumps(config, sort_keys=True)
    for forbidden in ("[4, 6]", "[3, 7]", "0.30"):
        assert forbidden not in serialized


def test_takeover_receipt_cold_proves_old_state_is_not_resumable() -> None:
    takeover = json.loads(
        (ROOT / "runs/autotuner_v5_takeover_20260817/takeover_receipt.json").read_text(
            encoding="utf-8"
        )
    )
    historical = json.loads(
        (
            ROOT
            / "runs/step6_figure8_autotune_v1_live_v5_seam_retry2_20260816_2317"
            / "handoff-resident-001/figure8_fingerprint.json"
        ).read_text(encoding="utf-8")
    )
    home = json.loads(
        (
            ROOT
            / "runs/step6_figure8_home_calibration_v1_20260816_201400"
            / "home_calibration_receipt.json"
        ).read_text(encoding="utf-8")
    )
    assert takeover["fresh_live_bench_state_claimed"] is False
    assert takeover["preimplementation_identity"]["resume_allowed"] is False
    assert (
        takeover["preimplementation_identity"]["last_live_fingerprint_source_sha256"]
        == historical["source_sha256"]
    )
    assert takeover["home_geometry"]["receipt_sha256"] == home["receipt_sha256"]
    assert takeover["home_geometry"]["role"] == "geometry_only"
    assert all(
        retired["resume_eligible"] is False
        for retired in takeover["retired_incomplete_state_roots"]
    )


def test_v5_contract_and_rollover_are_inside_source_closure() -> None:
    closed = {path.as_posix() for path in SOURCE_PATHS}
    assert "tools/step6_figure8_autotune_v1/v5_composition_contract.py" in closed
    assert "tools/step6_figure8_autotune_v1/v5_rollover.py" in closed
    assert "tools/step6_figure8_autotune_v1/v5_register_transport.py" in closed
    assert "config/step6/autotuner_v5_composition_contract_v2.json" in closed
    assert "config/step6/autotuner_v5_campaign_v2.json" in closed
    assert {
        "tools/step6_figure8_autotune_v1/v5_lifecycle_ledger.py",
        "tools/step6_figure8_autotune_v1/v5_campaign.py",
        "tools/step6_figure8_autotune_v1/v5_filter_shadow.py",
        "tools/step6_figure8_autotune_v1/v5_camera_observer.py",
        "tools/step6_figure8_autotune_v1/v5_ros2_observation_mirror.py",
        "tools/step6_figure8_autotune_v1/v5_sidecar_bundle.py",
        "tools/step6_figure8_autotune_v1/v5_capability_acceptance.py",
        "tools/run_autotuner_v5_sidecars.py",
        "config/step6/autotuner_v5_sidecars_v1.json",
    } <= closed


def test_rollover_input_preserves_early_censor_and_requires_atomic_qdot_commit() -> None:
    prepared = V5RolloverInput(
        command=RolloverCommand.PREPARE,
        generation=7,
        path_early_end_request=41,
        next_attempt_ordinal=42,
        next_attempt_kind=V5AttemptKind.PRIMARY_NOVEL,
        next_candidate_token=9001,
    )
    assert prepared.by_register == {
        33: 1,
        34: 7,
        35: 41,
        36: 42,
        37: 10,
        38: 9001,
        39: 0,
    }
    with pytest.raises(ValueError, match="qdot_generation"):
        V5RolloverInput(
            command=RolloverCommand.COMMIT,
            generation=7,
            next_attempt_ordinal=42,
            next_attempt_kind=V5AttemptKind.PRIMARY_NOVEL,
            next_candidate_token=9001,
            qdot_generation=6,
        )


def test_output_29_to_31_is_decoded_by_tp_state() -> None:
    base = decode_output_overlay(
        V5TPState.PATH,
        {29: 12, 30: int(V5AttemptKind.CORRECTION_NOVEL), 31: 0},
    )
    assert base == BaseOutputOverlayV2(
        consumed_session_command_sequence=12,
        attempt_kind=V5AttemptKind.CORRECTION_NOVEL,
        return_guard=0,
    )

    rollover = decode_output_overlay(
        V5TPState.ROLLOVER_PREPARED,
        {29: 7, 30: 6, 31: 9001},
    )
    assert rollover == RolloverOutputOverlayV2(
        rollover_ack_generation=7,
        active_qdot_generation=6,
        prepared_candidate_token=9001,
    )

    idle = decode_output_overlay(
        V5TPState.READY_HOME_NEXT,
        {29: 0, 30: 0, 31: 123},
    )
    assert idle == BaseOutputOverlayV2(
        consumed_session_command_sequence=0,
        attempt_kind=None,
        return_guard=123,
    )


@pytest.mark.parametrize(
    "bad_outputs",
    [range(24, 36), (*range(24, 35), 39), range(25, 35)],
)
def test_layout607_rejects_draft_or_shifted_output_recipes(bad_outputs) -> None:
    with pytest.raises(ValueError, match="output-integer"):
        AutotunerV5CompositionContractV2().assert_register_recipes(
            input_double_registers=range(24, 48),
            input_integer_registers=range(24, 40),
            output_double_registers=(24,),
            output_integer_registers=bad_outputs,
        )
