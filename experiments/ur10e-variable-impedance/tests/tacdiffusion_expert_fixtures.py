"""Synthetic JSON fixtures for exercising the expert lineage validators."""

from __future__ import annotations

import json
from pathlib import Path

from ur10e_vic.tacdiffusion.expert_data import (
    ExpertTraceManifestV2,
    sha256_file,
)


SHA = "a" * 64


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_expert_trace_manifest(
    root: Path,
    episode: str,
    split: str,
    *,
    sample_count: int = 2,
    controller_contract_valid: bool = True,
) -> Path:
    controller = _write(
        root / f"{episode}.controller.json",
        {
            "schema": "ur10e_controller_runtime_contract/v1",
            "polyscope_version": "5.26.0.140462",
            "probe_mode": "read_only",
            "controller_verified": False,
            "identity": {"controller_serial": "controller", "robot_serial": "robot", "evidence_sha256": SHA},
            "urcaps": [{"name": "external_control", "compatibility": "compatible", "evidence_sha256": SHA}],
            "configuration_artifacts": [{"role": role, "sha256": SHA} for role in ("installation", "safety_configuration", "tcp_payload")],
            "calibration_artifacts": [{"role": role, "sha256": SHA} for role in ("robot_calibration", "sensor_calibration", "sensor_to_tcp_transform")],
            "runtime_readback": {"safety_status": "NORMAL", "program_state": "STOPPED", "dashboard": True, "rtde": True, "network": True, "evidence_sha256": SHA},
            "direct_torque_api_readback": {"available_apis": ["direct_torque_v2", "get_jacobian", "get_coriolis_and_centrifugal_torques"], "evidence_sha256": SHA if controller_contract_valid else ""},
            "actions_observed": {"program_played": False, "bridge_started": False, "torque_sent": False, "force_torque_zeroed": False, "robot_motion_commanded": False, "controller_setting_written": False},
            "authorization": {"live_motion": False, "contact": False, "model_active": False, "upload": False},
        },
    )
    calibration = _write(
        root / f"{episode}.calibration.json",
        {"schema": "ur10e_sensor_calibration/v1", "sensor_serial": "kunwei-test", "sensor_frame_id": "kunwei_sensor", "axis_scale": [1.0] * 6, "axis_sign": [1.0] * 6, "source_evidence_sha256": SHA},
    )
    transform = _write(
        root / f"{episode}.transform.json",
        {"schema": "ur10e_sensor_to_tcp_transform/v1", "from_frame_id": "kunwei_sensor", "to_frame_id": "tool0_tcp", "translation_m": [0.0, 0.0, 0.1], "rotation_row_major": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0], "source_evidence_sha256": SHA},
    )
    calibration_hash = sha256_file(calibration)
    transform_hash = sha256_file(transform)
    bias = _write(
        root / f"{episode}.bias.json",
        {"schema": "ur10e_wrench_bias/v1", "sensor_serial": "kunwei-test", "sensor_frame_id": "kunwei_sensor", "bias_wrench_sensor_si": [0.0] * 6, "standard_deviation_si": [0.01] * 6, "sample_count": 1000, "started_at_s": 0.0, "ended_at_s": 1.0, "source_trace_sha256": SHA, "sensor_calibration_sha256": calibration_hash, "sensor_to_tcp_transform_sha256": transform_hash},
    )
    definition = _write(
        root / f"{episode}.expert-definition.json",
        {"schema": "ur10e_expert_force_definition/v1", "definition_id": "task_zft_minus_impedance_term_v1", "stiffness": [100.0, 100.0, 100.0, 10.0, 10.0, 10.0], "damping": [20.0, 20.0, 20.0, 2.0, 2.0, 2.0], "component_abs_max": [20.0, 20.0, 20.0, 2.0, 2.0, 2.0], "source_code_sha256": SHA, "canonical_frame_id": "tool0_tcp"},
    )
    bias_hash = sha256_file(bias)
    definition_hash = sha256_file(definition)
    zft = _write(
        root / f"{episode}.task-zft.json",
        {"schema": "ur10e_task_zft/v1", "episode_id": episode, "canonical_frame_id": "tool0_tcp", "timestamps_s": [0.0, 0.002], "wrench_tcp_si": [[0.0] * 6, [0.0] * 6], "source_definition_sha256": definition_hash, "sensor_calibration_sha256": calibration_hash, "sensor_to_tcp_transform_sha256": transform_hash, "wrench_bias_sha256": bias_hash},
    )
    artifacts = {
        "controller_contract": controller,
        "sensor_calibration": calibration,
        "sensor_to_tcp_transform": transform,
        "wrench_bias": bias,
        "task_zft": zft,
        "expert_force_definition": definition,
    }
    manifest = ExpertTraceManifestV2(
        trace_id=episode,
        source_kind="ur10e_expert_demonstration",
        dataset_split=split,
        controller_profile="polyscope-5.26-direct-torque-v2-500hz",
        canonical_frame_id="tool0_tcp",
        sample_count=sample_count,
        artifact_bindings={role: {"path": path.name, "sha256": sha256_file(path)} for role, path in artifacts.items()},
        software_sha256="3" * 64,
        package_sha256="4" * 64,
        trace_sha256="5" * 64,
        claim_boundary="ur10e_expert_force_labels",
    )
    payload = manifest.canonical_payload()
    payload["fingerprint_sha256"] = manifest.fingerprint_sha256
    return _write(root / f"{episode}.trace-manifest.json", payload)
