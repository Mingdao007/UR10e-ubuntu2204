#!/usr/bin/env python3
"""Focused v27 ablation tests."""

from __future__ import annotations

import json
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
import step5c_strict_rnn as strict_rnn  # noqa: E402
import step5d_runtime_interface as iface  # noqa: E402
import upload_ur_tp_package as upload  # noqa: E402
from step5d_paper_outer_loop import Step5dOuterLoopState  # noqa: E402


V27 = "step5d_strict_rnn_ablation_v27"
V28 = "step5d_strict_rnn_ablation_v28"
V29 = "step5d_strict_rnn_ablation_v29"
V30 = "step5d_strict_rnn_ablation_v30"
P0_V8 = "step5d_strict_rnn_no_contact_p0_v8"


def raw_bridge_args(profile: str = V29, **overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "bridge_profile": profile,
        "step5d_stage25_control_mode": "speedj_rnn_live",
        "step5d_rnn_backend": "cupy",
        "step5d_rnn_inner_iterations": 1024,
        "step5d_epsilon": 0.01,
        "step5d_sigr_exponent_r": 0.8,
        "step5d_qdot_limit_rad_s": 0.05,
        "skip_dashboard_preflight": False,
        "disable_dashboard_program_watch": False,
        "robot_host": "192.0.2.10",
        "max_normal_force_n": 50.0,
        "max_force_norm_n": 60.0,
        "max_torque_norm_nm": 3.0,
        "sensor_stale_s": 0.10,
        "dashboard_program_watch_timeout_s": 45.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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

        def warm_start(self, **_kwargs: object) -> None:
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


def fake_v27_runtime_with_solver_failure(state: bridge.BridgeState, _args: object) -> None:
    fake_v27_runtime(state, _args)

    class FailingSolver:
        def reset_state(self) -> None:
            return None

        def warm_start(self, **_kwargs: object) -> None:
            return None

        def solve(self, **_kwargs: object) -> SimpleNamespace:
            raise RuntimeError("synthetic shadow solver failure")

    state.step5d_solver = FailingSolver()


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
    def test_long_check_cache_rejects_nonempty_gate_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "long-check.json"
            fingerprint = {"boot_id": "test"}
            gate = {
                "ok": True,
                "issues": ["stale_failure"],
                "robot_host": "192.168.1.18",
                "same_subnet": True,
                "device": "enp3s0",
                "kunwei": {"route_ok": True, "tcp_connect": {"ok": True}},
            }
            cache.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "checked_at_epoch": time.time(),
                        "robot_host": "192.168.1.18",
                        "gate": gate,
                        "fingerprint": fingerprint,
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(iface, "current_long_check_fingerprint", return_value=fingerprint):
                status = iface.long_check_cache_status(cache)
            self.assertFalse(status["ok"])
            self.assertEqual(status["state"], "MISS")

    def test_bridge_parse_args_recognizes_v27_step5b_envelope_with_speedl_shadow(self) -> None:
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
        self.assertEqual(args.max_normal_force_n, 50.0)
        self.assertEqual(args.max_force_norm_n, 60.0)
        self.assertEqual(args.max_torque_norm_nm, 3.0)

    def test_bridge_parse_args_recognizes_v29_contact_strict_rnn_defaults(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--skip-dashboard-preflight",
                "--bridge-mode",
                "line",
                "--bridge-profile",
                V29,
                "--bridge-path-shape",
                "cycloid",
            ]
        )

        self.assertEqual(args.bridge_profile, V29)
        self.assertEqual(args.step5d_stage25_control_mode, "speedj_rnn_live")
        self.assertEqual(args.step5d_preload_filtered_min_n, 5.0)
        self.assertEqual(args.step5d_preload_filtered_max_n, 22.0)
        self.assertEqual(args.step5d_preload_raw_min_n, 3.0)
        self.assertEqual(args.step5d_preload_raw_max_n, 25.0)
        self.assertEqual(args.step5d_preload_force_norm_max_n, 35.0)
        self.assertEqual(args.max_normal_force_n, 50.0)
        self.assertEqual(args.max_force_norm_n, 60.0)
        self.assertEqual(args.max_torque_norm_nm, 3.0)
        self.assertEqual(args.step5d_rnn_backend, "cupy")
        self.assertEqual(args.step5d_rnn_inner_iterations, 1024)
        self.assertEqual(args.step5d_sigr_exponent_r, 0.8)
        self.assertEqual(args.step5d_qdot_limit_rad_s, 0.05)

    def test_bridge_parse_args_binds_v30_and_p0_v8_to_512(self) -> None:
        for profile in (V30, P0_V8):
            with self.subTest(profile=profile):
                args = bridge.parse_args(
                    [
                        "--no-start-command",
                        "--skip-dashboard-preflight",
                        "--bridge-mode",
                        "line",
                        "--bridge-profile",
                        profile,
                        "--bridge-path-shape",
                        "cycloid",
                    ]
                )
                self.assertEqual(args.step5d_rnn_backend, "cupy")
                self.assertEqual(args.step5d_rnn_inner_iterations, 512)
                self.assertEqual(args.step5d_epsilon, 0.010)
                self.assertEqual(args.step5d_sigr_exponent_r, 0.8)
                self.assertEqual(args.step5d_qdot_limit_rad_s, 0.05)

    def test_v29_raw_bridge_rejects_disabled_runtime_guard_values(self) -> None:
        bridge.require_v29_runtime_guard_policy(raw_bridge_args())
        bad_values = [
            {"max_normal_force_n": math.inf},
            {"max_force_norm_n": 60.1},
            {"max_torque_norm_nm": 3.1},
            {"sensor_stale_s": 0.101},
            {"dashboard_program_watch_timeout_s": math.inf},
        ]
        for overrides in bad_values:
            with self.subTest(overrides=overrides):
                with self.assertRaises(SystemExit):
                    bridge.require_v29_runtime_guard_policy(raw_bridge_args(**overrides))

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

    def test_v28_package_is_v27_source_boundary_with_60s_stage25_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local_dir = Path(tmp) / "v28"
            liveprep.write_outputs(
                "2026-07-06T0600HKT_STEP5D_STRICT_RNN_ABLATION_V28",
                "2026-07-06T06:00:00+08:00",
                output_dir=local_dir,
                local_only=True,
                program=V28,
            )
            files = {ext: local_dir / f"{V28}{ext}" for ext in upload.EXTENSIONS}
            result = upload.validate_package(
                files,
                V28,
                liveprep.CONTROLLER_DIR,
                require_exact_cached_script=True,
            )
            script_text = files[".script"].read_text(encoding="utf-8")
            txt_text = files[".txt"].read_text(encoding="utf-8")

        self.assertEqual(result["program"], V28)
        self.assertIn("local line_success_progress_m = 60.000000000", script_text)
        self.assertIn("local line_runtime_limit_s = 65.000", script_text)
        self.assertIn("Step5d ablation Stage25.0 multi-layout speedl/speedj full-run for 60 s", script_text)
        self.assertIn("# STAGE25_V28_SCAFFOLD: v27_step5b_speedl_live_shadow_boundary_60s_full_run", script_text)
        self.assertIn("v28 extends the successful v27 Step5b speedl live / Step5d shadow boundary to 60s", txt_text)
        self.assertIn("speedl_cartesian_oracle", script_text + txt_text)
        self.assertNotIn("step5d_strict_rnn_ablation_v27", script_text + txt_text)

    def test_v29_package_is_contact_strict_rnn_live_candidate_without_p0_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local_dir = Path(tmp) / "v29"
            liveprep.write_outputs(
                "2026-07-08T0700HKT_STEP5D_STRICT_RNN_ABLATION_V29",
                "2026-07-08T07:00:00+08:00",
                output_dir=local_dir,
                local_only=True,
                program=V29,
            )
            files = {ext: local_dir / f"{V29}{ext}" for ext in upload.EXTENSIONS}
            result = upload.validate_package(
                files,
                V29,
                liveprep.CONTROLLER_DIR,
                require_exact_cached_script=True,
            )
            script_text = files[".script"].read_text(encoding="utf-8")
            txt_text = files[".txt"].read_text(encoding="utf-8")

        self.assertEqual(result["program"], V29)
        self.assertEqual(liveprep.spec_for(V29).default_stage25_control_mode, "speedj_rnn_live")
        self.assertIn("local line_success_progress_m = 60.000000000", script_text)
        self.assertIn("local line_runtime_limit_s = 65.000", script_text)
        self.assertIn("strict RNN live candidate", script_text + txt_text)
        self.assertIn("# STAGE25_V29_SCAFFOLD: v28_envelope_strict_rnn_live_candidate_60s", script_text)
        self.assertNotIn(
            "# STAGE25_V29_SCAFFOLD: v29_minimal_fix_frame_aware_normal_contract_60s",
            script_text,
        )
        self.assertIn("Stage25.0 default bridge mode: STEP5D_STAGE25_CONTROL_MODE=speedj_rnn_live", txt_text)
        self.assertIn("speedl_cartesian_oracle remains an explicit fallback/debug mode for v29", txt_text)
        self.assertIn("speedj_rnn_live on layout 524", txt_text)
        self.assertIn("--target-force-n 12.0", txt_text)
        self.assertNotIn("NO_CONTACT_P0_CAPTURE", script_text + txt_text)
        self.assertNotIn("step5d_strict_rnn_no_contact_p0", script_text + txt_text)
        self.assertNotIn("step5d_strict_rnn_ablation_v28", script_text + txt_text)

    def test_upload_validator_has_v29_allowlist_and_speedj_default(self) -> None:
        source = (ROOT / "tools" / "upload_ur_tp_package.py").read_text(encoding="utf-8")

        self.assertIn('"step5d_strict_rnn_ablation_v29"', source)
        self.assertIn('expected_default_mode = "speedj_rnn_live" if version_label == "v29"', source)
        self.assertIn('version_label in {"v28", "v29"}', source)
        self.assertIn('version_label not in {"v27", "v28", "v29"}', source)

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
        runtime = iface.resolve_runtime_interface(program=V27, root=ROOT, env={})
        self.assertEqual(runtime.hard_contract["stage25_speedl_orientation_policy"], "shadow_only_full_stage25")
        self.assertIn("wx/wy/wz forced to 0", runtime.register_contract["stage25_0"])

    def test_v28_runtime_interface_records_live_step5b_orientation_follow(self) -> None:
        self.assertEqual(iface.STEP5D_ABLATION_V28_STAGE_ID, V28)
        self.assertEqual(liveprep.spec_for(V28).program_name, V28)
        runtime = iface.resolve_runtime_interface(program=V28, root=ROOT, env={})
        self.assertEqual(
            runtime.hard_contract["stage25_speedl_orientation_policy"],
            "step5b_orientation_follow_live_step5d_shadow",
        )
        self.assertIn("Step5b/step4e orientation follow wx/wy/wz", runtime.register_contract["stage25_0"])
        self.assertNotIn("wx/wy/wz forced to 0", runtime.register_contract["stage25_0"])

    def test_v29_runtime_interface_records_strict_rnn_live_source(self) -> None:
        self.assertEqual(iface.STEP5D_ABLATION_V29_STAGE_ID, V29)
        self.assertEqual(liveprep.spec_for(V29).program_name, V29)
        runtime = iface.resolve_runtime_interface(program=V29, root=ROOT, env={})
        self.assertEqual(runtime.stage25_control_mode, "speedj_rnn_live")
        self.assertEqual(runtime.hard_contract["stage25_live_control_source"], "strict_rnn_live_speedj")
        self.assertIn("strict RNN live", runtime.register_contract["stage25_0"])
        self.assertIn("layout 524", runtime.register_contract["stage25_0"])

    def test_v29_is_in_tcp_cage_contact_safety_profile(self) -> None:
        self.assertIn(V29, bridge.STEP5D_TCP_CAGE_PROFILES)

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
            bridge.STEP5D_ABLATION_V28_STAGE_ID,
            bridge.STEP5D_ABLATION_V29_STAGE_ID,
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
            patch.object(bridge, "compute_step5d_outer_loop", side_effect=fake_v27_outer_with_matching_orientation),
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

    def test_v27_speedl_entry_runs_step5b_orientation_follow_live_with_shadow_orientation_error(self) -> None:
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
        live_linear = (
            values["step4e_cmd_vx_m_s"],
            values["step4e_cmd_vy_m_s"],
            values["step4e_cmd_vz_m_s"],
        )
        self.assertFalse(np.allclose(live_linear, (0.0010, 0.0015, -0.0020), atol=1e-12))
        live_angular = (
            values["step4e_cmd_wx_rad_s"],
            values["step4e_cmd_wy_rad_s"],
            values["step4e_cmd_wz_rad_s"],
        )
        # v29 policy: live angular is the Step5b/step4e orientation follow
        # (inside its limit), never the raw Step5d shadow angular command.
        self.assertLessEqual(math.sqrt(sum(value**2 for value in live_angular)), 0.015 + 1e-9)
        self.assertEqual(values["_step5d_live_orientation_enabled"], 1.0)
        self.assertFalse(
            np.allclose(
                live_angular,
                (
                    values["_step5d_speedl_shadow_raw_wx_rad_s"],
                    values["_step5d_speedl_shadow_raw_wy_rad_s"],
                    values["_step5d_speedl_shadow_raw_wz_rad_s"],
                ),
                atol=1e-12,
            )
        )
        self.assertAlmostEqual(values["step4e_orientation_error_rad"], tilt_rad, delta=0.002)
        self.assertAlmostEqual(values["_step5d_outer_orientation_error_rad"], tilt_rad, delta=0.002)
        self.assertEqual(values["_step5d_stage25_control_mode"], "speedl_cartesian_oracle")
        self.assertEqual(values["_step5d_live_control_source"], "step5b_speedl_live_step5d_shadow")
        self.assertAlmostEqual(values["_step5d_speedl_shadow_raw_vx_m_s"], 0.0010, places=9)
        self.assertAlmostEqual(values["_step5d_speedl_shadow_raw_vy_m_s"], 0.0015, places=9)
        self.assertAlmostEqual(values["_step5d_speedl_shadow_raw_vz_m_s"], -0.0020, places=9)

    def test_v27_filtered_live_entry_relatches_stale_normal_to_live_candidate(self) -> None:
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
                "--bridge-normal-follow-mode",
                "filtered_live",
            ]
        )
        tilt_rad = 0.105
        stale_latch_b = (math.sin(tilt_rad), 0.0, math.cos(tilt_rad))
        latest_output = {
            "actual_TCP_pose": [0.49, 0.14, 0.02, 3.14, 0.0, 0.0],
            "actual_TCP_speed": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "actual_q": [0.0] * 6,
            "actual_qd": [0.0] * 6,
            "output_double_register_35": 25.0,
        }

        state = acquired_v27_state()
        state.latched_normal_b = stale_latch_b
        state.filtered_normal_b = stale_latch_b

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

        self.assertTrue(state.step5d_stage25_normal_relatched)
        self.assertAlmostEqual(values["_step5d_stage25_entry_relatch_angle_rad"], tilt_rad, delta=0.002)
        # After the entry re-latch the orientation reference is the measured
        # reaction direction, so the stale-latch tilt no longer appears as an
        # orientation error for the (shadow) outer loop to chase.
        self.assertLess(values["step4e_orientation_error_rad"], 0.01)
        self.assertLess(values["_step5d_outer_orientation_error_rad"], 0.01)
        relatched = state.latched_normal_b
        self.assertAlmostEqual(bridge.angle_between_unit(relatched, (0.0, 0.0, 1.0)), 0.0, delta=0.002)

    def test_v27_locked_follow_mode_does_not_relatch(self) -> None:
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
        stale_latch_b = (math.sin(tilt_rad), 0.0, math.cos(tilt_rad))
        latest_output = {
            "actual_TCP_pose": [0.49, 0.14, 0.02, 3.14, 0.0, 0.0],
            "actual_TCP_speed": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "actual_q": [0.0] * 6,
            "actual_qd": [0.0] * 6,
            "output_double_register_35": 25.0,
        }

        state = acquired_v27_state()
        state.latched_normal_b = stale_latch_b
        state.filtered_normal_b = stale_latch_b

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

        self.assertFalse(state.step5d_stage25_normal_relatched)
        self.assertTrue(math.isnan(values["_step5d_stage25_entry_relatch_angle_rad"]))
        self.assertEqual(state.latched_normal_b, stale_latch_b)
        self.assertAlmostEqual(values["step4e_orientation_error_rad"], tilt_rad, delta=0.002)

    def test_v27_speedl_keeps_paper_angular_shadow_only_after_entry_window(self) -> None:
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
        state.last_robot_stage = 25.0
        state.line_stage_s = 0.200
        state.step5d_active_stage25_s = 0.200

        with (
            patch.object(
                bridge,
                "step5_contact_path_reference",
                return_value={
                    "progress": 0.2,
                    "desired_xy": (0.49, 0.14),
                    "path_error_xy": (0.0, 0.0),
                    "desired_velocity_xy": (0.0, 0.0),
                    "path_time_s": 0.2,
                },
            ),
            patch.object(bridge, "step5d_tcp_jacobian_base", return_value=np.eye(6)),
            patch.object(bridge, "step5d_omega_bounds", return_value=(np.full(6, -0.05), np.full(6, 0.05))),
            patch.object(bridge, "compute_step5d_outer_loop", side_effect=fake_v27_outer),
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
        live_linear = (
            values["step4e_cmd_vx_m_s"],
            values["step4e_cmd_vy_m_s"],
            values["step4e_cmd_vz_m_s"],
        )
        self.assertFalse(np.allclose(live_linear, (0.0010, 0.0015, -0.0020), atol=1e-12))
        live_angular = (
            values["step4e_cmd_wx_rad_s"],
            values["step4e_cmd_wy_rad_s"],
            values["step4e_cmd_wz_rad_s"],
        )
        # v29 policy: live angular is the Step5b/step4e orientation follow
        # (inside its limit), never the raw Step5d shadow angular command.
        self.assertLessEqual(math.sqrt(sum(value**2 for value in live_angular)), 0.015 + 1e-9)
        self.assertEqual(values["_step5d_live_orientation_enabled"], 1.0)
        self.assertFalse(
            np.allclose(
                live_angular,
                (
                    values["_step5d_speedl_shadow_raw_wx_rad_s"],
                    values["_step5d_speedl_shadow_raw_wy_rad_s"],
                    values["_step5d_speedl_shadow_raw_wz_rad_s"],
                ),
                atol=1e-12,
            )
        )
        self.assertEqual(values["_step5d_stage25_control_mode"], "speedl_cartesian_oracle")
        self.assertEqual(values["_step5d_speedl_orientation_shadow_only"], 1.0)
        raw_angular_norm = math.sqrt(
            values["_step5d_speedl_shadow_raw_wx_rad_s"] ** 2
            + values["_step5d_speedl_shadow_raw_wy_rad_s"] ** 2
            + values["_step5d_speedl_shadow_raw_wz_rad_s"] ** 2
        )
        self.assertAlmostEqual(raw_angular_norm, 0.015, places=9)
        self.assertLess(values["_step5d_speedl_shadow_raw_wy_rad_s"], 0.0)
        self.assertGreater(values["_step5d_speedl_shadow_raw_wz_rad_s"], 0.0)
        self.assertAlmostEqual(values["_step5d_speedl_shadow_raw_vx_m_s"], 0.0010, places=9)
        self.assertAlmostEqual(values["_step5d_speedl_shadow_raw_vy_m_s"], 0.0015, places=9)
        self.assertAlmostEqual(values["_step5d_speedl_shadow_raw_vz_m_s"], -0.0020, places=9)
        self.assertEqual(values["_step5d_live_control_source"], "step5b_speedl_live_step5d_shadow")
        self.assertIn("stage25_step5b_speedl_live_step5d_shadow", values["_step5d_intervention_reason"])

    def test_v28_speedl_source_boundary_matches_v27_shadow_policy(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--skip-dashboard-preflight",
                "--bridge-mode",
                "line",
                "--bridge-profile",
                V28,
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
        state.last_robot_stage = 25.0
        state.line_stage_s = 0.200
        state.step5d_active_stage25_s = 0.200

        with (
            patch.object(
                bridge,
                "step5_contact_path_reference",
                return_value={
                    "progress": 0.2,
                    "desired_xy": (0.49, 0.14),
                    "path_error_xy": (0.0, 0.0),
                    "desired_velocity_xy": (0.0, 0.0),
                    "path_time_s": 0.2,
                },
            ),
            patch.object(bridge, "step5d_tcp_jacobian_base", return_value=np.eye(6)),
            patch.object(bridge, "step5d_omega_bounds", return_value=(np.full(6, -0.05), np.full(6, 0.05))),
            patch.object(bridge, "compute_step5d_outer_loop", side_effect=fake_v27_outer),
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

        live_linear = (
            values["step4e_cmd_vx_m_s"],
            values["step4e_cmd_vy_m_s"],
            values["step4e_cmd_vz_m_s"],
        )
        self.assertFalse(np.allclose(live_linear, (0.0010, 0.0015, -0.0020), atol=1e-12))
        live_angular = (
            values["step4e_cmd_wx_rad_s"],
            values["step4e_cmd_wy_rad_s"],
            values["step4e_cmd_wz_rad_s"],
        )
        # v29 policy: live angular is the Step5b/step4e orientation follow
        # (inside its limit), never the raw Step5d shadow angular command.
        self.assertLessEqual(math.sqrt(sum(value**2 for value in live_angular)), 0.015 + 1e-9)
        self.assertEqual(values["_step5d_live_orientation_enabled"], 1.0)
        self.assertFalse(
            np.allclose(
                live_angular,
                (
                    values["_step5d_speedl_shadow_raw_wx_rad_s"],
                    values["_step5d_speedl_shadow_raw_wy_rad_s"],
                    values["_step5d_speedl_shadow_raw_wz_rad_s"],
                ),
                atol=1e-12,
            )
        )
        self.assertEqual(values["_step5d_stage25_control_mode"], "speedl_cartesian_oracle")
        self.assertEqual(values["_step5d_live_control_source"], "step5b_speedl_live_step5d_shadow")
        self.assertEqual(values["_step5d_speedl_orientation_shadow_only"], 1.0)
        self.assertAlmostEqual(values["_step5d_speedl_shadow_raw_vx_m_s"], 0.0010, places=9)
        self.assertAlmostEqual(values["_step5d_speedl_shadow_raw_vy_m_s"], 0.0015, places=9)
        self.assertAlmostEqual(values["_step5d_speedl_shadow_raw_vz_m_s"], -0.0020, places=9)

    def test_speedj_rnn_resume_compute_path_warm_starts_before_solve(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--skip-dashboard-preflight",
                "--bridge-mode",
                "line",
                "--bridge-profile",
                V28,
                "--bridge-path-shape",
                "cycloid",
                "--step5d-stage25-control-mode",
                "speedj_rnn_live",
            ]
        )
        latest_output = {
            "actual_TCP_pose": [0.49, 0.14, 0.02, 3.14, 0.0, 0.0],
            "actual_TCP_speed": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "actual_q": [0.0] * 6,
            "actual_qd": [0.0] * 6,
            "output_double_register_35": 25.0,
        }
        calls: list[str] = []
        captured: dict[str, object] = {}

        class RecordingSolver:
            def reset_state(self) -> None:
                calls.append("reset")

            def warm_start(self, **kwargs: object) -> None:
                calls.append("warm_start")
                captured["warm_start"] = kwargs

            def solve(self, **kwargs: object) -> SimpleNamespace:
                calls.append("solve")
                captured["solve"] = kwargs
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

        state = acquired_v27_state()
        fake_v27_runtime(state, args)
        state.step5d_solver = RecordingSolver()
        state.step5d_solver_lifecycle_key = "stage25_hold_zero_qdot:soft_low_contact_hold"
        state.step5d_pending_solver_warm_start = False

        with (
            patch.object(
                bridge,
                "step5d_tcp_jacobian_base",
                return_value=np.diag([2.0, 1.5, 1.0, 1.0, 0.5, 0.25]),
            ),
            patch.object(bridge, "step5d_omega_bounds", return_value=(np.full(6, -0.05), np.full(6, 0.05))),
            patch.object(bridge, "compute_step5d_outer_loop", side_effect=fake_v27_outer),
            patch.object(
                bridge,
                "rnn_target_state_from_outer_loop",
                side_effect=lambda output, **kwargs: {"xdot_c": np.asarray(output.xdot_c, dtype=float), **kwargs},
            ),
        ):
            values = bridge.compute_bridge_values(
                args,
                [0.0, 0.0, -12.0, 0.0, 0.0, 0.0],
                latest_output,
                1.0,
                state,
                0.002,
            )

        self.assertEqual(calls, ["reset", "warm_start", "solve"])
        self.assertFalse(state.step5d_pending_solver_warm_start)
        self.assertEqual(state.step5d_solver_lifecycle_key, "stage25_pass_solver")
        self.assertEqual(values["_step5d_stage25_control_mode"], "speedj_rnn_live")
        self.assertIn("solver_warm_start", values["_step5d_intervention_reason"])
        self.assertEqual(values["step4e_cmd_valid"], 1.0)
        self.assertAlmostEqual(values["_step5d_rnn_raw_qd0_rad_s"], 0.020, places=9)
        self.assertAlmostEqual(values["_step5d_rnn_raw_qd1_rad_s"], 0.010, places=9)
        self.assertAlmostEqual(values["_step5d_rnn_raw_qd3_rad_s"], 0.004, places=9)
        warm_kwargs = captured["warm_start"]
        solve_target = captured["solve"]["target_state"]  # type: ignore[index]
        np.testing.assert_allclose(warm_kwargs["J"], solve_target["J"])  # type: ignore[index]
        np.testing.assert_allclose(warm_kwargs["xdot_c"], solve_target["xdot_c"])  # type: ignore[index]
        np.testing.assert_allclose(warm_kwargs["omega_minus"], solve_target["omega_minus"])  # type: ignore[index]
        np.testing.assert_allclose(warm_kwargs["omega_plus"], solve_target["omega_plus"])  # type: ignore[index]

    def test_v29_speedj_rnn_live_exports_contact_acceptance_diagnostics(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--skip-dashboard-preflight",
                "--bridge-mode",
                "line",
                "--bridge-profile",
                V29,
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
        fake_v27_runtime(state, args)
        state.step5d_solver_lifecycle_key = "stage25_hold_zero_qdot:soft_low_contact_hold"
        state.step5d_pending_solver_warm_start = False

        class AcceptedSolver:
            def reset_state(self) -> None:
                return None

            def warm_start(self, **_kwargs: object) -> None:
                return None

            def solve(self, **_kwargs: object) -> SimpleNamespace:
                return SimpleNamespace(
                    qdot=(0.0, 0.0, -0.0001, 0.0, 0.0, 0.0),
                    solver_status=40.0,
                    residual_norm=0.0002,
                    diagnostics={
                        "lambda_state": np.array([3.0, 4.0, 0.0, 0.0, 0.0, 0.0]),
                        "active_bounds_mask": [False, False, False, False, False, False],
                        "proj_input_form": "J.T @ lambda_state",
                        "lambda_update_form": "lambda_state -= (dt / epsilon) * (J @ theta_dot_state - xdot_c)",
                        "inner_iterations": 1024,
                        "backend": "cupy",
                        "solve_wall_ms": 0.25,
                        "epsilon": 0.010,
                        "sigr_exponent_r": 0.8,
                    },
                )

        state.step5d_solver = AcceptedSolver()

        def fake_v29_contact_outer(_config: object, _state: object, _inputs: object) -> SimpleNamespace:
            return SimpleNamespace(
                xdot_c=np.array([0.0, 0.0, -0.0001, 0.0, 0.0, 0.0]),
                next_state=Step5dOuterLoopState(),
                diagnostics={
                    "outer_orientation_angle_rad": 0.001592653589793113,
                    "e_f": 0.0,
                    "R_d_z_dot_R_cur_z": -0.999998855,
                    "force_sign_convention": "step5_step6_positive_normal_load",
                },
            )

        with (
            patch.object(bridge, "step5d_tcp_jacobian_base", return_value=np.eye(6)),
            patch.object(bridge, "step5d_omega_bounds", return_value=(np.full(6, -0.05), np.full(6, 0.05))),
            patch.object(bridge, "compute_step5d_outer_loop", side_effect=fake_v29_contact_outer),
            patch.object(
                bridge,
                "rnn_target_state_from_outer_loop",
                side_effect=lambda output, **kwargs: {"xdot_c": np.asarray(output.xdot_c, dtype=float), **kwargs},
            ),
        ):
            values = bridge.compute_bridge_values(
                args,
                [0.0, 0.0, -12.0, 0.0, 0.0, 0.0],
                latest_output,
                1.0,
                state,
                0.002,
            )

        self.assertEqual(args.step5d_stage25_control_mode, "speedj_rnn_live")
        self.assertEqual(values["_step5d_stage25_control_mode"], "speedj_rnn_live")
        self.assertEqual(values["step4e_cmd_valid"], 1.0)
        self.assertEqual(values["step4e_controller_state"], bridge.STEP5D_STAGE25_JOINT_LAYOUT_CODE)
        self.assertEqual(values["_step5d_rnn_accepted"], 1.0)
        self.assertEqual(values["_step5d_rnn_reject_reason"], "ok")
        self.assertEqual(values["_step5d_safe_hold_active"], 0.0)
        self.assertEqual(values["_step5d_cmd_valid_reason"], "rnn_accepted")
        self.assertIn("solver_warm_start", values["_step5d_intervention_reason"])
        self.assertTrue(np.isfinite(values["_step5d_lambda_norm"]))
        self.assertTrue(np.isfinite(values["_step5d_constraint_residual_norm"]))

    def test_v29_rnn_evidence_failure_keeps_cmd_valid_with_zero_qdot_safe_hold(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--skip-dashboard-preflight",
                "--bridge-mode",
                "line",
                "--bridge-profile",
                V29,
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

        class ResidualRejectSolver:
            def reset_state(self) -> None:
                return None

            def warm_start(self, **_kwargs: object) -> None:
                return None

            def solve(self, **_kwargs: object) -> SimpleNamespace:
                return SimpleNamespace(
                    qdot=(0.0, 0.0, -0.0001, 0.0, 0.0, 0.0),
                    solver_status=40.0,
                    residual_norm=0.050,
                    diagnostics={
                        "lambda_state": np.array([3.0, 4.0, 0.0, 0.0, 0.0, 0.0]),
                        "active_bounds_mask": [False, False, False, False, False, False],
                        "proj_input_form": "J.T @ lambda_state",
                        "lambda_update_form": "lambda_state -= (dt / epsilon) * (J @ theta_dot_state - xdot_c)",
                        "inner_iterations": 1024,
                        "backend": "cupy",
                        "solve_wall_ms": 0.25,
                        "epsilon": 0.010,
                        "sigr_exponent_r": 0.8,
                    },
                )

        state = acquired_v27_state()
        fake_v27_runtime(state, args)
        state.step5d_solver = ResidualRejectSolver()

        def fake_v29_contact_outer(_config: object, _state: object, _inputs: object) -> SimpleNamespace:
            return SimpleNamespace(
                xdot_c=np.array([0.0, 0.0, -0.0001, 0.0, 0.0, 0.0]),
                next_state=Step5dOuterLoopState(),
                diagnostics={
                    "outer_orientation_angle_rad": 0.001592653589793113,
                    "e_f": 0.0,
                    "R_d_z_dot_R_cur_z": -0.999998855,
                    "force_sign_convention": "step5_step6_positive_normal_load",
                },
            )

        with (
            patch.object(bridge, "step5d_tcp_jacobian_base", return_value=np.eye(6)),
            patch.object(bridge, "step5d_omega_bounds", return_value=(np.full(6, -0.05), np.full(6, 0.05))),
            patch.object(bridge, "compute_step5d_outer_loop", side_effect=fake_v29_contact_outer),
            patch.object(
                bridge,
                "rnn_target_state_from_outer_loop",
                side_effect=lambda output, **kwargs: {"xdot_c": np.asarray(output.xdot_c, dtype=float), **kwargs},
            ),
        ):
            values = bridge.compute_bridge_values(
                args,
                [0.0, 0.0, -12.0, 0.0, 0.0, 0.0],
                latest_output,
                1.0,
                state,
                0.002,
            )

        self.assertEqual(values["step4e_cmd_valid"], 1.0)
        self.assertEqual(values["step4e_controller_state"], bridge.STEP5D_STAGE25_JOINT_LAYOUT_CODE)
        self.assertEqual((values["step4e_cmd_vx_m_s"], values["step4e_cmd_vy_m_s"], values["step4e_cmd_vz_m_s"]), (0.0, 0.0, 0.0))
        self.assertEqual((values["step4e_cmd_wx_rad_s"], values["step4e_cmd_wy_rad_s"], values["step4e_cmd_wz_rad_s"]), (0.0, 0.0, 0.0))
        self.assertEqual(values["_step5d_rnn_accepted"], 0.0)
        self.assertEqual(values["_step5d_rnn_reject_reason"], "constraint_residual_norm_exceeds_v29_limit")
        self.assertEqual(values["_step5d_safe_hold_active"], 1.0)
        self.assertEqual(values["_step5d_cmd_valid_reason"], "rnn_evidence_rejected_safe_hold")
        self.assertAlmostEqual(values["_step5d_constraint_residual_norm"], 0.050)
        self.assertTrue(np.isfinite(values["_step5d_lambda_norm"]))

    def test_speedj_rnn_warm_start_failure_keeps_pending_fail_closed(self) -> None:
        class FailingWarmStartSolver:
            def warm_start(self, **_kwargs: object) -> None:
                raise np.linalg.LinAlgError("synthetic singular warm start")

        state = acquired_v27_state()
        state.step5d_solver = FailingWarmStartSolver()
        state.step5d_solver_lifecycle_key = "stage25_pass_solver"
        state.step5d_pending_solver_warm_start = True

        with self.assertRaisesRegex(RuntimeError, "Step5d solver warm_start failed"):
            bridge.apply_step5d_solver_warm_start_if_pending(
                state,
                jacobian=np.eye(6),
                xdot_c=np.ones(6) * 0.001,
                omega_minus=np.full(6, -0.05),
                omega_plus=np.full(6, 0.05),
            )

        self.assertTrue(state.step5d_pending_solver_warm_start)

        bridge.reset_step5d_solver_state_for_boundary(state, "stage25_pass_solver")

        self.assertTrue(state.step5d_pending_solver_warm_start)

    def test_speedl_shadow_path_does_not_consume_pending_rnn_warm_start(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--skip-dashboard-preflight",
                "--bridge-mode",
                "line",
                "--bridge-profile",
                V28,
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
        calls: list[str] = []

        class RecordingSolver:
            def reset_state(self) -> None:
                calls.append("reset")

            def warm_start(self, **_kwargs: object) -> None:
                calls.append("warm_start")

            def solve(self, **_kwargs: object) -> SimpleNamespace:
                calls.append("solve")
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

        state = acquired_v27_state()
        fake_v27_runtime(state, args)
        state.step5d_solver = RecordingSolver()
        state.step5d_solver_lifecycle_key = "stage25_hold_zero_qdot:soft_low_contact_hold"
        state.step5d_pending_solver_warm_start = False

        with (
            patch.object(bridge, "step5d_tcp_jacobian_base", return_value=np.eye(6)),
            patch.object(bridge, "step5d_omega_bounds", return_value=(np.full(6, -0.05), np.full(6, 0.05))),
            patch.object(bridge, "compute_step5d_outer_loop", side_effect=fake_v27_outer),
            patch.object(bridge, "rnn_target_state_from_outer_loop", return_value={"shadow": True}),
        ):
            values = bridge.compute_bridge_values(
                args,
                [0.0, 0.0, -12.0, 0.0, 0.0, 0.0],
                latest_output,
                1.0,
                state,
                0.002,
            )

        self.assertEqual(calls, ["reset", "solve"])
        self.assertTrue(state.step5d_pending_solver_warm_start)
        self.assertNotIn("solver_warm_start", values["_step5d_intervention_reason"])

    def test_speedj_dls_path_does_not_consume_pending_rnn_warm_start(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--skip-dashboard-preflight",
                "--bridge-mode",
                "line",
                "--bridge-profile",
                V28,
                "--bridge-path-shape",
                "cycloid",
                "--step5d-stage25-control-mode",
                "speedj_dls_oracle",
            ]
        )
        latest_output = {
            "actual_TCP_pose": [0.49, 0.14, 0.02, 3.14, 0.0, 0.0],
            "actual_TCP_speed": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "actual_q": [0.0] * 6,
            "actual_qd": [0.0] * 6,
            "output_double_register_35": 25.0,
        }
        calls: list[str] = []

        class RecordingSolver:
            def reset_state(self) -> None:
                calls.append("reset")

            def warm_start(self, **_kwargs: object) -> None:
                calls.append("warm_start")

            def solve(self, **_kwargs: object) -> SimpleNamespace:
                calls.append("solve")
                return SimpleNamespace(
                    qdot=(0.0,) * 6,
                    solver_status=40.0,
                    residual_norm=0.0,
                    diagnostics={
                        "lambda_state": np.zeros(6),
                        "active_bounds_mask": [False] * 6,
                        "proj_input_form": "J.T @ lambda_state",
                        "lambda_update_form": "lambda_state -= (dt / epsilon) * (J @ theta_dot_state - xdot_c)",
                    },
                )

        state = acquired_v27_state()
        fake_v27_runtime(state, args)
        state.step5d_solver = RecordingSolver()
        state.step5d_solver_lifecycle_key = "stage25_hold_zero_qdot:soft_low_contact_hold"
        state.step5d_pending_solver_warm_start = False

        with (
            patch.object(bridge, "step5d_tcp_jacobian_base", return_value=np.eye(6)),
            patch.object(bridge, "step5d_omega_bounds", return_value=(np.full(6, -0.05), np.full(6, 0.05))),
            patch.object(bridge, "compute_step5d_outer_loop", side_effect=fake_v27_outer),
            patch.object(bridge, "rnn_target_state_from_outer_loop", return_value={"shadow": True}),
        ):
            values = bridge.compute_bridge_values(
                args,
                [0.0, 0.0, -12.0, 0.0, 0.0, 0.0],
                latest_output,
                1.0,
                state,
                0.002,
            )

        self.assertEqual(calls, ["reset", "solve"])
        self.assertTrue(state.step5d_pending_solver_warm_start)
        self.assertNotIn("solver_warm_start", values["_step5d_intervention_reason"])
        self.assertEqual(values["_step5d_stage25_control_mode"], "speedj_dls_oracle")

    def test_v27_speedl_shadow_solver_failure_keeps_live_source_diagnostics(self) -> None:
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
        with (
            patch.object(bridge, "step5d_tcp_jacobian_base", return_value=np.eye(6)),
            patch.object(bridge, "step5d_omega_bounds", return_value=(np.full(6, -0.05), np.full(6, 0.05))),
            patch.object(bridge, "compute_step5d_outer_loop", side_effect=fake_v27_outer),
            patch.object(bridge, "rnn_target_state_from_outer_loop", return_value={"shadow": True}),
        ):
            fake_v27_runtime_with_solver_failure(state, args)
            values = bridge.compute_bridge_values(
                args,
                [0.0, 0.0, -12.0, 0.0, 0.0, 0.0],
                latest_output,
                1.0,
                state,
                0.002,
            )

        self.assertIn("synthetic shadow solver failure", values["_step5d_solver_error"])
        self.assertEqual(values["_step5d_live_control_source"], "step5b_speedl_live_step5d_shadow")
        self.assertEqual(values["_step5d_speedl_orientation_shadow_only"], 1.0)
        self.assertAlmostEqual(values["_step5d_speedl_shadow_raw_vx_m_s"], 0.0010, places=9)
        self.assertAlmostEqual(values["_step5d_speedl_shadow_raw_vy_m_s"], 0.0015, places=9)
        self.assertAlmostEqual(values["_step5d_speedl_shadow_raw_vz_m_s"], -0.0020, places=9)
        raw_angular_norm = math.sqrt(
            values["_step5d_speedl_shadow_raw_wx_rad_s"] ** 2
            + values["_step5d_speedl_shadow_raw_wy_rad_s"] ** 2
            + values["_step5d_speedl_shadow_raw_wz_rad_s"] ** 2
        )
        self.assertAlmostEqual(raw_angular_norm, 0.015, places=9)
        self.assertLess(values["_step5d_speedl_shadow_raw_wy_rad_s"], 0.0)
        self.assertGreater(values["_step5d_speedl_shadow_raw_wz_rad_s"], 0.0)
        live_angular = (
            values["step4e_cmd_wx_rad_s"],
            values["step4e_cmd_wy_rad_s"],
            values["step4e_cmd_wz_rad_s"],
        )
        # v29 policy: live angular is the Step5b/step4e orientation follow
        # (inside its limit), never the raw Step5d shadow angular command.
        self.assertLessEqual(math.sqrt(sum(value**2 for value in live_angular)), 0.015 + 1e-9)
        self.assertEqual(values["_step5d_live_orientation_enabled"], 1.0)
        self.assertFalse(
            np.allclose(
                live_angular,
                (
                    values["_step5d_speedl_shadow_raw_wx_rad_s"],
                    values["_step5d_speedl_shadow_raw_wy_rad_s"],
                    values["_step5d_speedl_shadow_raw_wz_rad_s"],
                ),
                atol=1e-12,
            )
        )


class Step5dSolverWarmStartLifecycleTest(unittest.TestCase):
    def make_state_with_fake_solver(self) -> tuple[bridge.BridgeState, list[str]]:
        state = bridge.BridgeState()
        calls: list[str] = []
        state.step5d_solver = SimpleNamespace(
            reset_state=lambda: calls.append("reset"),
            warm_start=lambda **kwargs: calls.append("warm_start"),
        )
        return state, calls

    def test_lifecycle_boundary_reset_flags_pending_warm_start(self) -> None:
        state, calls = self.make_state_with_fake_solver()
        self.assertFalse(state.step5d_pending_solver_warm_start)

        bridge.reset_step5d_solver_state_for_boundary(state, "stage25_pass_solver")
        self.assertEqual(calls, ["reset"])
        self.assertTrue(state.step5d_pending_solver_warm_start)

        # Same boundary key is a no-op and must not re-arm the warm start.
        state.step5d_pending_solver_warm_start = False
        bridge.reset_step5d_solver_state_for_boundary(state, "stage25_pass_solver")
        self.assertEqual(calls, ["reset"])
        self.assertFalse(state.step5d_pending_solver_warm_start)

        bridge.reset_step5d_solver_state_for_boundary(
            state, "stage25_hold_zero_qdot:soft_low_contact_hold"
        )
        self.assertEqual(calls, ["reset", "reset"])
        self.assertTrue(state.step5d_pending_solver_warm_start)

    def test_apply_warm_start_consumes_pending_flag_once(self) -> None:
        state, calls = self.make_state_with_fake_solver()
        bridge.reset_step5d_solver_state_for_boundary(state, "stage25_pass_solver")

        applied = bridge.apply_step5d_solver_warm_start_if_pending(
            state,
            jacobian=np.eye(6),
            xdot_c=np.zeros(6),
            omega_minus=np.full(6, -0.05),
            omega_plus=np.full(6, 0.05),
        )
        self.assertTrue(applied)
        self.assertEqual(calls, ["reset", "warm_start"])
        self.assertFalse(state.step5d_pending_solver_warm_start)

        applied_again = bridge.apply_step5d_solver_warm_start_if_pending(
            state,
            jacobian=np.eye(6),
            xdot_c=np.zeros(6),
            omega_minus=np.full(6, -0.05),
            omega_plus=np.full(6, 0.05),
        )
        self.assertFalse(applied_again)
        self.assertEqual(calls, ["reset", "warm_start"])

    def test_warm_start_places_real_solver_at_entry_command_fixed_point(self) -> None:
        state = bridge.BridgeState()
        truth = tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False)
        json.dump({"strict_rnn_enabled": True, "pending_pdf_verify": [], "sections": {}}, truth)
        truth.close()
        self.addCleanup(lambda: Path(truth.name).unlink(missing_ok=True))
        state.step5d_solver = strict_rnn.StrictTaseRnnSolver(
            strict_rnn.StrictRnnConfig(paper_truth_path=Path(truth.name))
        )
        bridge.reset_step5d_solver_state_for_boundary(state, "stage25_pass_solver")

        jacobian = np.eye(6)
        jacobian[0, 4] = -0.8
        xdot_c = np.array([1e-4, 0.0, 0.0, 0.0, 6e-3, 0.0])
        applied = bridge.apply_step5d_solver_warm_start_if_pending(
            state,
            jacobian=jacobian,
            xdot_c=xdot_c,
            omega_minus=np.full(6, -0.05),
            omega_plus=np.full(6, 0.05),
        )
        self.assertTrue(applied)
        np.testing.assert_allclose(jacobian @ state.step5d_solver.theta_dot_state, xdot_c, atol=1e-6)


class Step5dV29RawBridgeAuthorizationGateTest(unittest.TestCase):
    def test_periodic_deadline_skips_missed_slots_without_burst_catchup(self) -> None:
        deadline, missed, lateness = bridge.advance_periodic_deadline(10.0, 10.0245, 0.002)
        self.assertEqual(missed, 12)
        self.assertAlmostEqual(lateness, 0.0245)
        self.assertGreater(deadline, 10.0245)
        self.assertAlmostEqual(deadline, 10.026)

    def test_raw_v29_bridge_revalidates_exact_live_authorization_before_side_effects(self) -> None:
        args = SimpleNamespace(
            bridge_profile=V29,
            step5d_stage25_control_mode="speedj_rnn_live",
            step5d_rnn_backend="cupy",
            step5d_rnn_inner_iterations=1024,
            step5d_epsilon=0.01,
            step5d_sigr_exponent_r=0.8,
            step5d_qdot_limit_rad_s=0.05,
            skip_dashboard_preflight=False,
        )
        with patch.object(bridge, "verify_step5d_live_bridge_authorization", return_value={"ok": True}) as verify:
            result = bridge.require_v29_live_bridge_authorization(args)

        self.assertEqual(result, {"ok": True})
        verify.assert_called_once_with(
            bridge.EXPERIMENT_ROOT,
            V29,
            "speedj_rnn_live",
            rnn_backend="cupy",
            rnn_inner_iterations=1024,
            epsilon=0.01,
            sigr_exponent_r=0.8,
            qdot_cap_rad_s=0.05,
        )
        main_source = Path(bridge.__file__).read_text(encoding="utf-8")
        call = "require_v29_live_bridge_authorization(args)"
        self.assertIn(call, main_source)
        self.assertLess(main_source.index(call), main_source.index("args.output_dir.mkdir"))

    def test_raw_v29_pending_audit_override_keeps_exact_profile_and_binding(self) -> None:
        args = raw_bridge_args(V29)
        with (
            patch.dict(
                os.environ,
                {
                    "STEP5D_ALLOW_PENDING_OFFLINE_AUDIT": "1",
                    "STEP5D_CONFIRM": bridge.STEP5D_V29_LIVE_CONFIRMATION,
                },
            ),
            patch.object(bridge, "verify_step5d_binding", return_value={"ok": True}) as verify_binding,
            patch.object(bridge, "verify_step5d_live_bridge_authorization") as verify_live,
        ):
            result = bridge.require_v29_live_bridge_authorization(args)
        self.assertEqual(result, {"ok": True})
        verify_binding.assert_called_once_with(bridge.EXPERIMENT_ROOT, V29)
        verify_live.assert_not_called()

        for field, bad_value in (
            ("step5d_rnn_backend", "numpy"),
            ("step5d_rnn_inner_iterations", 512),
            ("step5d_epsilon", 0.02),
            ("step5d_sigr_exponent_r", 1.0),
            ("step5d_qdot_limit_rad_s", 0.1),
        ):
            bad_args = raw_bridge_args(V29, **{field: bad_value})
            with patch.dict(
                os.environ,
                {
                    "STEP5D_ALLOW_PENDING_OFFLINE_AUDIT": "1",
                    "STEP5D_CONFIRM": bridge.STEP5D_V29_LIVE_CONFIRMATION,
                },
            ):
                with self.assertRaisesRegex(SystemExit, "exact cupy/1024"):
                    bridge.require_v29_live_bridge_authorization(bad_args)

    def test_raw_v29_pending_audit_override_requires_exact_confirmation(self) -> None:
        args = raw_bridge_args(V29)
        for token in (None, "", "wrong token"):
            env = {"STEP5D_ALLOW_PENDING_OFFLINE_AUDIT": "1"}
            if token is not None:
                env["STEP5D_CONFIRM"] = token
            with (
                self.subTest(token=token),
                patch.dict(os.environ, env, clear=True),
                patch.object(bridge, "verify_step5d_binding") as verify_binding,
                patch.object(
                    bridge,
                    "verify_step5d_live_bridge_authorization",
                    return_value={"ok": True},
                ) as verify_live,
            ):
                self.assertEqual(bridge.require_v29_live_bridge_authorization(args), {"ok": True})
            verify_binding.assert_not_called()
            verify_live.assert_called_once()

    def test_pending_audit_override_never_applies_to_non_v29(self) -> None:
        args = raw_bridge_args(V28)
        with patch.dict(
            os.environ,
            {
                "STEP5D_ALLOW_PENDING_OFFLINE_AUDIT": "1",
                "STEP5D_CONFIRM": bridge.STEP5D_V29_LIVE_CONFIRMATION,
            },
        ):
            self.assertFalse(bridge.v29_pending_audit_override_authorized(args))

    def test_raw_v29_cannot_disable_dashboard_runtime_watch(self) -> None:
        args = raw_bridge_args(V29, disable_dashboard_program_watch=True)
        with patch.object(bridge, "verify_step5d_live_bridge_authorization") as verify:
            with self.assertRaisesRegex(SystemExit, "runtime watchdog"):
                bridge.require_v29_live_bridge_authorization(args)
        verify.assert_not_called()

    def test_v29_effective_scheduler_must_be_fifo_priority_20(self) -> None:
        args = raw_bridge_args(V29)
        with (
            patch.object(os, "sched_getscheduler", return_value=os.SCHED_FIFO),
            patch.object(os, "sched_getparam", return_value=os.sched_param(20)),
        ):
            self.assertIsNone(bridge.require_v29_realtime_scheduler(args))
        for scheduler, priority in ((os.SCHED_OTHER, 20), (os.SCHED_FIFO, 19)):
            with (
                self.subTest(scheduler=scheduler, priority=priority),
                patch.object(os, "sched_getscheduler", return_value=scheduler),
                patch.object(os, "sched_getparam", return_value=os.sched_param(priority)),
                self.assertRaisesRegex(SystemExit, "SCHED_FIFO priority exactly 20"),
            ):
                bridge.require_v29_realtime_scheduler(args)

    def test_v30_and_p0_v8_share_production_fifo_priority_20(self) -> None:
        with (
            patch.object(os, "sched_getscheduler", return_value=os.SCHED_FIFO),
            patch.object(os, "sched_getparam", return_value=os.sched_param(20)),
        ):
            for profile in (V30, P0_V8):
                with self.subTest(profile=profile):
                    self.assertIsNone(
                        bridge.require_step5d_realtime_scheduler(
                            raw_bridge_args(profile)
                        )
                    )

    def test_runtime_scheduler_metadata_records_effective_policy_and_priority(self) -> None:
        with (
            patch.object(os, "sched_getscheduler", return_value=os.SCHED_FIFO),
            patch.object(os, "sched_getparam", return_value=os.sched_param(20)),
        ):
            self.assertEqual(
                bridge.runtime_scheduler_metadata(),
                {"policy": "SCHED_FIFO", "policy_value": os.SCHED_FIFO, "priority": 20},
            )

    def test_raw_v29_bridge_rejects_dls_mode_before_authorization(self) -> None:
        args = SimpleNamespace(
            bridge_profile=V29,
            step5d_stage25_control_mode="speedj_dls_oracle",
            step5d_rnn_backend="cupy",
            step5d_rnn_inner_iterations=1024,
            step5d_epsilon=0.01,
            step5d_sigr_exponent_r=0.8,
            step5d_qdot_limit_rad_s=0.05,
            skip_dashboard_preflight=False,
        )
        with patch.object(bridge, "verify_step5d_live_bridge_authorization") as verify:
            with self.assertRaisesRegex(SystemExit, "speedj_rnn_live"):
                bridge.require_v29_live_bridge_authorization(args)
        verify.assert_not_called()

    def test_raw_profile_relabel_cannot_bypass_current_v29_binding(self) -> None:
        args = raw_bridge_args(
            V28,
            step5d_stage25_control_mode="speedj_dls_oracle",
            step5d_rnn_backend="numpy",
            step5d_rnn_inner_iterations=1,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "current_stage.json").write_text(
                json.dumps({"program": V29, "current_stage_id": V29}), encoding="utf-8"
            )
            with patch.object(bridge, "verify_step5d_live_bridge_authorization") as verify:
                with self.assertRaisesRegex(SystemExit, "profile relabel"):
                    bridge.require_v29_live_bridge_authorization(args, root=root)
        verify.assert_not_called()

    def test_raw_bridge_fails_closed_when_current_stage_is_missing_or_malformed(self) -> None:
        args = raw_bridge_args(V28)
        payloads = [None, "{", "[]"]
        for payload in payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "config").mkdir()
                if payload is not None:
                    (root / "config" / "current_stage.json").write_text(payload, encoding="utf-8")
                with patch.object(bridge, "verify_step5d_live_bridge_authorization") as verify:
                    with self.assertRaisesRegex(SystemExit, "current-stage identity"):
                        bridge.require_v29_live_bridge_authorization(args, root=root)
                verify.assert_not_called()

    def test_raw_bridge_rejects_inconsistent_current_identifiers(self) -> None:
        args = raw_bridge_args(V28)
        identities = [(V28, V29), (V29, V28), (V28, ""), ("", V28)]
        for program, stage_id in identities:
            with self.subTest(program=program, stage_id=stage_id), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "config").mkdir()
                (root / "config" / "current_stage.json").write_text(
                    json.dumps({"program": program, "current_stage_id": stage_id}), encoding="utf-8"
                )
                with patch.object(bridge, "verify_step5d_live_bridge_authorization") as verify:
                    with self.assertRaisesRegex(SystemExit, "inconsistent"):
                        bridge.require_v29_live_bridge_authorization(args, root=root)
                verify.assert_not_called()

    def test_raw_v29_cannot_skip_dashboard_program_identity(self) -> None:
        args = SimpleNamespace(
            bridge_profile=V29,
            step5d_stage25_control_mode="speedj_rnn_live",
            step5d_rnn_backend="cupy",
            step5d_rnn_inner_iterations=1024,
            step5d_epsilon=0.01,
            step5d_sigr_exponent_r=0.8,
            step5d_qdot_limit_rad_s=0.05,
            skip_dashboard_preflight=True,
        )
        with patch.object(bridge, "verify_step5d_live_bridge_authorization") as verify:
            with self.assertRaisesRegex(SystemExit, "Dashboard"):
                bridge.require_v29_live_bridge_authorization(args)
        verify.assert_not_called()

    def test_v29_dashboard_preflight_binds_loaded_program_identity(self) -> None:
        args = SimpleNamespace(bridge_profile=V29)
        good = {
            "programState": f"STOPPED </programs/andyl/kunwei/step5/{V29}.urp>",
            "is in remote control": "Is in remote control: true",
            "safetymode": "Safetymode: NORMAL",
            "robotmode": "Robotmode: RUNNING",
        }
        self.assertIsNone(bridge.require_v29_dashboard_program_binding(args, good))
        with self.assertRaisesRegex(SystemExit, "program identity"):
            bridge.require_v29_dashboard_program_binding(
                args,
                {**good, "programState": f"STOPPED <{V28}.urp>"},
            )
        tp_local = {**good, "is in remote control": "Is in remote control: false"}
        with patch.dict(
            os.environ,
            {
                "STEP5D_ALLOW_PENDING_OFFLINE_AUDIT": "1",
                "STEP5D_CONFIRM": bridge.STEP5D_V29_LIVE_CONFIRMATION,
            },
        ):
            self.assertIsNone(bridge.require_v29_dashboard_program_binding(args, tp_local))

    def test_v29_tp_local_dashboard_exception_requires_exact_confirmation(self) -> None:
        args = raw_bridge_args(V29)
        tp_local = {
            "programState": f"STOPPED </programs/{V29}.urp>",
            "is in remote control": "Is in remote control: false",
            "safetymode": "Safetymode: NORMAL",
            "robotmode": "Robotmode: RUNNING",
        }
        with patch.dict(
            os.environ,
            {"STEP5D_ALLOW_PENDING_OFFLINE_AUDIT": "1", "STEP5D_CONFIRM": "wrong"},
            clear=True,
        ):
            with self.assertRaisesRegex(SystemExit, "remote-control"):
                bridge.require_v29_dashboard_program_binding(args, tp_local)

    def test_v29_dashboard_preflight_rejects_fuzzy_or_ambiguous_states(self) -> None:
        args = SimpleNamespace(bridge_profile=V29)
        good = {
            "programState": f"STOPPED </programs/{V29}.urp>",
            "is in remote control": "Is in remote control: true",
            "safetymode": "Safetymode: NORMAL",
            "robotmode": "Robotmode: RUNNING",
        }
        cases = {
            "prefixed_basename": {"programState": f"STOPPED <prefix_{V29}.urp>"},
            "backup_suffix": {"programState": f"STOPPED <{V29}.urp.bak>"},
            "ambiguous": {"programState": f"STOPPED <{V29}.urp> <{V28}.urp>"},
            "false_remote": {"is in remote control": "Is in remote control: NOT TRUE"},
            "false_safety": {"safetymode": "Safetymode: NOT_NORMAL"},
            "false_robot": {"robotmode": "Robotmode: NOT_RUNNING"},
            "missing_program": {"programState": ""},
        }
        for name, mutation in cases.items():
            with self.subTest(name=name), self.assertRaises(SystemExit):
                bridge.require_v29_dashboard_program_binding(args, {**good, **mutation})

    def test_v29_dashboard_binding_runs_before_runtime_connections(self) -> None:
        source = Path(bridge.__file__).read_text(encoding="utf-8")
        authorization_call = source.index("require_v29_live_bridge_authorization(args)")
        scheduler_call = source.index("require_step5d_realtime_scheduler(args)")
        output_dir_create = source.index("args.output_dir.mkdir")
        binding_call = source.index("require_v29_dashboard_program_binding(args, dashboard)")
        sensor_connect = source.index("socket.create_connection((args.sensor_ip, args.sensor_port)")
        initial_rtde = source.index("open_rtde_bridge(args)", sensor_connect)
        self.assertLess(authorization_call, scheduler_call)
        self.assertLess(scheduler_call, output_dir_create)
        self.assertLess(binding_call, sensor_connect)
        self.assertLess(binding_call, initial_rtde)

    def test_v29_fail_stop_clears_motion_carriers_and_selects_sensor_fault(self) -> None:
        values = {name: 0.03 for name in bridge.BRIDGE_INPUT_NAMES}
        values.update({"sensor_ok": 1.0, "stop_request": 1.0})
        bridge.apply_v29_fail_stop(values)
        self.assertEqual(values["sensor_ok"], 0.0)
        self.assertEqual(values["stop_request"], 0.0)
        self.assertEqual(values["step4e_cmd_valid"], 0.0)
        self.assertTrue(all(values[name] == 0.0 for name in bridge.BRIDGE_INPUT_NAMES[:6]))

    def test_v29_fail_stop_prioritizes_hard_guard_and_preserves_first_reason(self) -> None:
        selected = bridge.select_v29_fail_stop_reason(
            None,
            hard_guard_reason="force_norm_guard",
            step4e_stop_request=True,
            step4e_guard_reason="step5d_contact_safety:watchdog",
        )
        self.assertEqual(selected, "force_norm_guard")
        self.assertEqual(
            bridge.select_v29_fail_stop_reason(
                selected,
                hard_guard_reason="torque_norm_guard",
                step4e_stop_request=True,
                step4e_guard_reason="step5d_contact_safety:nonfinite",
            ),
            "force_norm_guard",
        )

    def test_v29_fail_stop_dashboard_uses_only_hardcoded_stop(self) -> None:
        args = raw_bridge_args()
        with patch.object(bridge, "dashboard_exchange", return_value={"stop": "Stopped"}) as exchange:
            result = bridge.request_v29_fail_stop_dashboard_stop(args)
        self.assertTrue(result["delivered"])
        exchange.assert_called_once_with(
            args.robot_host,
            ["stop"],
            timeout=bridge.STEP5D_V29_FAIL_STOP_DASHBOARD_TIMEOUT_S,
        )
        self.assertLess(bridge.STEP5D_V29_FAIL_STOP_DASHBOARD_TIMEOUT_S, 0.1)

    def test_v29_fail_stop_dashboard_rejects_malformed_failed_or_exception_responses(self) -> None:
        args = raw_bridge_args()
        cases = [None, {}, {"stop": "Failed to execute: stop"}, OSError("offline"), RuntimeError("offline")]
        for response in cases:
            with self.subTest(response=type(response).__name__):
                effect = response if isinstance(response, BaseException) else None
                returned = None if isinstance(response, BaseException) else response
                with patch.object(bridge, "dashboard_exchange", return_value=returned, side_effect=effect):
                    result = bridge.request_v29_fail_stop_dashboard_stop(args)
                self.assertFalse(result["delivered"])

    @staticmethod
    def _v29_fail_stop_state() -> dict[str, object]:
        return {
            "rtde_reason3_packets_sent": 0,
            "first_rtde_packet_output_sequence": None,
            "heartbeat_min_sent": None,
            "heartbeat_max_sent": None,
            "tp_reason3_observed": False,
            "dashboard_stop_delivered": False,
        }

    def test_v29_reason3_ack_requires_post_packet_fresh_bound_echo(self) -> None:
        state = self._v29_fail_stop_state()
        stale = {
            "output_double_register_26": 42.0,
            "output_double_register_27": 0.0,
            "output_double_register_28": 0.0,
            "output_double_register_30": 3.0,
        }
        bridge.update_v29_fail_stop_tp_ack(state, output=stale, output_sequence=10)
        self.assertFalse(state["tp_reason3_observed"])
        bridge.record_v29_fail_stop_rtde_packet(state, heartbeat=9001.0, output_sequence=10)
        bridge.update_v29_fail_stop_tp_ack(state, output=stale, output_sequence=10)
        self.assertFalse(state["tp_reason3_observed"])
        bridge.update_v29_fail_stop_tp_ack(state, output=stale, output_sequence=11)
        self.assertFalse(state["tp_reason3_observed"])
        fresh = {**stale, "output_double_register_26": 9001.0}
        bridge.update_v29_fail_stop_tp_ack(state, output=fresh, output_sequence=11)
        self.assertTrue(state["tp_reason3_observed"])
        self.assertEqual(bridge.v29_fail_stop_termination_channel(state), "tp_reason3_echo")

    def test_v29_fail_stop_requires_confirmed_channel_before_termination(self) -> None:
        state = self._v29_fail_stop_state()
        self.assertIsNone(bridge.v29_fail_stop_termination_channel(state))
        state["dashboard_stop_delivered"] = True
        self.assertEqual(bridge.v29_fail_stop_termination_channel(state), "dashboard_stop_ack")

    def test_v29_runtime_timeouts_stay_below_tp_stale_limit(self) -> None:
        self.assertLess(bridge.STEP5D_V29_RUNTIME_DASHBOARD_WATCH_TIMEOUT_S, 0.1)
        self.assertLess(bridge.STEP5D_V29_FAIL_STOP_DASHBOARD_TIMEOUT_S, 0.1)
        self.assertLess(bridge.STEP5D_V29_FAIL_STOP_RTDE_RECONNECT_TIMEOUT_S, 0.1)
        source = Path(bridge.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "if args.bridge_profile == STEP5D_ABLATION_V29_STAGE_ID\n                                else None",
            source,
        )

    def test_v29_tp_reason3_is_non_autohome(self) -> None:
        script = (ROOT / "programs" / "step5" / f"{V29}.script").read_text(encoding="utf-8")
        guard = script.split("def codex_step4e_guard_stop_reason():", 1)[1].split(
            "def codex_should_auto_home", 1
        )[0]
        self.assertLess(guard.index("if sensor_ok < 0.5:"), guard.index("elif stop_request > 0.5:"))
        auto_home = script.split("def codex_should_auto_home(stop_reason):", 1)[1].split(
            "def codex_echo_basic", 1
        )[0]
        self.assertNotIn("stop_reason == 3.0", auto_home)
        for reason in (4, 5, 6, 7):
            self.assertIn(f"stop_reason == {reason}.0", auto_home)

    def test_v29_runtime_watch_latches_fail_stop_on_program_drift(self) -> None:
        source = Path(bridge.__file__).read_text(encoding="utf-8")
        watch = source.split("if dashboard_watch_enabled and not fail_stop_latched", 1)[1].split(
            "sensor_recv_start", 1
        )[0]
        self.assertIn("v29_dashboard_program_identity_matches", watch)
        self.assertIn('v29_safety_fail_stop["latched_reason"] = "dashboard_program_identity_drift"', watch)
        self.assertIn("STEP5D_V29_RUNTIME_DASHBOARD_WATCH_TIMEOUT_S", watch)

    def test_non_v29_hard_guard_stop_request_behavior_is_retained(self) -> None:
        source = Path(bridge.__file__).read_text(encoding="utf-8")
        block = source.split('if v29_safety_fail_stop["enabled"]:', 1)[1].split(
            "rtde_connected = rtde is not None", 1
        )[0]
        historical = block.split("else:", 1)[1]
        self.assertIn('bridge_values["stop_request"] = 1.0', historical)
        self.assertIn("hard_guard_reason is not None", historical)

    def test_v29_fail_stop_state_is_persisted_in_summary(self) -> None:
        source = Path(bridge.__file__).read_text(encoding="utf-8")
        summary = source.split('summary = {\n        "finished_at"', 1)[1]
        self.assertIn('"v29_safety_fail_stop": {', summary)
        self.assertIn('if key != "next_dashboard_stop_attempt_mono"', summary)


if __name__ == "__main__":
    unittest.main()
