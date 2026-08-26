from __future__ import annotations

import hashlib
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step6_figure8_autotune_v1 as builder  # noqa: E402


STAMP = "2026-08-17T1200HKT_STEP6_FIGURE8_AUTOTUNE_V5"


def _resident(script: str) -> str:
    block = script.split("def codex_r006_execute_chain", 1)[1].split(
        f"def {builder.PROGRAM_NAME}()", 1
    )[0]
    return block.split(
        "  # One contact acquisition feeds the complete resident chain.", 1
    )[1]


def _guarded_speedj(block: str) -> None:
    speeds = list(re.finditer(r"speedj\(", block))
    guards = list(
        re.finditer(
            r"local ([A-Za-z0-9_]+_guard) = codex_r006_packet_guard\(packet_reason, 60\.0, 100\.0, 3\.0\)",
            block,
        )
    )
    assert speeds
    assert len(guards) == len(speeds)
    for guard, speed in zip(guards, speeds):
        assert guard.start() < speed.start()
        failure = block.find(f"elif {guard.group(1)} != 0:", guard.start(), speed.start())
        fault = block.find("codex_r006_fault", failure, speed.start())
        assert 0 <= failure < fault < speed.start()


def _package() -> tuple[str, str, bytes, str, dict]:
    sanity = builder.numeric_sanity()
    input_sha = builder.build_input_sha256()
    script = builder.build_script(STAMP, sanity, input_sha256=input_sha)
    txt = builder.build_txt(STAMP, sanity, input_sha256=input_sha)
    urp = builder.build_urp(script, builder.PROGRAM_NAME, builder.CONTROLLER_DIR)
    checks = builder.validate_package(script, txt, urp, STAMP, input_sha256=input_sha)
    return script, txt, urp, input_sha, checks


def test_v5_package_recipe_output_identity_and_cached_urp_are_exact():
    script, txt, urp, input_sha, checks = _package()
    writes = {
        int(value)
        for value in re.findall(r"write_output_integer_register\(\s*(\d+)\s*,", script)
    }

    assert "WIRE_LAYOUT: layout-607" in script
    assert "input_double[24..47]" in script
    assert "input_integer[24..39]" in script
    assert "output_integer[24..34]" in script
    assert "layout != 607.0" in script
    assert writes == set(range(24, 35))
    assert not re.search(r"write_output_integer_register\(\s*(3[5-9]|[4-9][0-9])\s*,", script)
    assert "layout-606" not in script
    assert "cached_contents_exact" in checks
    assert checks["cached_contents_exact"] is True
    assert hashlib.sha256(script.encode()).hexdigest() != hashlib.sha256(txt.encode()).hexdigest()
    assert input_sha in script
    metric = builder.numeric_sanity()["force_metric_semantics"]
    assert metric["filtered_normal_is_metric_input"] is True
    assert "raw_measured_force_is_metric_input" not in metric
    assert "filtered_normal_n" in metric["metric_statistic"]
    assert "raw_measured_force_is_metric_input" not in script
    assert "mean(raw_measured_normal_force" not in script
    assert "STEP6_METRIC_INPUT: raw measured force" not in script


