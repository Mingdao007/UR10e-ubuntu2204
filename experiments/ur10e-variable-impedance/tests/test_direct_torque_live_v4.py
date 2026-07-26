import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import re

import pytest

from ur10e_vic.tacdiffusion.direct_torque_live_v4 import (
    COMPILE_PROBE_PROTOCOL_TOKEN,
    LIVE_PROTOCOL_TOKEN,
    LIVE_RECEIVER_SCHEMA,
    LiveTubeContract,
    SequenceDecision,
    build_compile_probe_source,
    build_live_receiver_source,
    evaluate_sequence,
    parse_compile_probe_source,
    parse_live_receiver_source,
)


ROOT = Path(__file__).resolve().parents[1]
PASSIVE_RUN = (
    ROOT.parent
    / "tase-contact-reproduction"
    / "runs"
    / "tacdiffusion"
    / "passive_remote_baseline_20260726"
)
REFERENCE = PASSIVE_RUN / "unknown_surface_anchor_circle_no_contact_2s_reference_v2.json"
BUILDER = ROOT / "tools" / "build_tacdiffusion_direct_torque_live_v4.py"


def reference_path() -> Path:
    if REFERENCE.is_file():
        return REFERENCE
    pytest.skip("fresh passive reference artifact is unavailable")


def test_live_tube_loads_fresh_anchor_without_cad_feedforward() -> None:
    path = reference_path()
    tube = LiveTubeContract.from_reference_artifact(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert tube.anchor_pose_base == tuple(payload["anchor_pose_base"])
    assert tube.normal_half_width_m == pytest.approx(0.002)
    assert payload["cad_height_or_normal_feedforward"] is False


def test_receiver_v4_is_invoked_holds_packets_and_returns_through_stopj() -> None:
    source = build_live_receiver_source(
        LiveTubeContract.from_reference_artifact(reference_path())
    )
    contract = parse_live_receiver_source(source)
    assert contract.schema == LIVE_RECEIVER_SCHEMA
    assert contract.protocol_token == LIVE_PROTOCOL_TOKEN
    assert contract.invocation_present
    assert contract.explicit_position_handoff
    assert contract.complete_identity_echo
    assert contract.applied_action_echo
    assert contract.hard_tube_guard
    assert not contract.source_builder_physical_io_enabled
    assert contract.controller_runtime_physical_io_enabled
    assert source.rstrip().endswith("tacdiffusion_remote_direct_torque_v4_program()")
    assert source.startswith("def tacdiffusion_remote_direct_torque_v4_program():\n")
    assert len(re.findall(r"(?m)^\s*def\s+", source)) == 1
    assert source.count("stopj(10.0)") == 1
    assert "release_ready_tolerance_m = 0.001" in source
    assert "write_output_float_register(32 + axis, last_k[axis])" in source
    assert "write_output_float_register(26 + axis, filtered_force[axis])" in source
    assert "write_output_float_register(38 + axis, tau[axis])" in source
    assert source.count("direct_torque(tau, friction_comp=True)") == 1
    assert "direct_torque([0.0" not in source
    assert "entry_pose[axis] = actual_pose[axis]" in source
    assert "entry_tick < entry_blend_ticks" in source
    assert "episode_latched == 0" in source
    assert "startup_packet_ok = command == command_idle" in source
    assert "packet_lease != lease_id or packet_episode != episode_identity" in source
    assert source.index("episode_latched == 0") < source.index(
        "direct_torque(tau, friction_comp=True)"
    )
    assert "write_output_integer_register(34, last_observed_command)" in source
    assert "write_output_integer_register(35, episode_latched)" in source
    assert not re.search(r"(?m)^\s*return\b", source)
    assert source.count("sync()") == 3


def test_compile_probe_is_bounded_and_contains_no_motion_api() -> None:
    source = build_compile_probe_source()
    parse_compile_probe_source(source)
    assert str(COMPILE_PROBE_PROTOCOL_TOKEN) in source
    assert source.count("sync()") == 1
    assert not re.search(
        r"\b(direct_torque|stopj|movej|movel|speedj|speedl|servoj|force_mode)\s*\(",
        source,
    )


def test_compile_probe_parser_rejects_torque_or_motion() -> None:
    source = build_compile_probe_source()
    with pytest.raises(ValueError, match="must not contain"):
        parse_compile_probe_source(source + "\ndirect_torque([0,0,0,0,0,0])\n")
    with pytest.raises(ValueError, match="must not contain"):
        parse_compile_probe_source(source + "\nstopj(10.0)\n")


def test_v4_source_is_episode_bound_and_deterministic() -> None:
    tube = LiveTubeContract.from_reference_artifact(reference_path())
    first = build_live_receiver_source(tube)
    second = build_live_receiver_source(tube)
    assert first == second
    assert hashlib.sha256(first.encode("utf-8")).hexdigest() == hashlib.sha256(
        second.encode("utf-8")
    ).hexdigest()
    for value in tube.anchor_pose_base:
        assert f"{value:.17g}" in first


def test_sequence_oracle_accepts_new_and_bounded_hold() -> None:
    first = evaluate_sequence(last_sequence=0, incoming_sequence=1, held_age_ticks=0)
    assert first.decision == SequenceDecision.NEW
    held = evaluate_sequence(last_sequence=1, incoming_sequence=1, held_age_ticks=0)
    assert held.decision == SequenceDecision.HELD
    assert held.next_age_ticks == 1
    for age in range(1, 10):
        held = evaluate_sequence(
            last_sequence=1,
            incoming_sequence=1,
            held_age_ticks=age,
        )
        assert held.decision == SequenceDecision.HELD


def test_sequence_oracle_rejects_stale_gap_and_replay() -> None:
    stale = evaluate_sequence(
        last_sequence=1,
        incoming_sequence=1,
        held_age_ticks=10,
    )
    assert stale.decision == SequenceDecision.FAULT
    assert stale.reason == "heartbeat_stale"
    assert (
        evaluate_sequence(
            last_sequence=1,
            incoming_sequence=3,
            held_age_ticks=0,
        ).reason
        == "sequence_gap_or_replay"
    )
    assert (
        evaluate_sequence(
            last_sequence=2,
            incoming_sequence=1,
            held_age_ticks=0,
        ).decision
        == SequenceDecision.FAULT
    )


def test_live_tube_rejects_nonorthogonal_and_outside_anchor() -> None:
    with pytest.raises(ValueError, match="orthogonal"):
        LiveTubeContract(
            center_base_m=(0.0, 0.0, 0.0),
            anchor_pose_base=(0.0, 0.0, 0.0, math.pi, 0.0, 0.0),
            u_axis_base=(1.0, 0.0, 0.0),
            v_axis_base=(1.0, 0.0, 0.0),
            safe_u_half_width_m=0.01,
            safe_v_half_width_m=0.01,
            normal_half_width_m=0.002,
            orientation_tolerance_rad=0.1,
        )


def test_live_tube_checks_actual_and_desired_pose() -> None:
    tube = LiveTubeContract.from_reference_artifact(reference_path())
    tube.assert_contains_pose(tube.anchor_pose_base, role="actual")
    outside = list(tube.anchor_pose_base)
    outside[2] += tube.normal_half_width_m + 0.001
    with pytest.raises(RuntimeError, match="actual_tube_normal_guard"):
        tube.assert_contains_pose(outside, role="actual")
    with pytest.raises(ValueError, match="outside"):
        LiveTubeContract(
            center_base_m=(0.0, 0.0, 0.0),
            anchor_pose_base=(0.02, 0.0, 0.0, math.pi, 0.0, 0.0),
            u_axis_base=(1.0, 0.0, 0.0),
            v_axis_base=(0.0, 1.0, 0.0),
            safe_u_half_width_m=0.01,
            safe_v_half_width_m=0.01,
            normal_half_width_m=0.002,
            orientation_tolerance_rad=0.1,
        )


def test_parser_rejects_missing_invocation_or_high_level_motion() -> None:
    source = build_live_receiver_source(
        LiveTubeContract.from_reference_artifact(reference_path())
    )
    with pytest.raises(ValueError, match="invocation"):
        parse_live_receiver_source(
            source.replace(
                "\ntacdiffusion_remote_direct_torque_v4_program()\n",
                "\n",
            )
        )
    with pytest.raises(ValueError, match="forbidden"):
        parse_live_receiver_source(source + "\nmovel(p[0,0,0,0,0,0])\n")
    with pytest.raises(ValueError, match="unique"):
        parse_live_receiver_source(
            source + "\ntacdiffusion_remote_direct_torque_v4_program()\n"
        )
    with pytest.raises(ValueError, match="gravity"):
        parse_live_receiver_source(source + "\n    get_gravity_torques(q)\n")
    with pytest.raises(ValueError, match="nested helpers"):
        parse_live_receiver_source(
            source.replace(
                "  local receiver_schema",
                "  def accidental_nested_helper():\n  end\n  local receiver_schema",
                1,
            )
        )
    with pytest.raises(ValueError, match="zero-torque"):
        parse_live_receiver_source(
            source + "\ndirect_torque([0.0, 0.0, 0.0, 0.0, 0.0, 0.0])\n"
        )
    with pytest.raises(ValueError, match="common exit"):
        parse_live_receiver_source(source + "\nreturn\n")
    with pytest.raises(ValueError, match=r"abs\(\)"):
        parse_live_receiver_source(source + "\nlocal x = abs(1.0)\n")


def test_bundle_builder_binds_reference_source_and_refuses_overwrite(tmp_path) -> None:
    source_path = tmp_path / "receiver.script"
    manifest_path = tmp_path / "receiver.manifest.json"
    command = [
        sys.executable,
        str(BUILDER),
        "--reference",
        str(reference_path()),
        "--receiver-source",
        str(source_path),
        "--manifest",
        str(manifest_path),
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT.parents[1],
        env={"PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = source_path.read_text(encoding="utf-8")
    assert report["live_actions"] is False
    assert manifest["receiver_source_sha256"] == hashlib.sha256(
        source.encode("utf-8")
    ).hexdigest()
    assert manifest["reference_artifact_sha256"] == hashlib.sha256(
        reference_path().read_bytes()
    ).hexdigest()
    assert manifest["gates"]["controller_runtime_physical_io"] is True
    assert manifest["gates"]["source_builder_physical_io"] is False
    assert manifest["register_contract"]["applied_action"]["stiffness_k"] == list(
        range(32, 38)
    )
    assert manifest["register_contract"]["applied_action"][
        "commanded_joint_torque_nm"
    ] == list(range(38, 44))
    assert manifest["gates"]["single_top_level_program"] is True
    assert manifest["gates"]["continuous_500hz_torque_site"] is True
    assert manifest["gates"]["zero_torque_startup_or_exit"] is False
    repeated = subprocess.run(
        command,
        cwd=ROOT.parents[1],
        env={"PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert repeated.returncode == 2
    assert "already exists" in repeated.stderr
