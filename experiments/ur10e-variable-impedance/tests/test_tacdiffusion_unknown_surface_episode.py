import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest

from ur10e_vic.tacdiffusion.expert_episode_artifact import (
    DurableExpertEpisodeWriter,
    ExpertEpisodeBindings,
    ExpertEpisodeFrame,
    read_expert_episode,
    validate_expert_episode_manifest,
)
from ur10e_vic.tacdiffusion.unknown_surface_episode import (
    generate_unknown_surface_references,
    load_unknown_surface_tube,
    rotation_vector_distance_rad,
)


ROOT = Path(__file__).resolve().parents[1]
SURFACE_MANIFEST = ROOT / "config" / "tacdiffusion_surface_input_20260726.json"
BUILDER = ROOT / "tools" / "build_tacdiffusion_unknown_surface_episode.py"
ANCHOR = (0.463, 0.172, 0.045, -3.14155, 0.0058, 0.0)


def bindings(*, training_eligible: bool = False) -> ExpertEpisodeBindings:
    return ExpertEpisodeBindings(
        episode_id="episode-001",
        dataset_split="train" if training_eligible else "shadow",
        capture_kind="expert_contact" if training_eligible else "no_contact_shadow",
        training_eligible=training_eligible,
        surface_manifest_sha256="a" * 64,
        sensor_calibration_sha256="b" * 64,
        wrench_bias_sha256="c" * 64,
        normalization_sha256="d" * 64,
        action_profile_sha256="e" * 64,
        filter_profile_sha256="f" * 64,
        controller_identity_sha256="1" * 64,
        receiver_identity_sha256="2" * 64,
    )


def frame(index: int, *, applied_equal: bool = True) -> ExpertEpisodeFrame:
    expert = tuple(float(index + axis) for axis in range(12))
    applied = expert if applied_equal else (0.0,) * 12
    timestamp = 1.0 + index * 0.002
    return ExpertEpisodeFrame(
        episode_id="episode-001",
        sample_index=index,
        control_sequence=index + 1,
        control_timestamp_s=timestamp,
        external_source_sequence=100 + index,
        external_source_timestamp_s=timestamp - 0.0005,
        observation_84d=(float(index),) * 84,
        expert_action_12d=expert,
        applied_action_12d=applied,
        filtered_f_ff=(0.0,) * 6,
        filter_velocity=(0.0,) * 6,
        receiver_ack_sequence=index,
    )


def test_unknown_surface_reference_is_constant_z_and_inside_tube() -> None:
    tube = load_unknown_surface_tube(
        SURFACE_MANIFEST,
        anchor_pose_base=ANCHOR,
        normal_half_width_m=0.002,
    )
    trajectory, rows = generate_unknown_surface_references(
        tube,
        family="anchor_circle",
        seed=7,
        duration_s=8.0,
        capture_duration_s=2.0,
        canary_radius_m=0.001,
        rate_hz=500,
    )
    assert len(rows) == 1001
    assert trajectory.rate_hz == 500
    assert {row["height_source"] for row in rows} == {"fresh_episode_anchor_constant_z"}
    assert {row["normal_source"] for row in rows} == {
        "nominal_task_loading_axis_base_positive_z"
    }
    assert all(row["desired_pose_base"][2] == pytest.approx(ANCHOR[2]) for row in rows)
    assert all("cad" not in json.dumps(row).lower() for row in rows)
    for row in rows:
        tube.assert_actual_and_desired(ANCHOR, row["desired_pose_base"])


def test_tube_checks_actual_and_desired_separately() -> None:
    tube = load_unknown_surface_tube(
        SURFACE_MANIFEST,
        anchor_pose_base=ANCHOR,
        normal_half_width_m=0.002,
    )
    with pytest.raises(RuntimeError, match="actual_tcp_tube_normal_guard"):
        tube.assert_actual_and_desired(
            (*ANCHOR[:2], ANCHOR[2] + 0.003, *ANCHOR[3:]),
            ANCHOR,
        )
    with pytest.raises(RuntimeError, match="desired_tcp_tube_u_guard"):
        tube.assert_actual_and_desired(
            ANCHOR,
            (ANCHOR[0] + 1.0, *ANCHOR[1:]),
        )


