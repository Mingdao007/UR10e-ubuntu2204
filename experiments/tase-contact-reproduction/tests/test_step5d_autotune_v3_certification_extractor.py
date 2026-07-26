from __future__ import annotations

from types import SimpleNamespace

import pytest

import extract_step5d_autotune_v3_certification as extractor
import promote_step5d_autotune_v3_stopping_bound_evidence as stopping
from ur10e_experiment_runtime.return_route import (
    RETURN_TELEMETRY_SCHEMA,
    analyze_source_exact_return_telemetry,
)


def _row(step, *, timestamp: float, state: int, speed: float) -> dict[str, str]:
    row = {
        "ur_timestamp": str(timestamp),
        "normal_force_n": "0.1",
        "force_norm_n": "0.2",
        "torque_norm_nm": "0.01",
        "sensor_ok": "1",
        "guard_reason": "",
        "command": str(step.command),
        "campaign_epoch": "7",
        "trial_id": str(step.trial_id),
        "execution_profile_id": str(extractor.EXECUTION_PROFILE_ID),
        "_step5d_certification_trigger_controller_timestamp_s": "",
        "ur_safety_mode": "1",
        "ur_actual_TCP_speed_0": str(speed),
        "ur_actual_TCP_speed_1": "0",
        "ur_actual_TCP_speed_2": "0",
    }
    for index in range(24, 34):
        row[f"ur_output_int_register_{index}"] = "0"
    for index in range(35, 48):
        row[f"ur_output_double_register_{index}"] = "0"
    row.update(
        {
            "ur_output_int_register_24": "7",
            "ur_output_int_register_25": str(step.trial_id),
            "ur_output_int_register_26": str(state),
            "ur_output_int_register_27": str(step.candidate_token),
            "ur_output_int_register_29": str(extractor.EXECUTION_PROFILE_ID),
            "ur_output_int_register_31": str(step.sample_index),
            "ur_output_int_register_32": "3",
        }
    )
    return row


def _stop_rows(step) -> tuple[dict[str, str], ...]:
    rows = tuple(
        _row(step, timestamp=timestamp, state=state, speed=speed)
        for timestamp, state, speed in (
            (99.999, 80, 0.006),
            (100.000, 80, 0.007),
            (100.002, 81, 0.006),
            (100.004, 82, 0.0014),
            (100.006, 85, 0.0004),
        )
    )
    for row in rows:
        row["_step5d_certification_trigger_controller_timestamp_s"] = "99.9995"
        row["ur_output_double_register_45"] = "100.001"
        row["ur_output_double_register_46"] = "100.003"
        row["ur_output_double_register_47"] = "100.005"
    return rows


def _return_rows(step) -> tuple[dict[str, str], ...]:
    rows = []
    for index in range(21):
        state = 83 if index == 0 else (85 if index == 20 else 84)
        segment = 1 if index < 7 else (2 if index < 14 else 3)
        row = _row(
            step,
            timestamp=200.0 + index * 0.002,
            state=state,
            speed=0.001,
        )
        row.update(
            {
                "ur_output_int_register_33": "127",
                "ur_output_double_register_35": f"40.{segment}",
                "ur_output_double_register_39": str(segment),
                "ur_output_double_register_40": "0.02",
                "ur_output_double_register_41": "0.1",
                "ur_output_double_register_42": "0.02",
                "ur_output_double_register_43": "0.1",
                "ur_output_double_register_44": "0.002",
            }
        )
        rows.append(row)
    return tuple(rows)


def test_stopping_extractor_retains_raw_samples_and_feeds_analyzer() -> None:
    procedures = ("direct_exact_stop", "stale_watchdog_exact_stop")
    trials = []
    for procedure in procedures:
        for sample_index in range(1, 4):
            step = SimpleNamespace(
                procedure=procedure,
                command=4 if procedure == procedures[0] else 5,
                sample_index=sample_index,
                trial_id=100 + sample_index,
                candidate_token=200 + sample_index,
            )
            unrelated = {name: "" for name in _stop_rows(step)[0]}
            selected = extractor._step_rows(
                (unrelated, *_stop_rows(step)), step=step, plant_epoch=7
            )
            trials.append(extractor._stop_trial(selected, step=step))
    payload = {
        "schema": stopping.RAW_SCHEMA,
        "candidate_stage_id": "step5d_strict_rnn_autotune_v3",
        "release_basis_fingerprint": "r" * 64,
        "deployment_fingerprint": "d" * 64,
        "plant_epoch": 7,
        "deployment_readback_sha256": "b" * 64,
        "certification_authorization_sha256": "a" * 64,
        "all_samples_retained": True,
        "trials": trials,
    }
    reaction, growth, deceleration = stopping._analyze_measurement(
        payload,
        expected_release_basis_fingerprint="r" * 64,
        expected_deployment_fingerprint="d" * 64,
        expected_plant_epoch=7,
        expected_deployment_readback_sha256="b" * 64,
        expected_authorization_sha256="a" * 64,
    )
    assert reaction == pytest.approx(0.0035)
    assert growth >= 0.0
    assert deceleration > 0.0


def test_return_extractor_selects_safe_closure_and_feeds_analyzer() -> None:
    step = SimpleNamespace(
        procedure="return_route",
        command=6,
        sample_index=1,
        trial_id=101,
        candidate_token=201,
    )
    selected = extractor._step_rows(_return_rows(step), step=step, plant_epoch=7)
    samples = extractor._return_telemetry_rows(selected)
    final = [row for row in selected if int(row["ur_output_int_register_26"]) == 85][-1]
    payload = {
        "schema": RETURN_TELEMETRY_SCHEMA,
        "candidate_stage_id": "step5d_strict_rnn_autotune_v3",
        "release_basis_fingerprint": "r" * 64,
        "deployment_fingerprint": "d" * 64,
        "source_binding_sha256": "s" * 64,
        "triplet_sha256": {"script": "1" * 64, "txt": "2" * 64, "urp": "3" * 64},
        "plant_epoch": 7,
        "deployment_readback_sha256": "b" * 64,
        "certification_authorization_sha256": "a" * 64,
        "all_samples_retained": True,
        "no_contact": True,
        "samples": samples,
        "final_readback": {
            "completed": True,
            "return_phase_echo": float(final["ur_output_double_register_35"]),
            "return_segment_id": int(final["ur_output_double_register_39"]),
            "return_guard_mask": int(final["ur_output_int_register_33"]),
            "max_angular_speed_rad_s": float(final["ur_output_double_register_42"]),
            "max_angular_acceleration_rad_s2": float(final["ur_output_double_register_43"]),
            "max_sample_gap_s": float(final["ur_output_double_register_44"]),
            "safety_mode": "NORMAL",
        },
    }
    summary = analyze_source_exact_return_telemetry(
        payload,
        expected_release_basis_fingerprint="r" * 64,
        expected_deployment_fingerprint="d" * 64,
        expected_source_binding_sha256="s" * 64,
        expected_triplet_sha256=payload["triplet_sha256"],
        expected_plant_epoch=7,
        expected_deployment_readback_sha256="b" * 64,
        expected_certification_authorization_sha256="a" * 64,
    )
    assert summary["segment_order"] == [1, 2, 3]
