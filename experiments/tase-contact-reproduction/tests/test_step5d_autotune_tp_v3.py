from __future__ import annotations

import gzip
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_tp as v1  # noqa: E402
import build_step5d_autotune_tp_v3 as v3  # noqa: E402

TEST_PROGRAM = "step5d_strict_rnn_autotune_v3_r999"
TEST_STAMP = "2026-07-20T1325HKT_" + TEST_PROGRAM.upper()


def test_r010_rolling_campaign_preserves_v1_kernel_and_has_one_motion_owner() -> None:
    rendered = v3.render_script(TEST_PROGRAM)
    v3.validate_rendered_script(rendered, program_id=TEST_PROGRAM)

    assert f"# TP_PROGRAM_ID: {TEST_PROGRAM}" in rendered
    assert "# CONTROL_PROFILE_ID: step5d_strict_rnn_autotune_v1" in rendered
    assert hashlib.sha256(v1.render_script().encode()).hexdigest() in rendered
    assert "def codex_step5d_autotune_trial_v1(" in rendered
    assert "local entry_xy_pose = p[entry_x, entry_y, p_current[2]" in rendered
    assert "movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)" in rendered
    assert "movel(entry_precontact_pose, a=0.060, v=0.040, r=0.0)" in rendered
    stage22 = rendered.index("write_output_float_register(35, 22.0)")
    entry_xy_movel = rendered.index(
        "movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)", stage22
    )
    entry_precontact_movel = rendered.index(
        "movel(entry_precontact_pose, a=0.060, v=0.040, r=0.0)",
        entry_xy_movel,
    )
    stage23 = rendered.index(
        "write_output_float_register(35, 23.0)", entry_precontact_movel
    )
    assert stage22 < entry_xy_movel < entry_precontact_movel < stage23
    assert "movel(rise_pose, a=0.060, v=0.040, r=0.0)" in rendered
    assert "movel(transfer_pose, a=0.135, v=0.090, r=0.0)" in rendered
    assert "movel(target_pose, a=0.060, v=0.040, r=0.0)" in rendered
    assert "local batch_row_index = read_input_integer_register(30)" in rendered
    assert "normal_level == 6" in rendered
    assert "normal_level == 6" not in v1.render_script()
    assert "if stale_s2 > 1.000:" in rendered
    assert "if stale_s2 > 1.000:" in v1.render_script()
    assert "if stale_s2 > 0.020:" not in rendered
    assert v3.STAGE25_STALE_COMMAND_HOLD_S == 1.000
    assert 0.060 < v3.STAGE25_STALE_COMMAND_HOLD_S
    assert "waiting_for_ack" not in rendered
    assert "ACK_BUNDLE" not in rendered
    assert "WAIT_ACK" not in rendered
    assert "codex_autotune_wait_for_arm(campaign_epoch, trial_id, 78" in rendered
    assert "codex_autotune_publish_state_and_halt(campaign_epoch, trial_id, 77" in rendered
    assert "codex_autotune_publish_state_and_halt(campaign_epoch, trial_id, 75" not in rendered
    assert "next_command == 2" in rendered
    assert "candidate_token, 19, execution_profile_id" not in rendered
    assert "while waiting_s < 30.000" not in rendered
    assert "while waiting_s <" not in rendered
    assert "while True" in rendered
    assert rendered.count("def codex_autotune_wait_for_arm") == 1
    assert "halt" in rendered
    state_body = rendered.split("def codex_autotune_write_state(", 1)[1].split(
        "\nend", 1
    )[0]
    assert state_body.index("codex_step5d_publish_runtime_identity()") < state_body.index(
        "write_output_integer_register(24, campaign_epoch)"
    )
    identity_body = rendered.split(
        "def codex_step5d_publish_runtime_identity():", 1
    )[1].split("\nend", 1)[0]
    identity_positions = [
        identity_body.index(f"write_output_integer_register({register},")
        for register in (35, 36, 37)
    ]
    assert identity_positions == sorted(identity_positions)
    assert rendered.count("thread codex_autotune_return_telemetry_observer():") == 1
    observer = rendered.split(
        "thread codex_autotune_return_telemetry_observer():", 1
    )[1].split("\nend\n", 1)[0]
    assert "time()" in observer
    assert "get_actual_tcp_speed()" in observer
    assert "sync()" in observer
    for forbidden_motion in (
        "movel(",
        "movej(",
        "speedl(",
        "speedj(",
        "stopl(",
        "stopj(",
    ):
        assert forbidden_motion not in observer
    for output_register in range(35, 45):
        assert f"write_output_float_register({output_register}," not in observer
    assert "stop_reason == 2 or stop_reason == 3 or stop_reason == 17" in rendered
    for forbidden in (
        "def codex_autotune_certification_stop(",
        "def codex_autotune_certification_return(",
        "command == 5",
        "command == 6",
        "codex_autotune_bounded_return_segment",
    ):
        assert forbidden not in rendered


