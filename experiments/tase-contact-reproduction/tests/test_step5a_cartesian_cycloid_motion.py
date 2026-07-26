#!/usr/bin/env python3
"""Focused offline tests for the Step5a Cartesian cycloid live entrypoint."""

from __future__ import annotations

import math
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
from sensor_msgs.msg import JointState

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_example_controllers"))

from ur10e_example_controllers.no_contact_cycloid_shadow import DEFAULT_CONFIG, _iter_reference_rows, load_no_contact_config  # noqa: E402
from ur10e_example_controllers.step5a_gate_a_audit import audit_gate_a_run  # noqa: E402
from ur10e_example_controllers.step5a_cartesian_cycloid_motion import (  # noqa: E402
    DEFAULT_GATE_A_POSITION_ERROR_LIMIT_M,
    DEFAULT_JOINT_HISTORY_MAX_SAMPLES,
    EXPECTED_CALIBRATION_HASH,
    JOINT_NAMES,
    ObservedJointSample,
    Step5aCartesianCycloidMotion,
    augment_trace_with_observed_fk,
    build_calibrated_model,
    build_cartesian_cycloid_trajectory,
    fk_tool0_base,
    step5a_acceptance,
    validate_cartesian_acceptance_summary,
)
from ur10e_example_controllers.step5a_historical_fixed_z_motion import (  # noqa: E402
    DEFAULT_CONFIG as HISTORICAL_FIXED_Z_CONFIG,
    Step5aHistoricalFixedZMotion,
    achieved_speed_hard_cap_m_s,
    build_historical_fixed_z_trajectory,
    build_positioning_trajectory,
    cartesian_task_space_spec,
    fixed_z_start_precheck,
    fixed_z_target_active_tcp_z_m,
    historical_visual_achieved_speed_gate_ok,
    validate_cartesian_task_space_spec,
)
from ur10e_example_controllers.step5a_return_to_anchor_motion import (  # noqa: E402
    build_return_trajectory,
    load_source_summary,
    source_start_positions,
)