def test_v5_builder_atomically_refreshes_deploy_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(builder, "NUMERIC_SANITY_PATH", tmp_path / "numeric.json")
    result = builder.write_outputs(stamp=STAMP, output_dir=tmp_path)
    manifest_path = Path(result["paths"]["deploy_manifest"])
    assert not manifest_path.with_name(manifest_path.name + ".part").exists()
    manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["basename"] == builder.PROGRAM_NAME
    assert manifest["controller_directory"] == builder.CONTROLLER_DIR
    assert [item["filename"].rsplit(".", 1)[1] for item in manifest["artifacts"]] == [
        "urp",
        "txt",
        "script",
    ]
    for item in manifest["artifacts"]:
        artifact = tmp_path / item["filename"]
        assert item["source"] == item["filename"]
        assert item["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()


def test_v5_resident_entry_path_tail_overlay_and_no_old_qualification_seams():
    script, txt, _, _, _ = _package()
    resident = _resident(script)
    entry = resident.split("while not latch_seen", 1)[1].split("while True", 1)[0]
    motion = resident.split("while True", 1)[1]

    assert "while not latch_seen" in resident
    assert "latch_seen = True" in resident
    assert "path_elapsed_s = 0.0" in resident
    assert "codex_r006_echo(epoch, ordinal, 25" in resident
    assert "local base_target_n = 5.000000000" in resident
    assert "path_elapsed_s / 4.000000000" not in resident
    assert "path_elapsed_s < 60.000000000" in resident
    assert "local tail_end_s = 20.0 * 3.141592653589793" in resident
    assert "tail_old_candidate_authority" in resident
    _guarded_speedj(entry)
    _guarded_speedj(motion)
    assert "speedj(active_path_qdot, 40.000000000, bounded_motion_dt)" in resident
    assert "sync()" not in motion
    assert "speedj(path_qdot, 40.000000000, actual_path_dt)" not in resident
    assert "local path_qdot = [read_input_float_register" not in resident
    assert resident.index("speedj(active_path_qdot") < resident.index(
        "active_path_qdot = prepared_path_qdot",
        resident.index("ordinal = prepared_ordinal"),
    )
    assert "tail_crossing = path_elapsed_s + actual_path_dt >= tail_end_s" in resident
    assert "bounded_motion_dt = tail_end_s - path_elapsed_s" in resident
    assert "path_elapsed_s = path_elapsed_s + bounded_motion_dt" in resident
    assert resident.rfind("if tail_crossing:") > resident.index("speedj(active_path_qdot")
    assert "stopj(" not in resident
    assert "codex_r006_stationary" not in resident
    assert resident.count("return codex_r006_return_home") == 1
    assert "if not prepared_valid and not commit_armed and rollover_command == 0:" in resident
    assert resident.index("return codex_r006_return_home") > resident.rfind("if tail_crossing:")
    assert "figure8-force-mae-v2" in txt
    assert "filtered_normal_n is the metric input" in txt
    assert "raw measured normal force is the metric input" not in txt
    assert "evidence=[0,60) 600 bins" in txt
    assert "formal=[5,60) 550 bins" in txt
    assert "closure tail=[60,20*pi)" in txt
    assert "qualification" not in script.lower()
    assert "baseline" not in script.lower()


def test_v5_rollover_prepare_commit_reset_early_censor_stop_and_cap_are_structural():
    script, _, _, _, _ = _package()
    resident = _resident(script)

    assert "prepared_valid" in resident
    assert "rollover_generation_fence" in resident
    assert "rollover_command == 1" in resident
    assert "rollover_command == 3" in resident
    assert "rollover_command == 2" in resident
    assert "prepared_valid = True" in resident
    assert "prepared_valid = False" in resident
    assert "rollover_generation_fence = rollover_generation" in resident
    assert "ordinal = prepared_ordinal" in resident
    assert "qdot_generation = armed_qdot_generation" in resident
    assert "metric_sample_count = 0" in resident
    assert "formal_metric_sample_count = 0" in resident
    assert "force_integral_n_s = 0.0" in resident
    assert "codex_r006_echo(epoch, ordinal, 27" in resident
    assert "codex_r006_echo(epoch, ordinal, 28" in resident
    assert "codex_v5_reject_home" in resident
    assert "early_end_request == ordinal" in resident
    assert "early_end_request != ordinal" in resident
    assert "codex_v5_stop_requested()" in resident
    assert "codex_r006_fault(epoch, ordinal, token, kind, consumed, 4" in resident
    assert "committed_rollovers >= 4" in resident
    assert "exactly four committed rollovers" in script
    gate = script.split("def codex_v5_switch_gate_families", 1)[1].split(
        "def codex_r006_echo", 1
    )[0]
    assert "= True" not in gate
    assert "codex_v5_switch_gate_families(hard_safety_gate, timing_freshness_gate, tube_cbf_gate, identity_metric_closure_gate, command_envelope_gate)" in resident
    assert "local hard_safety_gate = motion_guard == 0" in resident
    assert "local tube_cbf_gate = codex_r006_qdot_ok()" in resident
    assert "local identity_metric_closure_gate = path_elapsed_s == tail_end_s" in resident
    assert "local command_envelope_gate = integer_reason == 0" in resident
    assert "global codex_v5_active_ordinal = 0" in script
    assert "ordinal = prepared_ordinal" in resident
    assert "codex_v5_active_ordinal = ordinal" in resident
    assert "local chain_ok = codex_r006_execute_chain" in script
    assert "current_ordinal = codex_v5_active_ordinal" in script
    assert "current_kind = codex_v5_active_kind" in script
    assert "current_token = codex_v5_active_token" in script
    for marker in ("duplicate_prepare", "duplicate_cancel", "duplicate_commit", "duplicate_commit_ack"):
        assert marker in resident
    assert "last_rollover_qdot_generation" in resident
    assert "codex_r006_echo(epoch, ordinal, 28, token, 0, last_rollover_generation" in resident
    assert "codex_r006_echo(epoch, ordinal, 26, token, 0, rollover_generation_fence, qdot_generation, 0" in resident
    assert "local active_path_qdot = [read_input_float_register(37)" in resident
    assert "local live_candidate_qdot = [read_input_float_register(37)" in resident
    assert resident.count("active_path_qdot = live_candidate_qdot") == 1
    assert "if duplicate_commit_ack and not commit_armed:" in resident
    assert "commit_armed = True" in resident
    assert "prepared_path_qdot = live_candidate_qdot" in resident
    assert "active_path_qdot = live_candidate_qdot\n      if commit_armed:\n        prepared_path_qdot = live_candidate_qdot" in resident
    assert "if not tail_crossing and not commit_armed:" not in resident
    assert "active_path_qdot = prepared_path_qdot" in resident
    motion_start = resident.index("speedj(active_path_qdot")
    assert resident.index("if duplicate_commit_ack and not commit_armed:") < motion_start
    assert "elif duplicate_commit_ack:" in resident[:motion_start]
    assert "elif duplicate_commit_ack:" not in resident[motion_start:]
    assert "elif rollover_command == 0:" in resident
    assert "last_rollover_command = 0" in resident
    assert "last_rollover_generation = 0" in resident
    assert "path_elapsed_s = path_elapsed_s + bounded_motion_dt" in resident
    assert "speedj(active_path_qdot, 40.000000000, bounded_motion_dt)" in resident
    assert "codex_v5_active_epoch = active_epoch" in script
    assert script.index("codex_v5_active_epoch = active_epoch") < script.index("local chain_ok = codex_r006_execute_chain")
    assert "legacy_resident" not in Path(builder.__file__).read_text(encoding="utf-8")


def test_mature_hard_safety_contact_qdot_fixed_home_and_return_home_seams_remain():
    script, _, _, _, _ = _package()

    assert "codex_r006_packet_guard" in script
    assert "codex_r006_qdot_ok" in script
    assert "codex_r006_abs(read_input_float_register(24)) >= abs_normal_limit" in script
    assert "read_input_float_register(25) >= force_norm_limit" in script
    assert "read_input_float_register(30) >= torque_limit" in script
    assert "R008 B3 two-stage contact search" in script
    assert "local d_near_start_travel_m = 0.011029311" in script
    assert "local force_fuse_n = 50.000000000" in script
    assert "local v_far_m_s = 0.005000000" in script
    assert "local v_near_m_s = 0.000200000" in script
    assert "contact_elapsed_s >= 90.000000000" in script
    assert "codex_r006_entry_home_verified" in script
    assert "codex_r006_return_home" in script
    assert "READY_HOME_NEXT=78" in script
    assert "state = 80" in script
    assert "codex_r006_packet_guard(packet_reason, 60.0, 100.0, 3.0)" in script
    assert "codex_r006_packet_guard(packet_reason, 100.0, 100.0, 3.0)" in script
    assert (
        "if chain_ok:\n"
        "            session_active = False\n"
        "            state = 78\n"
        "            reason = 0\n"
        "            return_guard = codex_r006_attempt_guard\n"
        "          else:"
    ) in script


def test_validator_rejects_output_layout_and_old_seam_mutations():
    script, txt, _, input_sha, _ = _package()

    mutations = (
        script.replace(
            "write_output_integer_register(34, 520607)",
            "write_output_integer_register(35, 520607)",
            1,
        ),
        script.replace("layout-607", "layout-606", 1),
        script + "\n# baseline_successes\n",
        script + "\n# raw_measured_force_is_metric_input: True\n",
        script.replace(
            "local tube_cbf_gate = codex_r006_qdot_ok()",
            "local tube_cbf_gate = True",
            1,
        ),
        script.replace(
            "local entry_guard = codex_r006_packet_guard(packet_reason, 60.0, 100.0, 3.0)",
            "local entry_guard = 0",
            1,
        ),
        script.replace(
            "path_elapsed_s = path_elapsed_s + bounded_motion_dt",
            "path_elapsed_s = path_elapsed_s + actual_path_dt",
            1,
        ),
        script.replace(
            "path_elapsed_s = path_elapsed_s + bounded_motion_dt\n      if tail_crossing:",
            "path_elapsed_s = path_elapsed_s + bounded_motion_dt\n      sync()\n      if tail_crossing:",
            1,
        ),
        script.replace(
            "current_ordinal = codex_v5_active_ordinal",
            "current_ordinal = current_ordinal",
            1,
        ),
        script.replace(
            "speedj(active_path_qdot, 40.000000000, bounded_motion_dt)",
            "local path_qdot = [read_input_float_register(37), read_input_float_register(38), read_input_float_register(39), read_input_float_register(40), read_input_float_register(41), read_input_float_register(42)]\n        speedj(path_qdot, 40.000000000, bounded_motion_dt)",
            1,
        ),
        script.replace(
            "speedj(active_path_qdot, 40.000000000, bounded_motion_dt)",
            "speedj(live_candidate_qdot, 40.000000000, bounded_motion_dt)",
            1,
        ),
        script.replace(
            "if duplicate_commit_ack and not commit_armed:",
            "if duplicate_commit_ack:",
            1,
        ),
        script.replace(
            "codex_v5_active_epoch = active_epoch",
            "codex_v5_active_epoch = 0",
            1,
        ),
        script.replace(
            "            return_guard = codex_r006_attempt_guard\n          else:",
            "          else:",
            1,
        ),
    )
    for mutated in mutations:
        mutated_urp = builder.build_urp(
            mutated,
            builder.PROGRAM_NAME,
            builder.CONTROLLER_DIR,
        )
        with pytest.raises(builder.Step6FigureEightPackageError):
            builder.validate_package(
                mutated,
                txt,
                mutated_urp,
                STAMP,
                input_sha256=input_sha,
            )