def test_r016_local_guard_outcomes_are_trial_local_and_rearmable() -> None:
    rendered = v3.render_script(TEST_PROGRAM)

    auto_home = rendered.split("def codex_should_auto_home(", 1)[1].split(
        "\nend", 1
    )[0]
    for marker in (
        "if stop_reason == 1.0:\n    return True",
        "elif stop_reason == 4.0:\n    return True",
        "elif stop_reason == 5.0:\n    return True",
        "elif stop_reason == 6.0:\n    return True",
        "elif stop_reason == 7.0:\n    return True",
        "elif stop_reason == 8.0:\n    return True",
        "elif stop_reason == 9.0:\n    return True",
        "elif stop_reason == 10.0:\n    return True",
        "elif stop_reason == 12.0:\n    return True",
        "elif stop_reason == 13.0:\n    return True",
        "elif stop_reason == 14.0:\n    return True",
    ):
        assert marker in auto_home
    for marker in (
        "elif stop_reason == 2.0:\n    # Transport/heartbeat loss may follow a protective stop; never auto-home.\n    return False",
        "elif stop_reason == 17.0:\n    # Reserved unsafe-entry/safety interruption reason; never auto-home.\n    return False",
    ):
        assert marker in auto_home

    direct_terminal = rendered.split(
        "def codex_autotune_direct_terminal_state(", 1
    )[1].split("\nend", 1)[0]
    assert "return 78" in direct_terminal
    assert "return 90" not in direct_terminal

    main_start = rendered.index("def codex_step5d_strict_rnn_autotune_v3():")
    main_end = rendered.index("\n  end\nend\n", main_start)
    main = rendered[main_start:main_end]
    auto_home_path = main.split(
        "      elif codex_should_auto_home(stop_reason):", 1
    )[1].split("\n      else:", 1)[0]
    assert "codex_autotune_single_owner_return" in auto_home_path
    assert "codex_autotune_wait_for_arm(campaign_epoch, trial_id, 78" in auto_home_path
    assert "codex_autotune_write_state(campaign_epoch, trial_id, 60" in auto_home_path

    no_return_path = main.split(
        "      if stop_reason == 2 or stop_reason == 3 or stop_reason == 17:",
        1,
    )[1].split("\n      elif codex_should_auto_home", 1)[0]
    assert "codex_autotune_wait_for_external_home" in no_return_path
    assert "codex_autotune_single_owner_return" not in no_return_path
    assert "codex_autotune_publish_fault_and_halt" not in no_return_path
    assert "\n  halt" not in no_return_path
    assert "codex_autotune_write_state(campaign_epoch, trial_id, 78" in no_return_path
    assert no_return_path.index("codex_autotune_wait_for_external_home") < no_return_path.index(
        "codex_autotune_write_state(campaign_epoch, trial_id, 78"
    ) < no_return_path.index("codex_autotune_wait_for_arm")
    for forbidden_motion in ("movel(", "movej(", "speedl(", "speedj(", "servoj("):
        assert forbidden_motion not in no_return_path

    unknown_path = main.rsplit("\n      else:\n", 1)[1]
    assert "codex_autotune_wait_for_external_home" in unknown_path
    assert "codex_autotune_publish_fault_and_halt" not in unknown_path
    assert "\n  halt" not in unknown_path
    assert "codex_autotune_write_state(campaign_epoch, trial_id, 78" in unknown_path
    for forbidden_motion in ("movel(", "movej(", "speedl(", "speedj(", "servoj("):
        assert forbidden_motion not in unknown_path

    external_home = rendered.split(
        "def codex_autotune_wait_for_external_home(", 1
    )[1].split("\nend\n", 1)[0]
    assert "codex_autotune_write_state(campaign_epoch, trial_id, 75" in external_home
    assert "WAITING_FOR_HARDWARE / WAIT_INFRA_READY" in external_home
    proof = "codex_autotune_typed_target_verified(campaign_home_pose, campaign_home_q, True)"
    assert external_home.index(proof) < external_home.index("next_command =")
    assert "next_command == 3" in external_home
    assert "codex_autotune_publish_fault_and_halt" in external_home
    for forbidden_motion in ("movel(", "movej(", "speedl(", "speedj(", "servoj("):
        assert forbidden_motion not in external_home

    invalid_arm = main.split(
        "if campaign_epoch <= 0 or", 1
    )[1].split("\n      end", 1)[0]
    assert "codex_autotune_wait_for_arm(campaign_epoch, trial_id, 78" in invalid_arm
    assert "codex_autotune_publish_fault_and_halt" not in invalid_arm

    wait_for_arm = rendered.split(
        "def codex_autotune_wait_for_arm(", 1
    )[1].split("\nend\n", 1)[0]
    assert "while True:" in wait_for_arm
    assert "next_command == 1 and next_sequence > consumed_command_seq" in wait_for_arm
    assert "next_command == 4" in wait_for_arm
    assert "next_command == 3" in wait_for_arm
    assert "codex_autotune_publish_state_and_halt(campaign_epoch, trial_id, 77" in wait_for_arm
    assert "halt" in rendered.split(
        "def codex_autotune_publish_state_and_halt(", 1
    )[1].split("\nend\n", 1)[0]
    assert "codex_autotune_publish_fault_and_halt(0, 0, 0, 4" in main
    assert "sleep(60" not in rendered
    assert "time.sleep(" not in rendered


