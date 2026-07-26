"""Focused pure-function tests for the offline manual CoG analyzer."""

from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
SPEC = importlib.util.spec_from_file_location("analyze_manual_cog_sweep", TOOLS / "analyze_manual_cog_sweep.py")
assert SPEC and SPEC.loader
ANALYZER = importlib.util.module_from_spec(SPEC)
sys.modules["analyze_manual_cog_sweep"] = ANALYZER
SPEC.loader.exec_module(ANALYZER)


def _observation(name: str, rotation: np.ndarray, mass: float, cog: np.ndarray, force_bias: np.ndarray, torque_bias: np.ndarray) -> object:
    gravity_sensor = rotation.T @ np.array([0.0, 0.0, -1.0])
    force = force_bias + mass * gravity_sensor
    torque = torque_bias + np.cross(mass * cog, gravity_sensor)
    return ANALYZER.PoseObservation(
        name=name,
        rotation_base_to_sensor=rotation,
        force_manual=force,
        torque_manual=torque,
        joint_mean_rad=np.zeros(6),
        joint_columns=tuple(f"actual_q_{i}" for i in range(6)),
        target_moment_nm=np.zeros(6),
        source_quality={},
    )


def test_kunwei_fit_recovers_mass_cog_and_biases() -> None:
    rotations = [
        np.eye(3),
        ANALYZER.rotvec_to_matrix([0.3, 0.0, 0.0]),
        ANALYZER.rotvec_to_matrix([0.0, 0.4, 0.0]),
        ANALYZER.rotvec_to_matrix([0.0, 0.0, 0.5]),
        ANALYZER.rotvec_to_matrix([0.2, -0.3, 0.1]),
        ANALYZER.rotvec_to_matrix([-0.4, 0.2, 0.3]),
    ]
    mass = 0.401
    cog = np.array([0.012, -0.018, 0.025])
    force_bias = np.array([0.11, -0.07, 0.03])
    torque_bias = np.array([0.004, -0.006, 0.002])
    observations = [_observation(f"P{i}", rotation, mass, cog, force_bias, torque_bias) for i, rotation in enumerate(rotations)]

    result = ANALYZER.fit_kunwei_model(observations)

    assert result["parameters"]["mass_kg"] == pytest.approx(mass)
    assert np.asarray(result["parameters"]["cog_sensor_m"]) == pytest.approx(cog)
    assert np.asarray(result["parameters"]["force_bias_manual"]) == pytest.approx(force_bias)
    assert np.asarray(result["parameters"]["torque_bias_manual"]) == pytest.approx(torque_bias)
    assert result["residuals"]["force"]["rms_vector_norm"] == pytest.approx(0.0, abs=1e-12)
    assert result["residuals"]["torque"]["rms_vector_norm"] == pytest.approx(0.0, abs=1e-12)


def test_split_is_frozen_and_has_no_held_out_leakage() -> None:
    split = ANALYZER.split_pose_names(["P8", "P0R", "P3", "P0", "P6", "P1", "P7", "P2", "P5", "P4"])
    assert split["training"] == list(ANALYZER.TRAINING_POSES)
    assert split["held_out"] == ["P0R", "P7", "P8"]
    assert set(split["training"]).isdisjoint(split["held_out"])
    assert split["all_unique_pose_refit"] == ["P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"]
    assert "P0R" not in split["all_unique_pose_refit"]


def test_split_rejects_missing_or_unexpected_actual_pose_names() -> None:
    with pytest.raises(ANALYZER.DataQualityError, match="missing"):
        ANALYZER.split_pose_names(list(ANALYZER.POSE_NAMES[:-1]))
    with pytest.raises(ANALYZER.DataQualityError, match="unexpected"):
        ANALYZER.split_pose_names(list(ANALYZER.POSE_NAMES) + ["requested_P9"])


def test_insufficient_and_rank_deficient_kunwei_data_fail() -> None:
    one = _observation("P0", np.eye(3), 0.4, np.array([0.0, 0.0, 0.02]), np.zeros(3), np.zeros(3))
    with pytest.raises(ValueError, match="insufficient|rank deficient"):
        ANALYZER.fit_kunwei_model([one])

    repeated = [_observation(f"P{i}", np.eye(3), 0.4, np.array([0.0, 0.0, 0.02]), np.zeros(3), np.zeros(3)) for i in range(6)]
    with pytest.raises(ValueError, match="rank deficient"):
        ANALYZER.fit_kunwei_model(repeated)


