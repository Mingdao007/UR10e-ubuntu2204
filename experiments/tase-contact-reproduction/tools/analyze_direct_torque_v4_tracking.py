#!/usr/bin/env python3
"""Offline directional-tracking audit for Direct Torque v4 RTDE captures.

The live runner historically reported ``max(||actual displacement||) /
max(||desired displacement||)``.  That ratio is useful as an envelope check,
but it can misclassify orthogonal drift as commanded response.  This tool keeps
the envelope metric and adds a signed projection onto the command direction,
orthogonal motion, and correlation/regression diagnostics.

It reads retained CSV files only.  It never imports a live robot transport.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np


TORQUE_STATE = 2


def _active_rows_with_coherence(
    rows: list[Mapping[str, str]],
) -> tuple[list[Mapping[str, str]], bool, int]:
    active = [
        row
        for row in rows
        if int(float(row["receiver_state"])) == TORQUE_STATE
    ]
    has_coherence_stamp = bool(active) and all(
        "action_echo_coherent" in row for row in active
    )
    if not has_coherence_stamp:
        return active, False, 0
    coherent = [
        row
        for row in active
        if row["action_echo_coherent"].strip().lower()
        in {"1", "true", "yes"}
    ]
    return coherent, True, len(active) - len(coherent)


def _matrix(
    rows: Iterable[Mapping[str, str]],
    prefix: str,
    width: int,
) -> np.ndarray:
    return np.asarray(
        [
            [float(row[f"{prefix}{axis}"]) for axis in range(width)]
            for row in rows
        ],
        dtype=float,
    )


def _centered_column_correlations(
    left: np.ndarray,
    right: np.ndarray,
) -> list[float]:
    correlations: list[float] = []
    for axis in range(left.shape[1]):
        left_column = left[:, axis]
        right_column = right[:, axis]
        if (
            float(np.std(left_column)) <= 1e-15
            or float(np.std(right_column)) <= 1e-15
        ):
            correlations.append(0.0)
        else:
            correlations.append(
                float(np.corrcoef(left_column, right_column)[0, 1])
            )
    return correlations


def _actuator_response_audit(
    active: list[Mapping[str, str]],
) -> dict[str, Any] | None:
    required = (
        "target_current",
        "actual_current",
        "actual_current_as_torque",
        "joint_control_output",
    )
    if not all(f"{name}_0" in active[0] for name in required):
        return None
    commanded = _matrix(active, "commanded_joint_torque_nm_", 6)
    signals = {name: _matrix(active, f"{name}_", 6) for name in required}
    joint_modes = (
        [
            sorted(
                {
                    int(float(row[f"joint_mode_{axis}"]))
                    for row in active
                }
            )
            for axis in range(6)
        ]
        if "joint_mode_0" in active[0]
        else None
    )
    return {
        "method": "rtde_motor_diagnostic_crosscheck",
        "sample_count": len(active),
        "per_joint_centered_command_vs_target_current_correlation": (
            _centered_column_correlations(
                commanded,
                signals["target_current"],
            )
        ),
        "per_joint_centered_command_vs_actual_current_correlation": (
            _centered_column_correlations(
                commanded,
                signals["actual_current"],
            )
        ),
        "per_joint_centered_command_vs_actual_current_as_torque_correlation": (
            _centered_column_correlations(
                commanded,
                signals["actual_current_as_torque"],
            )
        ),
        "per_joint_centered_command_vs_joint_control_output_correlation": (
            _centered_column_correlations(
                commanded,
                signals["joint_control_output"],
            )
        ),
        "joint_control_output_minus_target_current_max_abs": float(
            np.max(
                np.abs(
                    signals["joint_control_output"]
                    - signals["target_current"]
                )
            )
        ),
        "actual_current_as_torque_peak_to_peak_nm": [
            float(value)
            for value in np.ptp(signals["actual_current_as_torque"], axis=0)
        ],
        "joint_mode_values_by_joint": joint_modes,
        "interpretation_boundary": (
            "motor-current/torque diagnostics only; no UR internal F/T signal "
            "is used as wrench input or safety guard"
        ),
    }


def analyze_rows(rows: list[Mapping[str, str]]) -> dict[str, Any]:
    active, action_coherence_verified, rejected_incoherent_rows = (
        _active_rows_with_coherence(rows)
    )
    if len(active) < 3:
        raise ValueError("tracking audit requires at least three TORQUE rows")

    timestamp_s = np.asarray(
        [float(row["controller_timestamp_s"]) for row in active],
        dtype=float,
    )
    timestamp_s -= timestamp_s[0]
    if np.any(np.diff(timestamp_s) <= 0.0):
        raise ValueError("TORQUE timestamps must be strictly increasing")

    desired = _matrix(active, "command_desired_pose_", 3)
    actual = _matrix(active, "actual_TCP_pose_", 3)
    desired -= desired[0]
    actual -= actual[0]
    desired_norm = np.linalg.norm(desired, axis=1)
    actual_norm = np.linalg.norm(actual, axis=1)
    response_mask = timestamp_s >= 0.020 - 1e-12
    response_desired = desired[response_mask]
    response_actual = actual[response_mask]
    centered_desired = response_desired - np.mean(
        response_desired,
        axis=0,
        keepdims=True,
    )
    centered_actual = response_actual - np.mean(
        response_actual,
        axis=0,
        keepdims=True,
    )
    centered_denominator = float(
        np.sum(centered_desired * centered_desired)
    )
    response_alpha = float(
        np.sum(centered_desired * centered_actual)
        / centered_denominator
    )
    response_intercept = (
        np.mean(response_actual, axis=0)
        - response_alpha * np.mean(response_desired, axis=0)
    )
    response_prediction = (
        response_alpha * response_desired + response_intercept
    )
    response_residual = response_actual - response_prediction
    response_sse = float(np.sum(response_residual * response_residual))
    response_sst = float(np.sum(centered_actual * centered_actual))
    response_r2 = (
        1.0 - response_sse / response_sst if response_sst > 0.0 else 0.0
    )
    response_dof = max(1, 3 * len(response_desired) - 4)
    response_iid_standard_error = float(
        np.sqrt((response_sse / response_dof) / centered_denominator)
    )
    reversed_desired = centered_desired[::-1]
    reverse_null_alpha = float(
        np.sum(reversed_desired * centered_actual)
        / np.sum(reversed_desired * reversed_desired)
    )

    first_active_index = next(
        index
        for index, row in enumerate(rows)
        if int(float(row["receiver_state"])) == TORQUE_STATE
    )
    waiting_rows = [
        row
        for row in rows[:first_active_index]
        if int(float(row["receiver_state"])) == 0
    ]
    waiting_noise: dict[str, Any] | None = None
    if len(waiting_rows) >= 3:
        waiting_pose = _matrix(waiting_rows, "actual_TCP_pose_", 3)
        waiting_centered = waiting_pose - np.mean(
            waiting_pose,
            axis=0,
            keepdims=True,
        )
        waiting_noise = {
            "row_count": len(waiting_rows),
            "per_axis_peak_to_peak_m": [
                float(value) for value in np.ptp(waiting_pose, axis=0)
            ],
            "per_axis_rms_about_mean_m": [
                float(value)
                for value in np.sqrt(
                    np.mean(waiting_centered * waiting_centered, axis=0)
                )
            ],
            "translation_norm_rms_about_mean_m": float(
                np.sqrt(
                    np.mean(
                        np.sum(
                            waiting_centered * waiting_centered,
                            axis=1,
                        )
                    )
                )
            ),
        }

    endpoint_norm = float(np.linalg.norm(desired[-1]))
    if endpoint_norm <= 1e-9:
        raise ValueError("command endpoint is too small to define a direction")
    command_direction = desired[-1] / endpoint_norm
    desired_along = desired @ command_direction
    actual_along = actual @ command_direction
    desired_orthogonal = desired - desired_along[:, None] * command_direction
    actual_orthogonal = actual - actual_along[:, None] * command_direction

    through_origin_gain = float(
        np.dot(desired_along, actual_along)
        / np.dot(desired_along, desired_along)
    )
    intercept_design = np.column_stack(
        [np.ones_like(desired_along), desired_along]
    )
    intercept_m, intercept_gain = np.linalg.lstsq(
        intercept_design,
        actual_along,
        rcond=None,
    )[0]
    if float(np.std(actual_along)) <= 1e-15:
        correlation = 0.0
    else:
        correlation = float(np.corrcoef(desired_along, actual_along)[0, 1])
    time_design = np.column_stack(
        [np.ones_like(timestamp_s), timestamp_s, desired_along]
    )
    time_intercept_m, drift_m_s, time_controlled_gain = np.linalg.lstsq(
        time_design,
        actual_along,
        rcond=None,
    )[0]

    torque = _matrix(active, "commanded_joint_torque_nm_", 6)
    qd = _matrix(active, "actual_qd_", 6)
    qdd = np.diff(qd, axis=0) / np.diff(timestamp_s)[:, None]
    first_20ms_end = max(2, int(np.searchsorted(timestamp_s, 0.020)))
    actual_speed = _matrix(active, "actual_TCP_speed_", 3)
    stiffness = _matrix(active, "applied_k_", 3)
    translational_damping = 2.0 * np.sqrt(2.0 * stiffness)
    translational_wrench = (
        stiffness * (desired - actual)
        - translational_damping * actual_speed
    )

    desired_max_m = float(np.max(desired_norm))
    actual_max_m = float(np.max(actual_norm))
    legacy_ratio = actual_max_m / desired_max_m
    directional_tracking_supported = bool(
        correlation >= 0.2
        and intercept_gain > 0.05
        and time_controlled_gain > 0.05
    )

    return {
        "schema": "direct_torque_v4_directional_tracking_audit/v1",
        "active_row_count": len(active),
        "active_span_s": float(timestamp_s[-1]),
        "action_coherence_verified": action_coherence_verified,
        "rejected_incoherent_action_rows": rejected_incoherent_rows,
        "baseline": "first receiver_state=2 row",
        "command_direction_base_xyz": [
            float(value) for value in command_direction
        ],
        "envelope": {
            "desired_max_displacement_m": desired_max_m,
            "actual_max_displacement_m": actual_max_m,
            "legacy_max_norm_response_ratio": legacy_ratio,
            "desired_path_max_orthogonal_deviation_m": float(
                np.max(np.linalg.norm(desired_orthogonal, axis=1))
            ),
        },
        "directional": {
            "actual_along_max_m": float(np.max(actual_along)),
            "actual_along_min_m": float(np.min(actual_along)),
            "actual_along_endpoint_m": float(actual_along[-1]),
            "actual_orthogonal_rms_m": float(
                np.sqrt(
                    np.mean(np.sum(actual_orthogonal * actual_orthogonal, axis=1))
                )
            ),
            "through_origin_gain": through_origin_gain,
            "intercept_fit_gain": float(intercept_gain),
            "intercept_fit_offset_m": float(intercept_m),
            "command_actual_correlation": correlation,
            "time_controlled_gain": float(time_controlled_gain),
            "time_controlled_drift_m_s": float(drift_m_s),
            "time_controlled_intercept_m": float(time_intercept_m),
            "directional_tracking_supported": directional_tracking_supported,
            "support_rule": (
                "correlation>=0.2 and intercept_fit_gain>0.05 and "
                "time_controlled_gain>0.05"
            ),
        },
        "coherent_response_fit": {
            "entry_exclusion_s": 0.020,
            "sample_count": len(response_desired),
            "model": "actual_xyz=alpha*desired_xyz+intercept_xyz",
            "alpha": response_alpha,
            "intercept_base_xyz_m": [
                float(value) for value in response_intercept
            ],
            "r_squared": response_r2,
            "iid_standard_error": response_iid_standard_error,
            "iid_caveat": (
                "standard error assumes independent residuals and is optimistic "
                "when RTDE pose noise is autocorrelated"
            ),
            "time_reversed_desired_null_alpha": reverse_null_alpha,
        },
        "pre_torque_waiting_noise": waiting_noise,
        "entry_and_torque": {
            "first_torque_state_command_norm_nm": float(
                np.linalg.norm(torque[0])
            ),
            "maximum_command_norm_nm": float(
                np.max(np.linalg.norm(torque, axis=1))
            ),
            "maximum_abs_command_component_nm": float(np.max(np.abs(torque))),
            "first_20ms_maximum_abs_derived_joint_acceleration_rad_s2": float(
                np.max(np.abs(qdd[: first_20ms_end - 1]))
            ),
            "maximum_abs_derived_joint_acceleration_rad_s2": float(
                np.max(np.abs(qdd))
            ),
            "maximum_reconstructed_translation_wrench_norm_n": float(
                np.max(np.linalg.norm(translational_wrench, axis=1))
            ),
            "mean_reconstructed_translation_wrench_norm_n": float(
                np.mean(np.linalg.norm(translational_wrench, axis=1))
            ),
            "translation_wrench_formula": (
                "applied_k*(desired_xyz-actual_xyz)"
                "-2*sqrt(virtual_mass_2kg*applied_k)*actual_tcp_speed_xyz"
            ),
        },
        "actuator_response": _actuator_response_audit(active),
        "claim_boundary": {
            "offline_only": True,
            "contact_control_qualified": False,
            "training_dataset": False,
            "causal_kunwei_rtde_alignment_used": False,
            "action_echo_coherence_verified": action_coherence_verified,
            "ur_internal_ft_used": False,
            "kunwei_force_source_unchanged": True,
        },
    }


def _skew(vector: np.ndarray) -> np.ndarray:
    x, y, z = vector
    return np.asarray(
        [[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]],
        dtype=float,
    )


def analyze_calibrated_mapping(
    rows: list[Mapping[str, str]],
    *,
    sample_stride: int = 10,
) -> dict[str, Any]:
    """Cross-check J.T*w with the local calibrated Pinocchio model.

    This is not a read-back of the controller's internal ``get_jacobian``.
    It is an independent model cross-check using the current RTDE q/TCP rows.
    """

    import pinocchio as pin
    import step5c_calibrated_kinematics_audit as calibrated_kinematics

    active, action_coherence_verified, rejected_incoherent_rows = (
        _active_rows_with_coherence(rows)
    )
    if len(active) < 3:
        raise ValueError("mapping audit requires at least three TORQUE rows")
    model_bundle = calibrated_kinematics.build_calibrated_model()

    inferred_offsets = []
    for row in active[:: max(sample_stride * 10, 1)]:
        q = np.asarray(
            [float(row[f"actual_q_{axis}"]) for axis in range(6)],
            dtype=float,
        )
        placement = calibrated_kinematics.base_to_tool0(model_bundle, q)
        tcp = np.asarray(
            [float(row[f"actual_TCP_pose_{axis}"]) for axis in range(3)],
            dtype=float,
        )
        inferred_offsets.append(
            placement.rotation.T @ (tcp - placement.translation)
        )
    inferred_offset = np.mean(inferred_offsets, axis=0)

    reconstructed = []
    commanded = []
    wrench_rows = []
    for row in active[::sample_stride]:
        q = np.asarray(
            [float(row[f"actual_q_{axis}"]) for axis in range(6)],
            dtype=float,
        )
        desired = np.asarray(
            [float(row[f"command_desired_pose_{axis}"]) for axis in range(3)],
            dtype=float,
        )
        actual = np.asarray(
            [float(row[f"actual_TCP_pose_{axis}"]) for axis in range(3)],
            dtype=float,
        )
        speed = np.asarray(
            [float(row[f"actual_TCP_speed_{axis}"]) for axis in range(3)],
            dtype=float,
        )
        stiffness = np.asarray(
            [float(row[f"applied_k_{axis}"]) for axis in range(3)],
            dtype=float,
        )
        damping = 2.0 * np.sqrt(2.0 * stiffness)
        wrench = stiffness * (desired - actual) - damping * speed

        model = model_bundle.model
        data = model_bundle.data
        pin.forwardKinematics(model, data, q)
        pin.computeJointJacobians(model, data, q)
        pin.updateFramePlacements(model, data)
        tool_jacobian_world = pin.getFrameJacobian(
            model,
            data,
            model_bundle.tool0_frame_id,
            pin.ReferenceFrame.LOCAL_WORLD_ALIGNED,
        )
        base_world_rotation = data.oMf[
            model_bundle.base_frame_id
        ].rotation.T
        tool_linear_base = base_world_rotation @ tool_jacobian_world[:3, :]
        tool_angular_base = base_world_rotation @ tool_jacobian_world[3:, :]
        tool0_base = (
            data.oMf[model_bundle.base_frame_id].inverse()
            * data.oMf[model_bundle.tool0_frame_id]
        )
        tcp_lever_base = tool0_base.rotation @ inferred_offset
        tcp_linear_base = (
            tool_linear_base
            - _skew(tcp_lever_base) @ tool_angular_base
        )
        reconstructed.append(tcp_linear_base.T @ wrench)
        commanded.append(
            [
                float(row[f"commanded_joint_torque_nm_{axis}"])
                for axis in range(6)
            ]
        )
        wrench_rows.append(wrench)

    reconstructed_array = np.asarray(reconstructed, dtype=float)
    commanded_array = np.asarray(commanded, dtype=float)
    wrench_array = np.asarray(wrench_rows, dtype=float)
    residual = commanded_array - reconstructed_array
    correlations = []
    for axis in range(6):
        if float(np.std(reconstructed_array[:, axis])) <= 1e-15:
            correlations.append(0.0)
        else:
            correlations.append(
                float(
                    np.corrcoef(
                        reconstructed_array[:, axis],
                        commanded_array[:, axis],
                    )[0, 1]
                )
            )

    return {
        "method": "calibrated_pinocchio_tcp_jacobian_crosscheck",
        "controller_internal_jacobian_readback": False,
        "action_echo_coherence_verified": action_coherence_verified,
        "rejected_incoherent_action_rows": rejected_incoherent_rows,
        "calibration_hash": model_bundle.calibration_hash,
        "sample_stride": sample_stride,
        "sample_count": len(commanded_array),
        "inferred_tcp_offset_tool0_m": [
            float(value) for value in inferred_offset
        ],
        "inferred_tcp_offset_norm_m": float(np.linalg.norm(inferred_offset)),
        "inferred_tcp_offset_max_std_m": float(
            np.max(np.std(inferred_offsets, axis=0))
        ),
        "maximum_translation_wrench_norm_n": float(
            np.max(np.linalg.norm(wrench_array, axis=1))
        ),
        "maximum_reconstructed_translation_torque_norm_nm": float(
            np.max(np.linalg.norm(reconstructed_array, axis=1))
        ),
        "maximum_commanded_torque_norm_nm": float(
            np.max(np.linalg.norm(commanded_array, axis=1))
        ),
        "rms_residual_torque_norm_nm": float(
            np.sqrt(np.mean(np.sum(residual * residual, axis=1)))
        ),
        "per_joint_reconstructed_vs_commanded_correlation": correlations,
        "endpoint_reconstructed_translation_torque_nm": [
            float(value) for value in reconstructed_array[-1]
        ],
        "endpoint_commanded_torque_nm": [
            float(value) for value in commanded_array[-1]
        ],
        "interpretation_boundary": (
            "translation-only reconstruction; residual also contains rotational "
            "impedance, Coriolis, and joint damping"
        ),
    }


def analyze_csv(
    path: Path,
    *,
    calibrated_mapping: bool = False,
) -> dict[str, Any]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    result = analyze_rows(rows)
    result["source_csv"] = str(path)
    if calibrated_mapping:
        result["calibrated_mapping_crosscheck"] = analyze_calibrated_mapping(rows)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--calibrated-mapping", action="store_true")
    args = parser.parse_args()
    result = analyze_csv(
        args.csv,
        calibrated_mapping=args.calibrated_mapping,
    )
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(text, end="")
    else:
        args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