def test_r010_triplet_is_exact_and_revision_is_immutable(tmp_path: Path) -> None:
    stamp = v3.source_stamp(
        TEST_PROGRAM,
        datetime(2026, 7, 20, 13, 25, tzinfo=timezone(timedelta(hours=8)))
    )
    result = v3.write_triplet(tmp_path, stamp, program_id=TEST_PROGRAM)
    basename = TEST_PROGRAM

    assert result["program"] == basename
    assert result["control_profile_id"] == "step5d_strict_rnn_autotune_v1"
    xml = gzip.decompress((tmp_path / f"{basename}.urp").read_bytes()).decode()
    assert f'name="{basename}"' in xml
    assert f"/programs/andyl/kunwei/step5/{basename}.script" in xml
    sanity = json.loads((tmp_path / f"{basename}.numeric-sanity.json").read_text())
    assert sanity["delta_class"] == "identity_precontact_prior_exact_batch_lifecycle_single_owner_return_read_only_telemetry_v5"
    assert sanity["stage25_stale_command_hold_s"] == 1.000
    assert sanity["return_segment_count"] == 3
    assert sanity["batch_row_policy"] == "five_row_logical_batches_every_row_campaign_home"
    assert sanity["host_protocol"] == "v3_full_home_rolling_arm_v1"
    assert sanity["ready_arm_timeout_s"] is None
    assert sanity["input_integer_registers"] == list(range(24, 32))
    assert sanity["output_integer_registers"] == list(range(24, 38))
    assert sanity["tp_runtime_identity"]["registers"] == {
        "protocol_version": 35,
        "digest_hi": 36,
        "digest_lo": 37,
    }
    assert sanity["execution_profile_id"] == "nf100-slew050-a050"
    assert sanity["execution_profile_integer_id"] == 633

    with pytest.raises(FileExistsError, match="increment rNNN"):
        v3.write_triplet(tmp_path, stamp, program_id=TEST_PROGRAM)


def test_r010_generator_check_recomputes_every_output_byte(tmp_path: Path) -> None:
    stamp = v3.source_stamp(
        TEST_PROGRAM,
        datetime(2026, 7, 20, 13, 25, tzinfo=timezone(timedelta(hours=8)))
    )
    v3.write_triplet(tmp_path, stamp, program_id=TEST_PROGRAM)
    assert v3.check_triplet(
        tmp_path, stamp, program_id=TEST_PROGRAM
    )["ok"] is True
    sanity = tmp_path / f"{TEST_PROGRAM}.numeric-sanity.json"
    sanity.write_bytes(sanity.read_bytes().replace(b'"return_segment_count": 3', b'"return_segment_count": 4'))
    with pytest.raises(ValueError, match="numeric-sanity.json:byte_drift"):
        v3.check_triplet(tmp_path, stamp, program_id=TEST_PROGRAM)


def test_r010_default_build_is_byte_stable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    identity_args = ["--program-id", TEST_PROGRAM, "--stamp", TEST_STAMP]
    assert v3.main(["--output-dir", str(first), *identity_args]) == 0
    capsys.readouterr()
    assert v3.main(["--output-dir", str(second), *identity_args]) == 0
    capsys.readouterr()

    first_bytes = {
        path.name: path.read_bytes() for path in sorted(first.iterdir())
    }
    second_bytes = {
        path.name: path.read_bytes() for path in sorted(second.iterdir())
    }
    assert first_bytes == second_bytes
    assert TEST_STAMP.encode() in first_bytes[f"{TEST_PROGRAM}.script"]