def test_tube_accepts_equivalent_rotation_vector_near_pi() -> None:
    tube = load_unknown_surface_tube(
        SURFACE_MANIFEST,
        anchor_pose_base=ANCHOR,
        normal_half_width_m=0.002,
    )
    anchor_rotvec = ANCHOR[3:]
    theta = math.sqrt(sum(value * value for value in anchor_rotvec))
    equivalent_rotvec = tuple(
        -value * (2.0 * math.pi - theta) / theta for value in anchor_rotvec
    )
    equivalent = (*ANCHOR[:3], *equivalent_rotvec)
    assert rotation_vector_distance_rad(equivalent_rotvec, anchor_rotvec) < 1e-6
    tube.assert_actual_and_desired(equivalent, ANCHOR)


def test_expert_episode_writer_persists_full_84d_12d_and_hash_manifest(tmp_path) -> None:
    artifact = tmp_path / "episode.jsonl"
    manifest = tmp_path / "episode.manifest.json"
    writer = DurableExpertEpisodeWriter(artifact, bindings=bindings())
    writer.append(frame(0, applied_equal=False))
    writer.append(frame(1, applied_equal=False))
    payload = writer.finalize(manifest)
    assert payload["row_count"] == 2
    recovered_bindings, recovered = read_expert_episode(artifact)
    assert recovered_bindings.capture_kind == "no_contact_shadow"
    assert len(recovered[0].observation_84d) == 84
    assert len(recovered[0].expert_action_12d) == 12
    assert validate_expert_episode_manifest(artifact, manifest) == payload


def test_training_eligible_episode_requires_expert_action_to_be_applied(tmp_path) -> None:
    writer = DurableExpertEpisodeWriter(
        tmp_path / "episode.jsonl",
        bindings=bindings(training_eligible=True),
    )
    with pytest.raises(ValueError, match="expert/applied"):
        writer.append(frame(0, applied_equal=False))
    writer.close()


def test_manifest_tamper_is_rejected(tmp_path) -> None:
    artifact = tmp_path / "episode.jsonl"
    manifest = tmp_path / "episode.manifest.json"
    writer = DurableExpertEpisodeWriter(artifact, bindings=bindings())
    writer.append(frame(0, applied_equal=False))
    writer.finalize(manifest)
    payload = json.loads(manifest.read_text())
    payload["row_count"] = 99
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        validate_expert_episode_manifest(artifact, manifest)


def test_expert_episode_manifest_refuses_overwrite(tmp_path) -> None:
    artifact = tmp_path / "episode.jsonl"
    manifest = tmp_path / "episode.manifest.json"
    manifest.write_text("preserve", encoding="utf-8")
    writer = DurableExpertEpisodeWriter(artifact, bindings=bindings())
    writer.append(frame(0, applied_equal=False))
    with pytest.raises(FileExistsError, match="already exists"):
        writer.finalize(manifest)
    assert manifest.read_text(encoding="utf-8") == "preserve"


def test_builder_emits_hash_bound_two_second_canary_without_live_io(tmp_path) -> None:
    output = tmp_path / "reference.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--anchor-pose",
            *[str(value) for value in ANCHOR],
            "--tube-normal-half-width-m",
            "0.002",
            "--family",
            "anchor_circle",
            "--seed",
            "7",
            "--path-duration-s",
            "8",
            "--capture-duration-s",
            "2",
            "--canary-radius-m",
            "0.001",
            "--speed-scale",
            "0.55",
            "--target-load-n",
            "0",
            "--preload-n",
            "0",
            "--output",
            str(output),
        ],
        cwd=ROOT.parents[1],
        env={"PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    artifact = json.loads(output.read_text())
    assert report["live_actions"] is False
    assert artifact["cad_height_or_normal_feedforward"] is False
    assert artifact["trajectory"]["row_count"] == 1001
    assert artifact["trajectory"]["target_load_n"] == 0.0
    assert artifact["trajectory"]["initial_release_pose_ready"] is True
    assert artifact["trajectory"]["initial_translation_error_m"] == pytest.approx(0.0)
    before = hashlib.sha256(output.read_bytes()).hexdigest()
    repeated = subprocess.run(
        completed.args,
        cwd=ROOT.parents[1],
        env={"PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert repeated.returncode == 2
    assert "already exists" in repeated.stderr
    assert hashlib.sha256(output.read_bytes()).hexdigest() == before