def test_pose_loader_uses_tcp_orientation_not_translation(
    tmp_path: Path,
) -> None:
    pose_dir = tmp_path / "P0"
    (pose_dir / "rtde").mkdir(parents=True)
    (pose_dir / "kunwei").mkdir()
    rtde_path = pose_dir / "rtde" / "normal_position_control_rtde.csv"
    rtde_columns = (
        [f"actual_q_{index}" for index in range(6)]
        + [f"actual_TCP_pose_{index}" for index in range(6)]
        + [f"target_moment_{index}" for index in range(6)]
    )
    rtde_values = (
        [0.0] * 6
        + [10.0, -20.0, 30.0, 0.0, 0.0, np.pi / 2.0]
        + [0.0] * 6
    )
    with rtde_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(rtde_columns)
        writer.writerow(rtde_values)
    kunwei_path = pose_dir / "kunwei" / "data.csv"
    with kunwei_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "Fx_kg_manual",
                "Fy_kg_manual",
                "Fz_kg_manual",
                "Mx_kg_m_manual",
                "My_kg_m_manual",
                "Mz_kg_m_manual",
            ]
        )
        writer.writerow([0.0] * 6)
    (pose_dir / "rtde" / "evidence.json").write_text(
        json.dumps({"sample_count": 1}),
        encoding="utf-8",
    )
    (pose_dir / "kunwei" / "summary.json").write_text(
        json.dumps({"samples": 1}),
        encoding="utf-8",
    )

    observation = ANALYZER.load_pose_observation(tmp_path, "P0")

    expected = ANALYZER.rotvec_to_matrix([0.0, 0.0, np.pi / 2.0])
    assert observation.rotation_base_to_sensor == pytest.approx(expected)


def test_ur_payload_fit_recovers_positive_mass_and_cog() -> None:
    coefficients = np.array([0.404, -0.00018, -0.00039, 0.01074])
    bare = np.array([0.0, 29.0, 28.0, 3.0, 0.0, 0.0])
    design = np.array(
        [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
            [1.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 1.0],
        ]
    )
    observations = []
    for index in range(2):
        observations.append(
            ANALYZER.PoseObservation(
                name=f"P{index}",
                rotation_base_to_sensor=np.eye(3),
                force_manual=np.zeros(3),
                torque_manual=np.zeros(3),
                joint_mean_rad=np.full(6, float(index)),
                joint_columns=tuple(f"actual_q_{axis}" for axis in range(6)),
                target_moment_nm=bare + design @ coefficients,
                source_quality={},
            )
        )

    result = ANALYZER.fit_ur_payload_model(
        observations,
        gravity_terms=lambda _q: (bare, design),
    )

    assert result["parameters"]["mass_kg"] == pytest.approx(coefficients[0])
    assert np.asarray(result["parameters"]["first_moment_kg_m"]) == pytest.approx(
        coefficients[1:]
    )
    assert np.asarray(result["parameters"]["cog_tool0_m"]) == pytest.approx(
        coefficients[1:] / coefficients[0]
    )


def test_quality_gate_rejects_low_rate_and_motion_evidence() -> None:
    observation = ANALYZER.PoseObservation(
        name="P0",
        rotation_base_to_sensor=np.eye(3),
        force_manual=np.zeros(3),
        torque_manual=np.zeros(3),
        joint_mean_rad=np.zeros(6),
        joint_columns=tuple(f"actual_q_{axis}" for axis in range(6)),
        target_moment_nm=np.zeros(6),
        source_quality={
            "rtde": {"row_count": 2500},
            "kunwei": {"row_count": 5000},
            "kunwei_summary_parse_errors": 0,
            "kunwei_summary_dropped_sync_bytes": 0,
            "kunwei_rate_hz": 900.0,
            "evidence": {
                "ok": True,
                "motion_performed": True,
                "maximum_joint_speed_rad_s": 0.0,
                "maximum_tcp_speed_m_s": 0.0,
                "maximum_tcp_translation_m": 0.0,
            },
        },
    )

    findings = ANALYZER._quality_findings([observation])

    assert any("motion_performed" in finding for finding in findings)
    assert any("below" in finding for finding in findings)
