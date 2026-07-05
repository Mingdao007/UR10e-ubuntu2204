#!/usr/bin/env python3
"""Focused v27 ablation tests."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
import os
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_liveprep as liveprep  # noqa: E402
import kunwei_rtde_bridge as bridge  # noqa: E402
import step5d_runtime_interface as iface  # noqa: E402
import upload_ur_tp_package as upload  # noqa: E402
from step5d_paper_outer_loop import Step5dOuterLoopState  # noqa: E402


V27 = "step5d_strict_rnn_ablation_v27"


def write_tcp_cage_source(path: Path, *, x: float, y: float, z: float) -> None:
    path.write_text(
        "\n".join(
            [
                "ur_output_double_register_35,step4e_cmd_valid,ur_actual_TCP_pose_0,ur_actual_TCP_pose_1,ur_actual_TCP_pose_2",
                f"25.0,1,{x},{y},{z}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def acquired_v27_state() -> bridge.BridgeState:
    state = bridge.BridgeState()
    state.latched_normal_b = (0.0, 0.0, 1.0)
    state.filtered_normal_b = (0.0, 0.0, 1.0)
    state.latched_normal_locked = True
    state.normal_acquired = True
    state.step5d_tcp_cage = SimpleNamespace(
        evaluate=lambda *_args, **_kwargs: {
            "distance_m": 0.010,
            "braking_margin_m": 0.010,
            "signed_distance_m": 0.010,
            "cell_index": 1.0,
            "reason": "inside_test_cage",
        }
    )
    return state


def fake_v27_runtime(state: bridge.BridgeState, _args: object) -> None:
    state.step5d_model_bundle = SimpleNamespace(
        model=SimpleNamespace(
            lowerPositionLimit=np.full(6, -3.14),
            upperPositionLimit=np.full(6, 3.14),
        )
    )
    state.step5d_tcp_offset_tool0 = np.zeros(3)

    class FakeSolver:
        def reset_state(self) -> None:
            return None

        def solve(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                qdot=(0.020, 0.010, -0.010, 0.004, -0.003, 0.002),
                solver_status=40.0,
                residual_norm=0.012,
                diagnostics={
                    "lambda_state": np.array([3.0, 4.0, 0.0, 0.0, 0.0, 0.0]),
                    "active_bounds_mask": [True, False, False, False, False, False],
                    "proj_input_form": "J.T @ lambda_state",
                    "lambda_update_form": "lambda_state -= (dt / epsilon) * (J @ theta_dot_state - xdot_c)",
                },
            )

    state.step5d_solver = FakeSolver()


def fake_v27_outer(*_args: object, **_kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(
        xdot_c=np.array([0.0010, 0.0015, -0.0020, 0.0100, -0.0200, 0.0300]),
        next_state=Step5dOuterLoopState(),
        diagnostics={
            "outer_orientation_angle_rad": 0.0,
            "e_f": 0.0,
            "R_d_z_dot_R_cur_z": 1.0,
            "force_sign_convention": "step5_step6_positive_normal_load",
        },
    )


def fake_v27_outer_with_matching_orientation(_config: object, _state: object, inputs: object) -> SimpleNamespace:
    pose = getattr(inputs, "tcp_pose_base")
    reaction = getattr(inputs, "control_reaction_normal_base")
    rotation = bridge.rotvec_to_matrix(float(pose[3]), float(pose[4]), float(pose[5]))
    tcp_z_axis_b = (rotation[0][2], rotation[1][2], rotation[2][2])
    approach_axis_b = (-float(reaction[0]), -float(reaction[1]), -float(reaction[2]))
    orientation_axis = bridge.cross3(tcp_z_axis_b, approach_axis_b)
    orientation_error = math.atan2(
        bridge.norm3(orientation_axis),
        bridge.clamp(bridge.dot3(tcp_z_axis_b, approach_axis_b), -1.0, 1.0),
    )
    return SimpleNamespace(
        xdot_c=np.array([0.0010, 0.0015, -0.0020, 0.0100, -0.0200, 0.0300]),
        next_state=Step5dOuterLoopState(),
        diagnostics={
            "outer_orientation_angle_rad": orientation_error,
            "e_f": 0.0,
            "R_d_z_dot_R_cur_z": math.cos(orientation_error),
            "force_sign_convention": "step5_step6_positive_normal_load",
        },
    )


class Step5dV27AblationTest(unittest.TestCase):
    def test_bridge_parse_args_recognizes_v27_speedl_wide_tube(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--skip-dashboard-preflight",
                "--bridge-mode",
                "line",
                "--bridge-profile",
                V27,
                "--bridge-path-shape",
                "cycloid",
            ]
        )

        self.assertEqual(args.bridge_profile, V27)
        self.assertEqual(args.step5d_stage25_control_mode, "speedl_cartesian_oracle")
        self.assertEqual(args.step5d_preload_filtered_min_n, 5.0)
        self.assertEqual(args.step5d_preload_filtered_max_n, 22.0)
        self.assertEqual(args.step5d_preload_raw_min_n, 3.0)
        self.assertEqual(args.step5d_preload_raw_max_n, 25.0)
        self.assertEqual(args.step5d_preload_force_norm_max_n, 35.0)
        self.assertEqual(args.max_normal_force_n, 35.0)
        self.assertEqual(args.max_force_norm_n, 35.0)
        self.assertEqual(args.max_torque_norm_nm, 4.0)

    def test_v27_package_keeps_step5b_scaffold_min_delta_and_consumption_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local_dir = Path(tmp) / "v27"
            liveprep.write_outputs(
                "2026-07-06T0100HKT_STEP5D_STRICT_RNN_ABLATION_V27",
                "2026-07-06T01:00:00+08:00",
                output_dir=local_dir,
                local_only=True,
                program=V27,
            )
            files = {ext: local_dir / f"{V27}{ext}" for ext in upload.EXTENSIONS}
            result = upload.validate_package(
                files,
                V27,
                liveprep.CONTROLLER_DIR,
                require_exact_cached_script=True,
            )
            script_text = files[".script"].read_text(encoding="utf-8")
            txt_text = files[".txt"].read_text(encoding="utf-8")

        self.assertEqual(result["program"], V27)
        self.assertEqual(result["target_dir"], liveprep.CONTROLLER_DIR)
        self.assertIn("# STAGE25_V27_SCAFFOLD: step5b_v3_scaffold_min_delta", script_text)
        self.assertIn("STAGE25_CADENCE_CONSUMPTION", script_text)
        self.assertIn("local stage25_command_consumed = 0", script_text)
        self.assertIn("write_output_float_register(47, stage25_command_consumed)", script_text)
        self.assertIn("local line_entry_default_normal_load_min_n = 5.000", script_text)
        self.assertIn("local line_entry_default_normal_load_max_n = 22.000", script_text)
        self.assertIn("local line_entry_default_force_norm_max_n = 35.000", script_text)
        self.assertIn("# SAFETY: raw normal guard 35 N, force norm guard 35 N, torque guard 4.0 Nm.", script_text)
        self.assertIn("v27 starts from the proven Step5b v3 scaffold", txt_text)
        self.assertIn("Stage25.0 cadence/command-consumption instrumentation", txt_text)
        self.assertNotIn("step5d_strict_rnn_ablation_v26", script_text + txt_text)

    def test_bridge_csv_fields_include_v27_stage25_timing_and_consumption(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(encoding="utf-8")

        self.assertIn("STEP5D_ABLATION_V27_STAGE_ID", source)
        self.assertIn("step5d_liveprep_v27_profile = args.bridge_profile == STEP5D_ABLATION_V27_STAGE_ID", source)
        self.assertIn("_step5d_stage25_echo_layout_tag", source)
        self.assertIn("_step5d_stage25_echo_cmd_valid", source)
        self.assertIn("_step5d_stage25_echo_command_norm", source)
        self.assertIn("_step5d_stage25_echo_consumed", source)
        self.assertIn("_step5d_stage25_row_gap_s", source)
        self.assertIn("_bridge_loop_rtde_recv_s", source)
        self.assertIn("_bridge_loop_compute_s", source)
        self.assertIn("_bridge_loop_rtde_send_s", source)
        self.assertIn("_bridge_loop_csv_write_s", source)

    def test_v27_runtime_constant_matches_interface(self) -> None:
        self.assertEqual(iface.STEP5D_ABLATION_V27_STAGE_ID, V27)
        self.assertEqual(liveprep.spec_for(V27).program_name, V27)

    def test_v27_runtime_prewarm_builds_tcp_cage_before_stage25(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--skip-dashboard-preflight",
                "--bridge-mode",
                "line",
                "--bridge-profile",
                V27,
                "--bridge-path-shape",
                "cycloid",
            ]
        )
        state = bridge.BridgeState()
        cage = object()
        model_bundle = SimpleNamespace(
            model=SimpleNamespace(
                lowerPositionLimit=np.full(6, -3.14),
                upperPositionLimit=np.full(6, 3.14),
            )
        )

        with (
            patch.object(bridge.step5d_kin, "build_calibrated_model", return_value=model_bundle),
            patch.object(bridge.step5d_kin, "finite_run_rows", return_value=[]),
            patch.object(bridge.step5d_kin, "infer_tcp_offset", return_value={"mean": np.zeros(3)}),
            patch.object(bridge, "StrictTaseRnnSolver", return_value=SimpleNamespace(reset_state=lambda: None)),
            patch.object(bridge, "build_step5d_v15a_tcp_cage", return_value=cage) as build_cage,
        ):
            bridge.ensure_step5d_liveprep_runtime(state, args)

        self.assertIs(state.step5d_tcp_cage, cage)
        build_cage.assert_called_once_with()

    def test_tcp_cage_cache_hit_uses_fingerprint_without_reparsing_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.csv"
            cache = Path(tmp) / "cage.json"
            write_tcp_cage_source(source, x=1.0, y=2.0, z=3.0)

            cold = bridge.build_step5d_v15a_tcp_cage([source], cache_path=cache)
            with patch.object(bridge.csv, "DictReader", side_effect=AssertionError("cache miss")):
                cached = bridge.build_step5d_v15a_tcp_cage([source], cache_path=cache)

        self.assertEqual(cold.min_xyz, cached.min_xyz)
        self.assertEqual(cold.max_xyz, cached.max_xyz)
        self.assertEqual(cold.source_rows, cached.source_rows)

    def test_tcp_cage_cache_fingerprint_mismatch_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.csv"
            cache = Path(tmp) / "cage.json"
            write_tcp_cage_source(source, x=1.0, y=2.0, z=3.0)
            first = bridge.build_step5d_v15a_tcp_cage([source], cache_path=cache)

            write_tcp_cage_source(source, x=10.0, y=20.0, z=30.0)
            rebuilt = bridge.build_step5d_v15a_tcp_cage([source], cache_path=cache)

        self.assertNotEqual(first.min_xyz, rebuilt.min_xyz)
        self.assertAlmostEqual(rebuilt.min_xyz[0], 10.0 - bridge.STEP5D_V15A_TCP_CAGE_PADDING_M)

    def test_tcp_cage_cache_valid_json_non_object_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.csv"
            cache = Path(tmp) / "cage.json"
            write_tcp_cage_source(source, x=1.0, y=2.0, z=3.0)
            cache.write_text("[]\n", encoding="utf-8")

            cage = bridge.build_step5d_v15a_tcp_cage([source], cache_path=cache)

        self.assertEqual(cage.source_rows, 1)
        self.assertAlmostEqual(cage.min_xyz[0], 1.0 - bridge.STEP5D_V15A_TCP_CAGE_PADDING_M)

    def test_tcp_cage_cache_content_hash_mismatch_rebuilds_when_stat_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.csv"
            cache = Path(tmp) / "cage.json"
            write_tcp_cage_source(source, x=1.0, y=2.0, z=3.0)
            first = bridge.build_step5d_v15a_tcp_cage([source], cache_path=cache)
            stat = source.stat()

            write_tcp_cage_source(source, x=4.0, y=5.0, z=6.0)
            source.write_text(
                source.read_text(encoding="utf-8").ljust(stat.st_size),
                encoding="utf-8",
            )
            source.chmod(stat.st_mode)
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertEqual(source.stat().st_size, stat.st_size)
            self.assertEqual(source.stat().st_mtime_ns, stat.st_mtime_ns)

            rebuilt = bridge.build_step5d_v15a_tcp_cage([source], cache_path=cache, validate_content_hash=True)

        self.assertNotEqual(first.min_xyz, rebuilt.min_xyz)
        self.assertAlmostEqual(rebuilt.min_xyz[0], 4.0 - bridge.STEP5D_V15A_TCP_CAGE_PADDING_M)

    def test_tcp_cage_empty_custom_source_list_does_not_fall_back_to_default_sources(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no finite Stage25 source poses"):
            bridge.build_step5d_v15a_tcp_cage([])

    def test_default_tcp_cage_cache_artifact_loads_without_reparsing_source_csvs(self) -> None:
        self.assertTrue(bridge.STEP5D_V15A_TCP_CAGE_CACHE_PATH.is_file())
        with patch.object(bridge.csv, "DictReader", side_effect=AssertionError("default cache miss")):
            cage = bridge.build_step5d_v15a_tcp_cage()

        self.assertEqual(cage.source_rows, 119709)

    def test_tcp_cage_profiles_are_single_source_for_prewarm_and_online_gate(self) -> None:
        expected = {
            bridge.STEP5D_LIVEPREP_V15A_STAGE_ID,
            bridge.STEP5D_LIVEPREP_V16_STAGE_ID,
            bridge.STEP5D_LIVEPREP_V17_STAGE_ID,
            bridge.STEP5D_LIVEPREP_V18_STAGE_ID,
            bridge.STEP5D_LIVEPREP_V19_STAGE_ID,
            bridge.STEP5D_LIVEPREP_V20_STAGE_ID,
            bridge.STEP5D_LIVEPREP_V21_STAGE_ID,
            bridge.STEP5D_LIVEPREP_V22_STAGE_ID,
            bridge.STEP5D_LIVEPREP_V23_STAGE_ID,
            bridge.STEP5D_LIVEPREP_V24_STAGE_ID,
            bridge.STEP5D_ABLATION_V25_STAGE_ID,
            bridge.STEP5D_ABLATION_V26_STAGE_ID,
            bridge.STEP5D_ABLATION_V27_STAGE_ID,
        }
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(encoding="utf-8")

        self.assertEqual(bridge.STEP5D_TCP_CAGE_PROFILES, expected)
        self.assertIn("args.bridge_profile in STEP5D_TCP_CAGE_PROFILES", source)
        self.assertIn("step5d_liveprep_online_cage_profile = args.bridge_profile in STEP5D_TCP_CAGE_PROFILES", source)

    def test_v27_first_stage25_tick_stays_under_50ms_after_prewarm(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--skip-dashboard-preflight",
                "--bridge-mode",
                "line",
                "--bridge-profile",
                V27,
                "--bridge-path-shape",
                "cycloid",
            ]
        )
        latest_output = {
            "actual_TCP_pose": [0.49, 0.14, 0.02, 3.14, 0.0, 0.0],
            "actual_TCP_speed": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "actual_q": [0.0] * 6,
            "actual_qd": [0.0] * 6,
            "output_double_register_35": 25.0,
        }

        state = acquired_v27_state()
        state.step5d_tcp_cage = None
        model_bundle = SimpleNamespace(
            model=SimpleNamespace(
                lowerPositionLimit=np.full(6, -3.14),
                upperPositionLimit=np.full(6, 3.14),
            )
        )
        prewarmed_cage = SimpleNamespace(
            evaluate=lambda *_args, **_kwargs: {
                "distance_m": 0.010,
                "braking_margin_m": 0.010,
                "signed_distance_m": 0.010,
                "cell_index": 1.0,
                "reason": "inside_test_cage",
            }
        )
        with (
            patch.object(bridge.step5d_kin, "build_calibrated_model", return_value=model_bundle),
            patch.object(bridge.step5d_kin, "finite_run_rows", return_value=[]),
            patch.object(bridge.step5d_kin, "infer_tcp_offset", return_value={"mean": np.zeros(3)}),
            patch.object(bridge, "StrictTaseRnnSolver", return_value=SimpleNamespace(reset_state=lambda: None)),
            patch.object(bridge, "build_step5d_v15a_tcp_cage", return_value=prewarmed_cage) as build_cage,
        ):
            bridge.ensure_step5d_liveprep_runtime(state, args)
        self.assertIs(state.step5d_tcp_cage, prewarmed_cage)
        build_cage.assert_called_once_with()

        with (
            patch.object(bridge, "build_step5d_v15a_tcp_cage", side_effect=AssertionError("lazy cage build")),
            patch.object(bridge, "step5d_tcp_jacobian_base", return_value=np.eye(6)),
            patch.object(bridge, "step5d_omega_bounds", return_value=(np.full(6, -0.05), np.full(6, 0.05))),
            patch.object(bridge, "compute_step5d_outer_loop", side_effect=fake_v27_outer),
            patch.object(bridge, "rnn_target_state_from_outer_loop", return_value={"shadow": True}),
        ):
            fake_v27_runtime(state, args)
            state.step5d_tcp_cage = prewarmed_cage
            t0 = time.perf_counter()
            values = bridge.compute_bridge_values(
                args,
                [0.0, 0.0, -12.0, 0.0, 0.0, 0.0],
                latest_output,
                1.0,
                state,
                0.002,
            )
            elapsed_s = time.perf_counter() - t0

        self.assertLess(elapsed_s, 0.050)
        self.assertEqual(values["step4e_cmd_valid"], 1.0)
        self.assertEqual(values["_step5d_stage25_control_mode"], "speedl_cartesian_oracle")

    def test_v27_speedl_entry_freezes_angular_command_but_keeps_shadow_orientation_error(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--skip-dashboard-preflight",
                "--bridge-mode",
                "line",
                "--bridge-profile",
                V27,
                "--bridge-path-shape",
                "cycloid",
            ]
        )
        tilt_rad = 0.105
        reaction_normal_b = (math.sin(tilt_rad), 0.0, math.cos(tilt_rad))
        latest_output = {
            "actual_TCP_pose": [0.49, 0.14, 0.02, 3.14, 0.0, 0.0],
            "actual_TCP_speed": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "actual_q": [0.0] * 6,
            "actual_qd": [0.0] * 6,
            "output_double_register_35": 25.0,
        }

        state = acquired_v27_state()
        state.latched_normal_b = reaction_normal_b
        state.filtered_normal_b = reaction_normal_b

        with (
            patch.object(bridge, "step5d_tcp_jacobian_base", return_value=np.eye(6)),
            patch.object(bridge, "step5d_omega_bounds", return_value=(np.full(6, -0.05), np.full(6, 0.05))),
            patch.object(bridge, "compute_step5d_outer_loop", side_effect=fake_v27_outer_with_matching_orientation),
            patch.object(bridge, "rnn_target_state_from_outer_loop", return_value={"shadow": True}),
        ):
            fake_v27_runtime(state, args)
            values = bridge.compute_bridge_values(
                args,
                [0.0, 0.0, -12.0, 0.0, 0.0, 0.0],
                latest_output,
                1.0,
                state,
                0.002,
            )

        self.assertEqual(values["step4e_cmd_valid"], 1.0)
        self.assertAlmostEqual(values["step4e_cmd_vx_m_s"], 0.0010, places=9)
        self.assertAlmostEqual(values["step4e_cmd_vy_m_s"], 0.0015, places=9)
        self.assertAlmostEqual(values["step4e_cmd_vz_m_s"], -0.0020, places=9)
        self.assertAlmostEqual(values["step4e_cmd_wx_rad_s"], 0.0, places=9)
        self.assertAlmostEqual(values["step4e_cmd_wy_rad_s"], 0.0, places=9)
        self.assertAlmostEqual(values["step4e_cmd_wz_rad_s"], 0.0, places=9)
        self.assertAlmostEqual(values["step4e_orientation_error_rad"], tilt_rad, delta=0.002)
        self.assertAlmostEqual(values["_step5d_outer_orientation_error_rad"], tilt_rad, delta=0.002)
        self.assertEqual(values["_step5d_stage25_control_mode"], "speedl_cartesian_oracle")


if __name__ == "__main__":
    unittest.main()