class Step5aCartesianCycloidMotionTest(unittest.TestCase):
    def test_calibrated_model_and_full_reference_are_step5a_shape(self) -> None:
        model = build_calibrated_model()
        self.assertEqual(model.calibration_hash, EXPECTED_CALIBRATION_HASH)
        self.assertEqual(model.model.nq, 6)
        self.assertEqual(model.model.nv, 6)
        config = load_no_contact_config(DEFAULT_CONFIG)
        rows = _iter_reference_rows(config)
        self.assertEqual(len(rows), 1101)
        self.assertAlmostEqual(float(rows[-1]["phase_rad"]), 6.0, places=12)
        self.assertLessEqual(max(float(row["reference_speed_m_s"]) for row in rows), float(config["velocity_cap_m_s"]))

    def test_short_cartesian_ik_trajectory_is_continuous(self) -> None:
        model = build_calibrated_model()
        config = load_no_contact_config(DEFAULT_CONFIG)
        short_config = deepcopy(config)
        short_config["duration_s"] = 0.2
        short_config["final_phase_rad"] = float(config["omega_rad_s"]) * 0.2
        start = [0.0, -1.57, 1.57, -1.57, -1.57, 0.0]
        points, trace_rows, metrics = build_cartesian_cycloid_trajectory(short_config, model, start)
        self.assertEqual(len(points), 11)
        self.assertEqual(len(trace_rows), 11)
        self.assertLess(metrics["max_commanded_position_error_m"], 5e-4)
        self.assertTrue(math.isfinite(metrics["max_commanded_fk_speed_m_s"]))
        self.assertIn("reference_final_offset_xyz_m", metrics)
        self.assertIn("commanded_net_displacement_norm_m", metrics)
        for previous, current in zip(points, points[1:]):
            jump = max(abs(a - b) for a, b in zip(previous.positions, current.positions))
            self.assertLess(jump, 0.02)
        for field in [f"command_{name}_rad" for name in JOINT_NAMES]:
            self.assertIn(field, trace_rows[0])
        self.assertIn("commanded_fk_x_m", trace_rows[0])

    def test_acceptance_summary_requires_all_gate_a_conditions(self) -> None:
        payload = {
            "role": "step5a_live_no_contact_cartesian_cycloid",
            "motion_kind": "cartesian_cycloid",
            "rows": 1101,
            "phase_final": 6.0,
            "sent_goal": True,
            "accepted": True,
            "result_error_code": 0,
            "kunwei_artifact_ok": True,
            "velocity_cap_m_s": 0.009,
            "max_reference_speed_m_s": 0.009,
            "max_commanded_fk_speed_m_s": 0.009,
            "max_achieved_speed_m_s": 0.009,
            "max_cartesian_position_error_m": 0.001,
            "anchor_pose_base": {"frame": "base_to_tool0"},
            "ik_source": {"solver": "pinocchio_calibrated_tool0_warm_start_deterministic_dls"},
            "contact_policy": {"force_control": False, "contact_search": False},
            "kunwei_monitor": {
                "ok": True,
                "stream_start_command_sent": True,
                "stream_stop_command_sent": True,
                "failure_reason": None,
            },
            "trace_alignment": {"trace_alignment_ok": True},
            "gate_a_thresholds": {
                "velocity_cap_m_s": 0.009,
                "cartesian_position_error_limit_m": DEFAULT_GATE_A_POSITION_ERROR_LIMIT_M,
                "trace_max_sample_gap_s": 0.08,
            },
        }
        self.assertTrue(validate_cartesian_acceptance_summary(dict(payload)))
        self.assertTrue(step5a_acceptance(dict(payload))["gate_a_pass"])

        bad = dict(payload)
        bad["motion_kind"] = "joint_proxy_cycloid_timing_not_cartesian_cycloid"
        self.assertFalse(validate_cartesian_acceptance_summary(bad))
        self.assertIn("cartesian_shape", bad["acceptance"]["failed_conditions"])

        bad = dict(payload)
        bad["rows"] = 1100
        self.assertFalse(validate_cartesian_acceptance_summary(bad))
        self.assertIn("cartesian_shape", bad["acceptance"]["failed_conditions"])

        bad = dict(payload)
        bad["max_reference_speed_m_s"] = 0.010
        self.assertFalse(validate_cartesian_acceptance_summary(bad))
        self.assertIn("speed_cap", bad["acceptance"]["failed_conditions"])

        bad = dict(payload)
        bad["kunwei_artifact_ok"] = False
        bad["kunwei_monitor"] = {"ok": False}
        self.assertFalse(validate_cartesian_acceptance_summary(bad))
        self.assertIn("kunwei_evidence", bad["acceptance"]["failed_conditions"])

        bad = dict(payload)
        bad["max_cartesian_position_error_m"] = 0.059225
        self.assertFalse(validate_cartesian_acceptance_summary(bad))
        self.assertIn("cartesian_equivalence", bad["acceptance"]["failed_conditions"])

    def test_trace_alignment_anchors_row0_and_interpolates_observed_fk(self) -> None:
        model = build_calibrated_model()
        config = load_no_contact_config(DEFAULT_CONFIG)
        short_config = deepcopy(config)
        short_config["duration_s"] = 0.2
        short_config["final_phase_rad"] = float(config["omega_rad_s"]) * 0.2
        start = [0.0, -1.57, 1.57, -1.57, -1.57, 0.0]
        _, trace_rows, _ = build_cartesian_cycloid_trajectory(short_config, model, start)
        send_start = 100.0
        samples = []
        for row in trace_rows[::2]:
            positions = [float(row[f"command_{name}_rad"]) for name in JOINT_NAMES]
            samples.append(ObservedJointSample(send_start + float(row["t_rel_s"]), None, positions))
        positions = [float(trace_rows[-1][f"command_{name}_rad"]) for name in JOINT_NAMES]
        samples.append(ObservedJointSample(send_start + float(trace_rows[-1]["t_rel_s"]), None, positions))

        alignment = augment_trace_with_observed_fk(
            trace_rows,
            model,
            samples,
            send_start,
            start,
            max_sample_gap_s=0.08,
        )
        self.assertTrue(alignment["trace_alignment_ok"])
        self.assertLessEqual(alignment["row0_anchor_error_m"], 1e-9)
        self.assertEqual(float(trace_rows[0]["cartesian_error_m"]), 0.0)
        self.assertIn("observed_sample_t_rel_s", trace_rows[0])
        self.assertIn("observed_shoulder_pan_joint_rad", trace_rows[0])

    def test_full_step5a_reference_endpoint_displacement_is_explicit(self) -> None:
        model = build_calibrated_model()
        config = load_no_contact_config(DEFAULT_CONFIG)
        start = [0.0, -1.57, 1.57, -1.57, -1.57, 0.0]
        _, _, metrics = build_cartesian_cycloid_trajectory(config, model, start)

        self.assertFalse(metrics["reference_returns_to_anchor"])
        self.assertEqual(metrics["cartesian_reference_frame"]["mode"], "step5_safe_frame_local_xy_to_base_xy_offset")
        self.assertAlmostEqual(metrics["reference_local_final_offset_xy_m"][0], 0.06279415498198926, places=12)
        self.assertAlmostEqual(metrics["reference_local_final_offset_xy_m"][1], 0.00039829713349634025, places=12)
        self.assertAlmostEqual(metrics["reference_final_offset_xyz_m"][0], -0.0010755492808608481, places=12)
        self.assertAlmostEqual(metrics["reference_final_offset_xyz_m"][1], 0.06278620656046259, places=12)
        self.assertGreater(metrics["commanded_net_displacement_norm_m"], 0.06)
        self.assertLess(metrics["commanded_net_displacement_norm_m"], 0.07)

    def test_joint_state_history_default_retains_full_step5a_window(self) -> None:
        self.assertGreaterEqual(DEFAULT_JOINT_HISTORY_MAX_SAMPLES, 50000)
        node = Step5aCartesianCycloidMotion.__new__(Step5aCartesianCycloidMotion)
        node.args = SimpleNamespace(joint_history_max_samples=DEFAULT_JOINT_HISTORY_MAX_SAMPLES)
        node.joint_history = []
        node.joint_state = None
        msg = JointState()
        msg.name = list(JOINT_NAMES)
        msg.position = [0.1 * index for index, _ in enumerate(JOINT_NAMES)]

        for _ in range(6001):
            Step5aCartesianCycloidMotion._on_joint_state(node, msg)

        self.assertEqual(len(node.joint_history), 6001)

    def test_joint_state_history_respects_explicit_limit(self) -> None:
        node = Step5aCartesianCycloidMotion.__new__(Step5aCartesianCycloidMotion)
        node.args = SimpleNamespace(joint_history_max_samples=3)
        node.joint_history = []
        node.joint_state = None
        msg = JointState()
        msg.name = list(JOINT_NAMES)
        msg.position = [0.1 * index for index, _ in enumerate(JOINT_NAMES)]

        for _ in range(5):
            Step5aCartesianCycloidMotion._on_joint_state(node, msg)

        self.assertEqual(len(node.joint_history), 3)

    def test_return_to_anchor_trajectory_is_separate_low_speed_joint_return(self) -> None:
        model = build_calibrated_model()
        source = {
            "start_positions": {
                name: value
                for name, value in zip(JOINT_NAMES, [0.1, -1.2, 1.3, -0.8, 1.5, -1.4])
            }
        }
        target = source_start_positions(source)
        current = [value + 0.03 for value in target]
        points, trace_rows, metrics = build_return_trajectory(
            model,
            current,
            target,
            dt=0.02,
            min_duration_s=2.0,
            max_joint_speed_rad_s=0.05,
            max_cartesian_speed_m_s=0.004,
        )

        self.assertEqual(list(points[0].positions), current)
        self.assertEqual(list(points[-1].positions), target)
        self.assertGreaterEqual(metrics["duration_s"], 2.0)
        self.assertLessEqual(metrics["max_commanded_fk_speed_m_s"], 0.004 + 1e-12)
        self.assertEqual(len(points), len(trace_rows))

    def test_return_to_anchor_prefers_historical_position_source_summary(self) -> None:
        path_source = {
            "motion_kind": "historical_fixed_z_cartesian_cycloid",
            "start_positions": {
                name: value
                for name, value in zip(JOINT_NAMES, [0.1, -1.2, 1.3, -0.8, 1.5, -1.4])
            },
        }
        position_source = {
            "motion_kind": "fixed_z_start_positioning",
            "start_positions": {
                name: value
                for name, value in zip(JOINT_NAMES, [0.2, -1.1, 1.2, -0.7, 1.4, -1.3])
            },
        }
        with TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "step5a_historical_fixed_z.json").write_text(json.dumps(path_source), encoding="utf-8")
            (run_dir / "step5a_historical_fixed_z_position.json").write_text(json.dumps(position_source), encoding="utf-8")

            loaded = load_source_summary(run_dir)

        self.assertEqual(loaded["motion_kind"], "fixed_z_start_positioning")
        self.assertTrue(loaded["_source_summary_path"].endswith("step5a_historical_fixed_z_position.json"))
        self.assertEqual(source_start_positions(loaded), [0.2, -1.1, 1.2, -0.7, 1.4, -1.3])

    def test_historical_fixed_z_path_preserves_full_cartesian_task_spec(self) -> None:
        model = build_calibrated_model()
        config = load_no_contact_config(HISTORICAL_FIXED_Z_CONFIG)
        start = [
            0.5743721127510071,
            -1.1999615293792267,
            -2.6663639545440674,
            -0.8600547474673768,
            1.5672391653060913,
            -1.3829334417926233,
        ]
        points, trace_rows, metrics = build_historical_fixed_z_trajectory(config, model, start)
        spec = cartesian_task_space_spec(config, metrics)

        self.assertEqual(
            spec["local_control_spec_source"]["spec_path"],
            str(WORKSPACE / "experiments/tase-contact-reproduction/config/step5a_local_control_spec.json"),
        )
        self.assertEqual(spec["local_control_spec_source"]["active_baseline"], "step5a_cycloid_no_contact_v3")
        self.assertTrue(spec["local_control_spec_source"]["source_scripts"]["active_baseline"].endswith("step5a_cycloid_no_contact_v3.script"))
        self.assertEqual(len(points), 751)
        self.assertEqual(len(trace_rows), 751)
        self.assertEqual(spec["reference_frame"]["mode"], "step5_safe_frame_local_xy_to_base_xy_offset")
        self.assertEqual(spec["reference_frame"]["local_x_axis"], "u_along_xy")
        self.assertEqual(spec["reference_frame"]["local_y_axis"], "p_lateral_xy")
        self.assertAlmostEqual(float(config["fixed_base_z_m"]), 0.029423891, places=12)
        self.assertEqual(config["fixed_base_z_frame"], "historical_ur_actual_tcp_pose")
        self.assertAlmostEqual(float(config["visual_air_gap_above_fixed_z_m"]), 0.010, places=12)
        self.assertAlmostEqual(fixed_z_target_active_tcp_z_m(config), 0.039423891, places=12)
        self.assertAlmostEqual(float(config["amplitude_m"]), 0.015, places=12)
        self.assertAlmostEqual(float(config["final_phase_rad"]), 6.0, places=12)
        self.assertAlmostEqual(float(config["duration_s"]), 15.0, places=12)
        self.assertAlmostEqual(float(config["omega_rad_s"]), 0.4, places=12)
        self.assertAlmostEqual(float(config["sample_period_s"]), 0.02, places=12)
        self.assertAlmostEqual(float(config["legacy_v3_command_clamp_m_s"]), 0.009, places=12)
        self.assertAlmostEqual(float(config["visual_gate_expected_reference_speed_peak_m_s"]), 0.012, places=12)
        self.assertAlmostEqual(achieved_speed_hard_cap_m_s(config), 0.015, places=12)
        self.assertEqual(config["achieved_speed_policy"], "hard_gate_for_historical_15s_visual_gate")
        self.assertAlmostEqual(metrics["max_reference_speed_m_s"], 0.012, places=5)
        self.assertGreater(metrics["max_reference_speed_m_s"], float(config["legacy_v3_command_clamp_m_s"]))
        self.assertIn("reference_base_offset_x_m", trace_rows[0])
        self.assertIn("reference_base_x_m", trace_rows[0])
        self.assertAlmostEqual(float(trace_rows[0]["reference_base_z_m"]), 0.039423891, places=12)
        self.assertEqual(metrics["target_pose_base"]["frame"], "base_to_active_tcp")
        self.assertEqual(metrics["target_tool0_pose_base"]["frame"], "base_to_tool0")
        self.assertNotAlmostEqual(
            metrics["target_tool0_pose_base"]["position_xyz_m"][2],
            float(config["fixed_base_z_m"]),
            places=3,
        )
        self.assertAlmostEqual(metrics["reference_local_final_offset_xy_m"][0], 0.09419123247298389, places=12)
        self.assertEqual(spec["endpoint_semantics"], "non_returning_historical_step5a_endpoint")
        self.assertEqual(spec["timing_law"], "linear_time_phase")
        self.assertEqual(spec["acceleration_profile_or_bound"]["acceleration_bound_m_s2"], 0.300)
        self.assertEqual(spec["caps"]["legacy_v3_command_clamp_m_s"], 0.009)
        self.assertEqual(
            spec["caps"]["legacy_v3_command_clamp_role"],
            "tp_v3_command_clamp_provenance_not_ros2_achieved_speed_hard_gate",
        )
        self.assertEqual(spec["caps"]["achieved_speed_hard_cap_m_s"], 0.015)
        self.assertEqual(spec["caps"]["achieved_speed_policy"], "hard_gate_for_historical_15s_visual_gate")
        self.assertEqual(
            spec["return_semantics"]["combo_return_target_source"],
            "step5a_historical_fixed_z_position.json:start_positions_pre_cycle",
        )
        self.assertEqual(spec["control_frame"], "active_tcp")
        self.assertTrue(spec["bench_proven_corrections"]["safe_frame_xy_remapping"])
        self.assertEqual(spec["bench_proven_corrections"]["calibration_hash"], EXPECTED_CALIBRATION_HASH)
        self.assertEqual(spec["bench_proven_corrections"]["fixed_base_z_frame"], "historical_ur_actual_tcp_pose")
        self.assertAlmostEqual(spec["bench_proven_corrections"]["visual_air_gap_above_fixed_z_m"], 0.010, places=12)
        alignment = spec["local_control_textbook_alignment"]
        self.assertEqual(alignment["amplitude_m"]["status"], "preserved")
        self.assertEqual(alignment["frame_map_choice"]["status"], "changed_with_reason")
        self.assertEqual(alignment["frame_map_choice"]["tp_v3_choice"], "affine_map_exact_to_shifted_drag_teach_start_mid_end")
        self.assertEqual(alignment["frame_map_choice"]["ros2_choice"], "step5_safe_frame_local_xy_to_base_xy_offset")
        self.assertEqual(alignment["tp_v3_command_clamp"]["status"], "changed_with_reason")
        self.assertEqual(alignment["endpoint_semantics"]["status"], "preserved")
        self.assertEqual(alignment["combo_return"]["status"], "out_of_scope")
        self.assertTrue(validate_cartesian_task_space_spec(spec))

    def test_cartesian_task_space_spec_rejects_silent_textbook_drift(self) -> None:
        model = build_calibrated_model()
        config = load_no_contact_config(HISTORICAL_FIXED_Z_CONFIG)
        start = [
            0.5743721127510071,
            -1.1999615293792267,
            -2.6663639545440674,
            -0.8600547474673768,
            1.5672391653060913,
            -1.3829334417926233,
        ]
        _, _, metrics = build_historical_fixed_z_trajectory(config, model, start)
        spec = cartesian_task_space_spec(config, metrics)

        missing_source = deepcopy(spec)
        del missing_source["local_control_spec_source"]
        with self.assertRaisesRegex(ValueError, "local_control_spec_source"):
            validate_cartesian_task_space_spec(missing_source)

        silent_frame_swap = deepcopy(spec)
        silent_frame_swap["local_control_textbook_alignment"]["frame_map_choice"]["status"] = "preserved"
        with self.assertRaisesRegex(ValueError, "frame map differs"):
            validate_cartesian_task_space_spec(silent_frame_swap)

        bad_speed_policy = deepcopy(spec)
        bad_speed_policy["caps"]["achieved_speed_policy"] = "hard_gate_at_tp_v3_command_clamp"
        with self.assertRaisesRegex(ValueError, "achieved-speed policy"):
            validate_cartesian_task_space_spec(bad_speed_policy)

        bad_return_source = deepcopy(spec)
        bad_return_source["return_semantics"]["combo_return_target_source"] = "step5a_historical_fixed_z.json:start_positions"
        with self.assertRaisesRegex(ValueError, "combo return target source"):
            validate_cartesian_task_space_spec(bad_return_source)

    def test_historical_fixed_z_positioning_and_path_precheck_are_separate(self) -> None:
        model = build_calibrated_model()
        config = load_no_contact_config(HISTORICAL_FIXED_Z_CONFIG)
        high_start = [
            0.5743721127510071,
            -1.1999615293792267,
            -2.6663639545440674,
            -0.8600547474673768,
            1.5672391653060913,
            -1.3829334417926233,
        ]
        current_pose = fk_tool0_base(model, np.array(high_start, dtype=float))
        self.assertFalse(fixed_z_start_precheck(config, current_pose, 0.003)["ok"])

        points, trace_rows, metrics = build_positioning_trajectory(
            config,
            model,
            high_start,
            max_cartesian_speed_m_s=0.020,
            sample_period_s=0.02,
            min_segment_duration_s=2.0,
        )
        self.assertGreater(len(points), 2)
        self.assertEqual(trace_rows[-1]["segment"], "active_tcp_fixed_z_clearance")
        self.assertEqual(metrics["target_pose_base"]["frame"], "base_to_active_tcp")
        self.assertAlmostEqual(metrics["target_pose_base"]["position_xyz_m"][2], 0.039423891, places=12)
        self.assertAlmostEqual(metrics["position_entry_speed_m_s"], 0.020, places=12)
        self.assertEqual(metrics["position_entry_speed_source"], "historical_step5a_tp_movel_v_0.020_a_0.030")
        self.assertGreater(metrics["max_commanded_fk_speed_m_s"], float(config["legacy_v3_command_clamp_m_s"]))
        self.assertLessEqual(metrics["max_commanded_fk_speed_m_s"], 0.020 + 1e-12)

    def test_historical_visual_achieved_speed_hard_gate(self) -> None:
        config = load_no_contact_config(HISTORICAL_FIXED_Z_CONFIG)

        self.assertFalse(historical_visual_achieved_speed_gate_ok(0.0150001, config))
        self.assertTrue(historical_visual_achieved_speed_gate_ok(0.015, config))
        self.assertTrue(historical_visual_achieved_speed_gate_ok(0.014999, config))
        self.assertFalse(historical_visual_achieved_speed_gate_ok(float("nan"), config))

    def test_historical_visual_summary_ok_requires_achieved_speed_hard_gate(self) -> None:
        config = load_no_contact_config(HISTORICAL_FIXED_Z_CONFIG)

        def make_summary(max_achieved_speed_m_s: float) -> dict:
            node = Step5aHistoricalFixedZMotion.__new__(Step5aHistoricalFixedZMotion)
            node.args = SimpleNamespace(
                execute=True,
                mode="path",
                robot_ip="192.168.1.18",
                action_name="/scaled_joint_trajectory_controller/follow_joint_trajectory",
                sample_period_s=0.02,
                trace=Path("/tmp/offline_step5a_historical_trace.csv"),
                position_tolerance_m=0.003,
                cartesian_position_error_limit_m=0.005,
                trace_max_sample_gap_s=0.08,
                kunwei_max_force_delta_n=2.0,
                joint_history_max_samples=50000,
                calibration_yaml=Path("/home/andy/ur10e_ros2_ws/src/ur10e_bringup/config/ur10e_calibration.yaml"),
                xacro_path=Path("/opt/ros/humble/share/ur_description/urdf/ur.urdf.xacro"),
            )
            node.config = config
            node.model_bundle = SimpleNamespace(calibration_hash=EXPECTED_CALIBRATION_HASH)
            node.joint_history = []
            node.sent_goal = True
            node.accepted = True
            node.result_status = 4
            node.result_error_code = 0
            node.result_error_string = "Goal successfully reached!"
            node.failure_stage = "trajectory_execution"
            node.trajectory_authority_entered = True
            trace_rows = [
                {"achieved_speed_m_s": 0.0 if index == 0 else max_achieved_speed_m_s, "cartesian_error_m": 0.0003}
                for index in range(751)
            ]
            metrics = {
                "duration_s": 15.0,
                "max_reference_speed_m_s": 0.012,
                "max_commanded_fk_speed_m_s": 0.012,
                "max_commanded_position_error_m": 0.0003,
                "cartesian_reference_frame": {
                    "mode": "step5_safe_frame_local_xy_to_base_xy_offset",
                    "safe_frame_path": "/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/config/step5_safe_frame.json",
                    "local_x_axis": "u_along_xy",
                    "local_y_axis": "p_lateral_xy",
                    "u_along_xy": [-0.010785642631908187, 0.9999418332648238],
                    "p_lateral_xy": [-0.9999418332648239, -0.010785642631908406],
                    "origin_xy_m": [0.487795411149049, 0.12932679270060748],
                },
                "target_pose_base": {"frame": "base_to_active_tcp"},
                "target_tool0_pose_base": {"frame": "base_to_tool0"},
                "start_pose_base": {"frame": "base_to_tool0"},
                "start_active_tcp_pose_base": {"frame": "base_to_active_tcp"},
                "reference_local_final_offset_xy_m": [0.09419123247298389, 0.0005974457002445104],
                "reference_final_base_xyz_m": [0.0, 0.0, 0.039423891],
                "commanded_net_displacement_xyz_m": [0.0, 0.094, 0.0],
                "commanded_net_displacement_norm_m": 0.094,
            }
            return Step5aHistoricalFixedZMotion._summary(
                node,
                {},
                {
                    "ok": True,
                    "stream_start_command_sent": True,
                    "stream_stop_command_sent": True,
                    "failure_reason": None,
                },
                [0.0] * 6,
                trace_rows,
                metrics,
                {"trace_alignment_ok": True},
                [],
            )

        passing = make_summary(0.015)
        self.assertTrue(passing["ok"])
        self.assertTrue(passing["achieved_speed_gate_ok"])
        self.assertEqual(passing["achieved_speed_hard_cap_m_s"], 0.015)
        self.assertEqual(
            passing["speed_gate_status"],
            "achieved_speed_hard_gate_reference_and_commanded_fk_advisory",
        )

        failing = make_summary(0.0150001)
        self.assertFalse(failing["ok"])
        self.assertFalse(failing["achieved_speed_gate_ok"])
        self.assertEqual(failing["max_achieved_speed_m_s"], 0.0150001)

    def test_current_failed_evidence_run_audits_as_gate_a_failed(self) -> None:
        run_dir = WORKSPACE / "experiments" / "tase-contact-reproduction" / "runs" / "no_contact_test_20260617_143540"
        if not run_dir.exists():
            self.skipTest(f"local evidence run is unavailable: {run_dir}")
        audit = audit_gate_a_run(run_dir)
        self.assertFalse(audit["gate_a_pass"])
        self.assertEqual(audit["judgement"], "Step5a live motion executed, but Gate A failed")
        self.assertIn("speed_cap", audit["acceptance"]["failed_conditions"])
        self.assertIn("cartesian_equivalence", audit["acceptance"]["failed_conditions"])
        self.assertEqual(audit["trace_diagnostics"]["achieved_speed_over_cap_rows"], 21)
        self.assertEqual(audit["trace_diagnostics"]["cartesian_error_over_5mm_rows"], 571)


if __name__ == "__main__":
    unittest.main()
