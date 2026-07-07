#!/usr/bin/env python3
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_step5d_no_contact_p0 as p0  # noqa: E402
import build_step5d_liveprep as liveprep  # noqa: E402
import kunwei_rtde_bridge as bridge  # noqa: E402
import step5_table  # noqa: E402
import step5d_runtime_interface as iface  # noqa: E402
from step5d_paper_outer_loop import Step5dOuterLoopState  # noqa: E402


P0_FIELDS = [
    "t_monotonic_s",
    "ur_output_double_register_35",
    "_step4e_normal_load_n",
    "force_norm_n",
    "_step5d_stage25_control_mode",
    "_step5d_stage25_echo_consumed",
    "_step5d_intervention_reason",
    "_step5d_outer_xdot_limited_approach_normal_m_s",
    "_step5d_jqdot_raw_approach_normal_m_s",
    "_step5d_jqdot_cmd_approach_normal_m_s",
    "_step5d_constraint_residual_norm",
    "_step5d_lambda_norm",
    "_step5d_active_bounds_count",
]


def _p0_no_contact_state(*, normal_acquired: bool) -> bridge.BridgeState:
    state = bridge.BridgeState()
    state.normal_acquired = normal_acquired
    state.latched_normal_b = (0.0, 0.0, 1.0)
    state.filtered_normal_b = (0.0, 0.0, 1.0)
    state.latched_normal_locked = True
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


def _p0_fake_runtime(state: bridge.BridgeState, _args: object) -> None:
    state.step5d_model_bundle = SimpleNamespace(
        model=SimpleNamespace(
            lowerPositionLimit=np.full(6, -np.pi),
            upperPositionLimit=np.full(6, np.pi),
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


def _p0_fake_outer(*_args: object, **_kwargs: object) -> SimpleNamespace:
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


def _p0_no_contact_runtime_values(
    *,
    normal_acquired: bool,
    sensor_ok: float = 1.0,
) -> dict[str, float]:
    args = bridge.parse_args(
        [
            "--no-start-command",
            "--skip-dashboard-preflight",
            "--bridge-mode",
            "line",
            "--bridge-profile",
            iface.STEP5D_NO_CONTACT_P0_STAGE_ID,
            "--bridge-path-shape",
            "cycloid",
            "--step5d-stage25-control-mode",
            "speedj_rnn_live",
        ]
    )
    latest_zeroed = [0.0, 0.0, -12.0, 0.0, 0.0, 0.0]
    latest_output = {
        "actual_TCP_pose": [0.49, 0.14, 0.02, np.pi, 0.0, 0.0],
        "actual_TCP_speed": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "actual_q": [0.0] * 6,
        "actual_qd": [0.0] * 6,
        "output_double_register_35": 25.0,
    }
    state = _p0_no_contact_state(normal_acquired=normal_acquired)
    with (
        patch.object(bridge, "ensure_step5d_liveprep_runtime", _p0_fake_runtime),
        patch.object(bridge, "step5d_tcp_jacobian_base", return_value=np.eye(6)),
        patch.object(bridge, "step5d_omega_bounds", return_value=(np.full(6, -0.05), np.full(6, 0.05))),
        patch.object(bridge, "compute_step5d_outer_loop", side_effect=_p0_fake_outer),
        patch.object(bridge, "rnn_target_state_from_outer_loop", return_value={"shadow": True}),
    ):
        return bridge.compute_bridge_values(
            args,
            latest_zeroed,
            latest_output,
            sensor_ok,
            state,
            0.002,
        )


def write_p0_run(run_dir: Path, rows: list[dict[str, str]]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "bridge_rtde_500hz.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=P0_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def good_rows() -> list[dict[str, str]]:
    rows = []
    for idx in range(4):
        rows.append(
            {
                "t_monotonic_s": f"{1.0 + 0.002 * idx:.6f}",
                "ur_output_double_register_35": "25.0",
                "_step4e_normal_load_n": "0.300000",
                "force_norm_n": "0.800000",
                "_step5d_stage25_control_mode": "speedj_rnn_live",
                "_step5d_stage25_echo_consumed": "1",
                "_step5d_intervention_reason": "solver_warm_start" if idx == 0 else "none",
                "_step5d_outer_xdot_limited_approach_normal_m_s": "0.000100000",
                "_step5d_jqdot_raw_approach_normal_m_s": "0.000095000",
                "_step5d_jqdot_cmd_approach_normal_m_s": "0.000094000",
                "_step5d_constraint_residual_norm": "0.000020000",
                "_step5d_lambda_norm": "0.012000000",
                "_step5d_active_bounds_count": "0",
            }
        )
    return rows


P0_V4_SHA256 = {
    ".script": "860e0f126d45e4c791a04b8a48b36d19feeffba651f708d6e6cc5bd5b57d90b7",
    ".txt": "a93a75ada8ed6beb9aca1fcd431cae352ba5beb585ae887f54e1f872a195b70c",
    ".urp": "fdfdbfd0948d3b4c79f1a059b2be61019cb0ad0b8ec015b793cbac1dae608d9e",
}


def install_sandbox_p0_readback(config_root: Path, run_root: Path) -> Path:
    manifest_rel = "runs/controller_readback_step5d_strict_rnn_no_contact_p0_v4_test/manifest.json"
    manifest_path = run_root / "controller_readback_step5d_strict_rnn_no_contact_p0_v4_test" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "validation": {
                    "program": iface.STEP5D_NO_CONTACT_P0_STAGE_ID,
                    "target_dir": "/programs/andyl/kunwei/step5",
                    "script_node_path": f"/programs/andyl/kunwei/step5/{iface.STEP5D_NO_CONTACT_P0_STAGE_ID}.script",
                },
                "sha256": {
                    "local": P0_V4_SHA256,
                    "controller": P0_V4_SHA256,
                    "readback": P0_V4_SHA256,
                },
            }
        ),
        encoding="utf-8",
    )
    current_path = config_root / "current_stage.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    capture = current["bridge_trigger"]["no_contact_p0_capture"]
    capture["controller_readback_manifest"] = manifest_rel
    capture["controller_readback_verified"] = True
    capture["sha256"] = P0_V4_SHA256
    current_path.write_text(json.dumps(current), encoding="utf-8")

    table_path = config_root / "step5_stage_table.json"
    table = json.loads(table_path.read_text(encoding="utf-8"))
    row = next(row for row in table["stages"] if row.get("id") == iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
    row["package_delivery"]["controller_readback_manifest"] = manifest_rel
    row["package_delivery"]["controller_readback_status"] = "verified_gate_package"
    row["package_delivery"]["sha256"] = P0_V4_SHA256
    table_path.write_text(json.dumps(table), encoding="utf-8")
    return manifest_path


class Step5dNoContactP0Test(unittest.TestCase):
    def test_no_contact_p0_package_enters_stage25_without_contact_acquire(self) -> None:
        self.assertEqual(iface.STEP5D_NO_CONTACT_P0_STAGE_ID, "step5d_strict_rnn_no_contact_p0_v4")
        spec = liveprep.spec_for(iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        stamp = "2026-07-07T1000HKT_STEP5D_STRICT_RNN_NO_CONTACT_P0_V4"
        script = liveprep.build_script(
            stamp,
            "2026-07-06T21:00:00+08:00",
            liveprep.line_cfg(liveprep.load_json(liveprep.CONFIG_PATH)),
            liveprep.load_safe_frame(spec),
            spec,
        )
        txt = liveprep.build_txt(stamp, spec)
        urp = liveprep.build_urp(script, spec.program_name, spec.controller_dir)

        liveprep.validate_package(script, txt, urp, stamp, spec)
        bad_xml = gzip.decompress(urp).decode("utf-8").replace(
            f"\n\ncodex_{spec.program_name}()\n</cachedContents>",
            "\n</cachedContents>",
        )
        with self.assertRaisesRegex(RuntimeError, "cachedContents"):
            liveprep.validate_package(script, txt, gzip.compress(bad_xml.encode("utf-8")), stamp, spec)
        self.assertIn("NO_CONTACT_P0_CAPTURE", script + txt)
        self.assertIn("write_output_float_register(35, 25.95)", script)
        self.assertIn("write_output_float_register(35, 25.0)", script)
        self.assertIn("write_output_float_register(28, 0.0)", script)
        self.assertIn("write_output_float_register(26, heartbeat)", script)
        self.assertIn("write_output_float_register(27, sensor_ok)", script)
        self.assertIn("write_output_float_register(29, t_wait)", script)
        self.assertIn("write_output_float_register(30, healthy_heartbeat_count)", script)
        self.assertIn("write_output_float_register(31, sensor_ok_seen_num)", script)
        self.assertIn("local healthy_heartbeat_count = 0.0", script)
        self.assertIn("local required_healthy_heartbeats = 2.0", script)
        self.assertIn("if sensor_ok >= 0.5:", script)
        self.assertIn("if heartbeat != last_healthy_heartbeat:", script)
        self.assertIn("if healthy_heartbeat_count >= required_healthy_heartbeats:", script)
        self.assertNotIn("if heartbeat_seen and sensor_ok >= 0.5:", script)
        self.assertIn("write_output_float_register(36, 20.95)", script)
        self.assertIn("write_output_float_register(36, 20.90)", script)
        self.assertIn("local joint_layout_code = 524.000", script)
        self.assertIn("speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5]", script)
        self.assertIn("codex_wait_for_bridge_ready(60.0)", script)
        self.assertNotIn("codex_wait_for_sensor_ok(60.0)", script)
        self.assertIn("local stage25_runtime_limit_s = 65.000", script)
        self.assertNotIn("write_output_float_register(35, 24.0)", script)
        self.assertNotIn("write_output_float_register(35, 25.3)", script)
        self.assertNotIn("codex_step5d_down_search", script)
        self.assertNotIn("deadband contact acquire", txt)
        self.assertEqual(spec.controller_dir, "/programs/andyl/kunwei/step5")

    def test_no_contact_p0_runtime_interface_defaults_follow_step5d_full_window(self) -> None:
        runtime = iface.resolve_runtime_interface(program=iface.STEP5D_NO_CONTACT_P0_STAGE_ID, root=ROOT, env={})

        self.assertEqual(runtime.program, iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        self.assertEqual(runtime.stage25_control_mode, "speedj_rnn_live")
        self.assertEqual(runtime.bridge_defaults.duration_s, 180.0)
        self.assertEqual(runtime.hard_contract["stage25_success_target_s"], 60.0)
        self.assertEqual(runtime.hard_contract["stage25_runtime_limit_s"], 65.0)
        self.assertEqual(runtime.bridge_defaults.max_normal_force_n, 2.0)
        self.assertEqual(runtime.bridge_defaults.max_force_norm_n, 5.0)
        self.assertEqual(runtime.bridge_defaults.max_torque_norm_nm, 3.0)
        self.assertTrue(runtime.hard_contract["no_contact_p0_capture"])
        self.assertIn("no-contact", runtime.register_contract["stage25_0"])

    def test_no_contact_p0_profile_is_step5_table_reference(self) -> None:
        stage = step5_table.step5_stage(iface.STEP5D_NO_CONTACT_P0_STAGE_ID)

        self.assertEqual(stage["id"], iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        self.assertEqual(stage["shape"], "cycloid")
        self.assertFalse(stage["contact"])
        self.assertTrue(stage["bridge"])
        self.assertTrue(stage["strict_rnn"])
        self.assertEqual(stage["duration_s"], 60.0)
        self.assertEqual(stage["guard"]["stage25_success_target_s"], 60.0)
        self.assertEqual(stage["guard"]["stage25_runtime_limit_s"], 65.0)
        self.assertEqual(stage["runtime_interface_ref"]["stage25_success_target_s"], 60.0)
        self.assertEqual(stage["runtime_interface_ref"]["stage25_runtime_limit_s"], 65.0)

        ref = step5_table.step5_path_reference(
            iface.STEP5D_NO_CONTACT_P0_STAGE_ID,
            pose_xy=(0.0, 0.0),
            elapsed_s=0.0,
        )
        self.assertEqual(ref["stage_id"], iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        self.assertEqual(ref["path_time_s"], 0.0)
        self.assertIn("desired_velocity_xy", ref)

    def test_no_contact_p0_version_is_single_v3_across_current_runtime_wrapper_and_table(self) -> None:
        wrapper = (ROOT / "scripts" / "step5d-strict-rnn-p0.sh").read_text(encoding="utf-8")
        bridge_operator = (ROOT / "scripts" / "bridge-line-operator.sh").read_text(encoding="utf-8")
        current = json.loads((ROOT / "config" / "current_stage.json").read_text(encoding="utf-8"))
        stage = step5_table.step5_stage(iface.STEP5D_NO_CONTACT_P0_STAGE_ID)

        self.assertEqual(iface.STEP5D_NO_CONTACT_P0_STAGE_ID, "step5d_strict_rnn_no_contact_p0_v4")
        self.assertIn('P0_PROFILE="step5d_strict_rnn_no_contact_p0_v4"', wrapper)
        self.assertIn('STEP5D_NO_CONTACT_P0_PROFILE="step5d_strict_rnn_no_contact_p0_v4"', bridge_operator)
        self.assertEqual(current["bridge_trigger"]["no_contact_p0_capture"]["profile"], iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        self.assertIn("P0_PROFILE", wrapper)
        self.assertEqual(current["bridge_trigger"]["no_contact_p0_capture"]["controller_target"], stage["package_delivery"]["controller_target"])

    def test_no_contact_p0_runtime_interface_ignores_ambient_live_caps(self) -> None:
        runtime = iface.resolve_runtime_interface(
            program=iface.STEP5D_NO_CONTACT_P0_STAGE_ID,
            root=ROOT,
            env={
                "BRIDGE_DURATION_S": "180",
                "BRIDGE_TARGET_FORCE_N": "12",
                "BRIDGE_BASELINE_S": "5",
                "BRIDGE_REZERO_S": "1",
                "BRIDGE_FORCE_P_GAIN": "0.9",
                "BRIDGE_FORCE_I_GAIN": "0.8",
                "BRIDGE_FORCE_DAMPING": "0.7",
                "BRIDGE_INTEGRAL_LIMIT_N_S": "0.6",
                "BRIDGE_NORMAL_FILTER_ALPHA": "0.99",
                "BRIDGE_RTDE_HZ": "125",
                "BRIDGE_SENSOR_STALE_S": "9",
                "BRIDGE_SOCKET_TIMEOUT_S": "8",
                "MAX_NORMAL_FORCE_N": "50",
                "MAX_FORCE_NORM_N": "60",
                "MAX_TORQUE_NORM_NM": "4",
                "BRIDGE_MOTION_LIMIT_M_S": "0.5",
                "BRIDGE_TOTAL_LINEAR_LIMIT_M_S": "0.006",
                "BRIDGE_NORMAL_VELOCITY_LIMIT_M_S": "0.5",
                "BRIDGE_ANGULAR_LIMIT_RAD_S": "1.5",
                "BRIDGE_NORMAL_MIN_FORCE_N": "2",
                "STEP5D_STAGE25_CONTROL_MODE": "speedl_cartesian_oracle",
                "STEP5D_PRELOAD_FILTERED_MIN_N": "9",
                "STEP5D_PRELOAD_FILTERED_MAX_N": "99",
                "STEP5D_PRELOAD_RAW_MIN_N": "8",
                "STEP5D_PRELOAD_RAW_MAX_N": "88",
                "STEP5D_PRELOAD_FORCE_NORM_MAX_N": "77",
                "STEP5D_PRELOAD_HOLD_S": "6",
                "STEP5D_PRELOAD_TIMEOUT_S": "9",
            },
        )

        self.assertEqual(runtime.preload_gate.filtered_min_n, 0.0)
        self.assertEqual(runtime.preload_gate.filtered_max_n, 2.0)
        self.assertEqual(runtime.preload_gate.raw_min_n, 0.0)
        self.assertEqual(runtime.preload_gate.raw_max_n, 2.0)
        self.assertEqual(runtime.preload_gate.force_norm_max_n, 5.0)
        self.assertEqual(runtime.preload_gate.hold_s, 0.0)
        self.assertEqual(runtime.preload_gate.timeout_s, 1.0)
        self.assertEqual(runtime.bridge_defaults.duration_s, 180.0)
        self.assertEqual(runtime.bridge_defaults.target_force_n, 1.0)
        self.assertEqual(runtime.bridge_defaults.baseline_s, 1.0)
        self.assertEqual(runtime.bridge_defaults.rezero_s, 0.25)
        self.assertEqual(runtime.bridge_defaults.force_p_gain, 0.001)
        self.assertEqual(runtime.bridge_defaults.force_i_gain, 0.00001)
        self.assertEqual(runtime.bridge_defaults.force_damping, 7.0)
        self.assertEqual(runtime.bridge_defaults.integral_limit_n_s, 1.0)
        self.assertEqual(runtime.bridge_defaults.normal_filter_alpha, 0.55)
        self.assertEqual(runtime.bridge_defaults.rtde_hz, 500.0)
        self.assertEqual(runtime.bridge_defaults.sensor_stale_s, 0.10)
        self.assertEqual(runtime.bridge_defaults.socket_timeout_s, 0.0)
        self.assertEqual(runtime.bridge_defaults.max_normal_force_n, 2.0)
        self.assertEqual(runtime.bridge_defaults.max_force_norm_n, 5.0)
        self.assertEqual(runtime.bridge_defaults.max_torque_norm_nm, 3.0)
        self.assertEqual(runtime.bridge_defaults.normal_velocity_limit_m_s, 0.003)
        self.assertEqual(runtime.bridge_defaults.total_linear_limit_m_s, 0.004)
        self.assertEqual(runtime.bridge_defaults.angular_limit_rad_s, 0.015)
        self.assertEqual(runtime.bridge_defaults.normal_min_force_n, 0.001)

    def test_no_contact_p0_stage25_writer_operates_without_normal_acquired_when_sensor_ok(self) -> None:
        values = _p0_no_contact_runtime_values(normal_acquired=False, sensor_ok=1.0)
        self.assertEqual(values["step4e_controller_state"], bridge.STEP5D_STAGE25_JOINT_LAYOUT_CODE)
        self.assertEqual(values["step4e_cmd_valid"], 1.0)
        self.assertEqual(values["_step5d_stage25_control_mode"], "speedj_rnn_live")

    def test_no_contact_p0_bridge_parse_args_uses_full_window_no_contact_defaults(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--skip-dashboard-preflight",
                "--bridge-mode",
                "line",
                "--bridge-profile",
                iface.STEP5D_NO_CONTACT_P0_STAGE_ID,
                "--bridge-path-shape",
                "cycloid",
                "--rtde-hz",
                "125",
                "--sensor-stale-s",
                "9",
                "--socket-timeout-s",
                "8",
                "--bridge-force-p-gain",
                "0.9",
                "--bridge-force-i-gain",
                "0.8",
                "--bridge-force-damping",
                "0.7",
                "--bridge-integral-limit-n-s",
                "0.6",
                "--bridge-motion-limit-m-s",
                "0.5",
                "--bridge-total-linear-limit-m-s",
                "0.5",
                "--bridge-normal-velocity-limit-m-s",
                "0.5",
                "--bridge-normal-filter-alpha",
                "0.99",
                "--bridge-angular-limit-rad-s",
                "1.5",
            ]
        )

        self.assertEqual(args.bridge_profile, iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        self.assertEqual(args.step5d_stage25_control_mode, "speedj_rnn_live")
        self.assertEqual(args.duration_s, 180.0)
        self.assertEqual(args.baseline_s, 1.0)
        self.assertEqual(args.rezero_s, 0.25)
        self.assertEqual(args.target_force_n, 1.0)
        self.assertEqual(args.rtde_hz, 500.0)
        self.assertEqual(args.sensor_stale_s, 0.10)
        self.assertEqual(args.socket_timeout_s, 0.0)
        self.assertEqual(args.bridge_force_p_gain, 0.001)
        self.assertEqual(args.step4e_force_p_gain, 0.001)
        self.assertEqual(args.bridge_force_i_gain, 0.00001)
        self.assertEqual(args.step4e_force_i_gain, 0.00001)
        self.assertEqual(args.bridge_force_damping, 7.0)
        self.assertEqual(args.step4e_force_damping, 7.0)
        self.assertEqual(args.bridge_integral_limit_n_s, 1.0)
        self.assertEqual(args.step4e_integral_limit_n_s, 1.0)
        self.assertEqual(args.bridge_motion_limit_m_s, 0.004)
        self.assertEqual(args.step4e_motion_limit_m_s, 0.004)
        self.assertEqual(args.bridge_total_linear_limit_m_s, 0.004)
        self.assertEqual(args.step4e_total_linear_limit_m_s, 0.004)
        self.assertEqual(args.bridge_normal_velocity_limit_m_s, 0.003)
        self.assertEqual(args.step4e_normal_velocity_limit_m_s, 0.003)
        self.assertEqual(args.bridge_normal_filter_alpha, 0.55)
        self.assertEqual(args.step4e_normal_filter_alpha, 0.55)
        self.assertEqual(args.bridge_angular_limit_rad_s, 0.015)
        self.assertEqual(args.step4e_angular_limit_rad_s, 0.015)
        self.assertEqual(args.bridge_normal_min_force_n, 0.001)
        self.assertTrue(args.bridge_integrate_stage25_only)
        self.assertEqual(args.step5d_preload_timeout_s, 1.0)
        self.assertEqual(args.step5d_preload_hold_s, 0.0)
        self.assertEqual(args.max_normal_force_n, 2.0)
        self.assertEqual(args.max_force_norm_n, 5.0)
        self.assertEqual(args.max_torque_norm_nm, 3.0)

    def test_worktree_no_contact_p0_triplet_matches_current_stage_metadata(self) -> None:
        stem = ROOT / "programs" / "step5" / "step5d" / iface.STEP5D_NO_CONTACT_P0_STAGE_ID
        files = {ext: stem.with_suffix(ext) for ext in (".script", ".txt", ".urp")}
        current = json.loads((ROOT / "config" / "current_stage.json").read_text(encoding="utf-8"))
        capture = current["bridge_trigger"]["no_contact_p0_capture"]

        self.assertFalse(capture["capture_authorized"])
        self.assertFalse(capture["passed"])
        self.assertEqual(capture["local_triplet"], "programs/step5/step5d/step5d_strict_rnn_no_contact_p0_v4")
        self.assertEqual(
            capture["controller_target"],
            "/programs/andyl/kunwei/step5/step5d_strict_rnn_no_contact_p0_v4.urp",
        )
        for ext, path in files.items():
            self.assertTrue(path.exists(), path)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), capture["sha256"][ext])

        spec = liveprep.spec_for(iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        liveprep.validate_package(
            files[".script"].read_text(encoding="utf-8"),
            files[".txt"].read_text(encoding="utf-8"),
            files[".urp"].read_bytes(),
            "STEP5D_STRICT_RNN_NO_CONTACT_P0_V4",
            spec,
        )

    def test_passes_when_first_speedj_rnn_tick_has_warm_start_and_same_normal_sign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, good_rows())

            result = p0.verify_run_dir(run_dir)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["first_tick"]["intervention_reason"], "solver_warm_start")
        self.assertGreater(result["first_tick"]["jqdot_raw_approach_normal_m_s"], 0.0)
        self.assertEqual(result["blockers"], [])

    def test_fails_without_solver_warm_start_on_first_speedj_rnn_tick(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_intervention_reason"] = "none"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_missing_solver_warm_start", result["blockers"])

    def test_fails_when_raw_jqdot_unloads_while_outer_presses(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_jqdot_raw_approach_normal_m_s"] = "-0.000300000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_press_unload_mismatch", result["blockers"])

    def test_fails_when_outer_command_is_not_pressing(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_outer_xdot_limited_approach_normal_m_s"] = "0.000000000"
        rows[0]["_step5d_jqdot_raw_approach_normal_m_s"] = "0.000000000"
        rows[0]["_step5d_jqdot_cmd_approach_normal_m_s"] = "0.000000000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_outer_not_pressing", result["blockers"])

    def test_fails_when_cmd_jqdot_unloads_while_outer_presses(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_jqdot_cmd_approach_normal_m_s"] = "-0.000300000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_cmd_press_unload_mismatch", result["blockers"])

    def test_fails_when_speedj_rnn_rows_are_not_stage25(self) -> None:
        rows = good_rows()
        for row in rows:
            row["ur_output_double_register_35"] = "24.0"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("no_stage25_speedj_rnn_live_rows", result["blockers"])

    def test_fails_when_any_speedj_rnn_row_is_outside_stage25(self) -> None:
        rows = good_rows()
        rows.append(good_rows()[0] | {"ur_output_double_register_35": "24.0"})
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("non_stage25_speedj_rnn_live_rows_present", result["blockers"])

    def test_fails_when_first_speedj_rnn_tick_was_not_consumed(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_stage25_echo_consumed"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_not_consumed_by_stage25", result["blockers"])

    def test_fails_when_first_speedj_rnn_tick_consumed_evidence_is_fractional(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_stage25_echo_consumed"] = "0.6"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_not_consumed_by_stage25", result["blockers"])

    def test_fails_when_run_is_not_no_contact(self) -> None:
        rows = good_rows()
        rows[0]["_step4e_normal_load_n"] = "5.100000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("normal_load_exceeds_no_contact_limit", result["blockers"])

    def test_fails_when_first_load_or_force_evidence_is_nonfinite(self) -> None:
        rows = good_rows()
        rows[0]["_step4e_normal_load_n"] = "nan"
        rows[0]["force_norm_n"] = "nan"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_missing_normal_load_evidence", result["blockers"])
        self.assertIn("first_speedj_rnn_tick_missing_force_norm_evidence", result["blockers"])

    def test_fails_when_first_lambda_evidence_is_nonfinite(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_lambda_norm"] = "nan"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_missing_lambda_norm", result["blockers"])

    def test_fails_when_active_bounds_evidence_is_fractional(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_active_bounds_count"] = "0.4"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_missing_active_bounds_count", result["blockers"])

    def test_cli_writes_summary_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            summary = Path(tmp) / "p0_summary.json"
            write_p0_run(run_dir, good_rows())

            completed = subprocess.run(
                [
                    "python3",
                    str(ROOT / "tools" / "verify_step5d_no_contact_p0.py"),
                    str(run_dir),
                    "--output",
                    str(summary),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertTrue(summary.exists())
            self.assertTrue(json.loads(summary.read_text(encoding="utf-8"))["ok"])

    def test_cli_returns_24_when_artifact_fails(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_jqdot_cmd_approach_normal_m_s"] = "-0.000300000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            write_p0_run(run_dir, rows)

            completed = subprocess.run(
                [
                    "python3",
                    str(ROOT / "tools" / "verify_step5d_no_contact_p0.py"),
                    str(run_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 24, completed.stdout + completed.stderr)
            self.assertIn("first_speedj_rnn_tick_cmd_press_unload_mismatch", completed.stdout)

    def test_operator_script_exposes_capture_bridge_without_contact_bridge(self) -> None:
        script = (ROOT / "scripts" / "step5d-strict-rnn-p0.sh").read_text(encoding="utf-8")

        self.assertIn("validate-run", script)
        self.assertIn("live-ready", script)
        self.assertIn("capture-ready", script)
        self.assertIn("capture-bridge", script)
        self.assertIn("LIVE STEP5D STRICT RNN NO CONTACT P0", script)
        self.assertIn("step5d_strict_rnn_no_contact_p0_v4", script)
        self.assertIn("BRIDGE_ALLOW_NO_CONTACT_P0_CAPTURE=1", script)
        self.assertIn("verify_step5d_no_contact_p0.py", script)
        self.assertIn("step5d_no_contact_p0_summary", script)
        self.assertIn("BRIDGE_DURATION_S=180", script)
        self.assertNotIn("contact-bridge", script)

    def test_capture_bridge_refuses_without_p0_confirm_before_starting_bridge(self) -> None:
        completed = subprocess.run(
            ["bash", str(ROOT / "scripts" / "step5d-strict-rnn-p0.sh"), "capture-bridge"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 40, completed.stdout + completed.stderr)
        self.assertIn("STEP5D_P0_CONFIRM", completed.stdout + completed.stderr)
        self.assertNotIn("bridge output:", completed.stdout)

    def test_capture_bridge_hands_off_hardened_p0_env_and_validates_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            scripts_dir = sandbox / "scripts"
            tools_dir = sandbox / "tools"
            run_dir = sandbox / "run"
            scripts_dir.mkdir()
            tools_dir.mkdir()
            run_dir.mkdir()
            shutil.copytree(ROOT / "config", sandbox / "config")
            install_sandbox_p0_readback(sandbox / "config", sandbox / "runs")
            wrapper = scripts_dir / "step5d-strict-rnn-p0.sh"
            wrapper.write_text((ROOT / "scripts" / "step5d-strict-rnn-p0.sh").read_text(encoding="utf-8"), encoding="utf-8")
            wrapper.chmod(0o755)
            bridge_spy = scripts_dir / "bridge-line-operator.sh"
            bridge_spy.write_text(
                f"""#!/usr/bin/env bash
set -euo pipefail
env >"{sandbox / 'bridge_env.txt'}"
printf '%s\\n' "$@" >"{sandbox / 'bridge_argv.txt'}"
echo "[operator] bridge output: {run_dir}"
""",
                encoding="utf-8",
            )
            bridge_spy.chmod(0o755)
            verifier_spy = tools_dir / "verify_step5d_no_contact_p0.py"
            verifier_spy.write_text(
                f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path
Path({str(sandbox / 'verifier_argv.txt')!r}).write_text("\\n".join(sys.argv[1:]), encoding="utf-8")
out = Path(sys.argv[sys.argv.index("--output") + 1])
out.write_text(json.dumps({{"ok": True}}), encoding="utf-8")
""",
                encoding="utf-8",
            )
            verifier_spy.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "STEP5D_P0_CONFIRM": "LIVE STEP5D STRICT RNN NO CONTACT P0",
                    "MAX_NORMAL_FORCE_N": "50",
                    "MAX_FORCE_NORM_N": "60",
                    "MAX_TORQUE_NORM_NM": "4",
                    "BRIDGE_DURATION_S": "180",
                    "BRIDGE_TARGET_FORCE_N": "12",
                    "STEP5D_STAGE25_CONTROL_MODE": "speedl_cartesian_oracle",
                    "STEP5D_PRELOAD_FILTERED_MAX_N": "99",
                }
            )

            completed = subprocess.run(
                ["bash", str(wrapper), "capture-bridge"],
                cwd=sandbox,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual((sandbox / "bridge_argv.txt").read_text(encoding="utf-8").strip(), "line-bridge-fast")
            bridge_env = dict(
                line.split("=", 1)
                for line in (sandbox / "bridge_env.txt").read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(bridge_env["BRIDGE_PROFILE"], "step5d_strict_rnn_no_contact_p0_v4")
            self.assertEqual(bridge_env["BRIDGE_ALLOW_NO_CONTACT_P0_CAPTURE"], "1")
            self.assertEqual(bridge_env["BRIDGE_DURATION_S"], "180")
            self.assertEqual(bridge_env["BRIDGE_RTDE_HZ"], "500")
            self.assertEqual(bridge_env["BRIDGE_SENSOR_STALE_S"], "0.10")
            self.assertEqual(bridge_env["BRIDGE_SOCKET_TIMEOUT_S"], "0.0")
            self.assertEqual(bridge_env["BRIDGE_TARGET_FORCE_N"], "1.0")
            self.assertEqual(bridge_env["BRIDGE_FORCE_P_GAIN"], "0.001")
            self.assertEqual(bridge_env["BRIDGE_FORCE_I_GAIN"], "0.00001")
            self.assertEqual(bridge_env["BRIDGE_FORCE_DAMPING"], "7.0")
            self.assertEqual(bridge_env["BRIDGE_INTEGRAL_LIMIT_N_S"], "1.0")
            self.assertEqual(bridge_env["MAX_NORMAL_FORCE_N"], "2")
            self.assertEqual(bridge_env["MAX_FORCE_NORM_N"], "5")
            self.assertEqual(bridge_env["MAX_TORQUE_NORM_NM"], "3.0")
            self.assertEqual(bridge_env["BRIDGE_NORMAL_MIN_FORCE_N"], "0.001")
            self.assertEqual(bridge_env["BRIDGE_NORMAL_FILTER_ALPHA"], "0.55")
            self.assertEqual(bridge_env["BRIDGE_MOTION_LIMIT_M_S"], "0.004")
            self.assertEqual(bridge_env["BRIDGE_NORMAL_VELOCITY_LIMIT_M_S"], "0.003")
            self.assertEqual(bridge_env["STEP5D_STAGE25_CONTROL_MODE"], "speedj_rnn_live")
            self.assertEqual(bridge_env["STEP5D_PRELOAD_FILTERED_MAX_N"], "2.0")
            verifier_args = (sandbox / "verifier_argv.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(verifier_args, [str(run_dir), "--output", str(run_dir / "step5d_no_contact_p0_summary.json")])
            self.assertTrue((run_dir / "step5d_no_contact_p0_summary.json").exists())
            self.assertIn("Step5d no-contact P0 summary", completed.stdout)

    def test_capture_ready_preflight_rejects_stale_no_contact_p0_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            scripts_dir = sandbox / "scripts"
            shutil.copytree(ROOT / "scripts", scripts_dir)
            shutil.copytree(ROOT / "tools", sandbox / "tools")
            shutil.copytree(ROOT / "config", sandbox / "config")
            wrapper = scripts_dir / "step5d-strict-rnn-p0.sh"
            current_path = sandbox / "config" / "current_stage.json"
            current = json.loads(current_path.read_text(encoding="utf-8"))
            current["bridge_trigger"]["no_contact_p0_capture"]["profile"] = "step5d_strict_rnn_no_contact_p0_v2"
            current_path.write_text(json.dumps(current), encoding="utf-8")

            completed = subprocess.run(
                [str(wrapper), "capture-ready"],
                cwd=sandbox,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 24, completed.stdout + completed.stderr)
            self.assertIn(
                "refusing no-contact P0: current capture profile is step5d_strict_rnn_no_contact_p0_v2, expected step5d_strict_rnn_no_contact_p0_v4",
                completed.stdout + completed.stderr,
            )

    def test_capture_ready_preflight_rejects_missing_no_contact_p0_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            scripts_dir = sandbox / "scripts"
            shutil.copytree(ROOT / "scripts", scripts_dir)
            shutil.copytree(ROOT / "tools", sandbox / "tools")
            shutil.copytree(ROOT / "config", sandbox / "config")
            wrapper = scripts_dir / "step5d-strict-rnn-p0.sh"
            current_path = sandbox / "config" / "current_stage.json"
            current = json.loads(current_path.read_text(encoding="utf-8"))
            current["bridge_trigger"]["no_contact_p0_capture"]["controller_readback_manifest"] = (
                "runs/controller_readback_step5d_strict_rnn_no_contact_p0_v4_LOCAL_PENDING_READBACK/missing_manifest.json"
            )
            current_path.write_text(json.dumps(current), encoding="utf-8")

            completed = subprocess.run(
                [str(wrapper), "capture-ready"],
                cwd=sandbox,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 24, completed.stdout + completed.stderr)
            self.assertIn(
                "refusing no-contact P0: manifest missing: runs/controller_readback_step5d_strict_rnn_no_contact_p0_v4_LOCAL_PENDING_READBACK/missing_manifest.json",
                completed.stdout + completed.stderr,
            )

    def test_capture_ready_preflight_rejects_incorrect_no_contact_p0_manifest_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            scripts_dir = sandbox / "scripts"
            shutil.copytree(ROOT / "scripts", scripts_dir)
            shutil.copytree(ROOT / "tools", sandbox / "tools")
            shutil.copytree(ROOT / "config", sandbox / "config")
            manifest_path = install_sandbox_p0_readback(sandbox / "config", sandbox / "runs")
            wrapper = scripts_dir / "step5d-strict-rnn-p0.sh"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["validation"]["program"] = "step5d_strict_rnn_no_contact_p0_bad"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            completed = subprocess.run(
                [str(wrapper), "capture-ready"],
                cwd=sandbox,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 24, completed.stdout + completed.stderr)
            self.assertIn(
                "refusing no-contact P0: table/current mismatch: ['validation program']",
                completed.stdout + completed.stderr,
            )

    def test_existing_failed_v3_artifact_is_still_rejected_with_no_speedj_rnn_live_rows(self) -> None:
        run_dir = ROOT / "runs/bridge_step4e_line_outerloop_step5d_strict_rnn_no_contact_p0_v3_autowatch_20260707_084025"
        completed = subprocess.run(
            [
                "python3",
                str(ROOT / "tools" / "verify_step5d_no_contact_p0.py"),
                str(run_dir),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 24, completed.stdout + completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"], payload)
        self.assertIn("no_speedj_rnn_live_rows", payload["blockers"])
        self.assertEqual(payload["speedj_rnn_live_rows"], 0)

    def test_capture_ready_hardens_no_contact_caps_against_ambient_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            shutil.copytree(ROOT / "scripts", sandbox / "scripts")
            shutil.copytree(ROOT / "tools", sandbox / "tools")
            shutil.copytree(ROOT / "config", sandbox / "config")
            install_sandbox_p0_readback(sandbox / "config", sandbox / "runs")
            wrapper = sandbox / "scripts" / "step5d-strict-rnn-p0.sh"
            env = os.environ.copy()
            env.update(
                {
                    "BRIDGE_DURATION_S": "180",
                    "BRIDGE_TARGET_FORCE_N": "12",
                    "BRIDGE_BASELINE_S": "5",
                    "BRIDGE_REZERO_S": "1",
                    "BRIDGE_FORCE_P_GAIN": "0.9",
                    "BRIDGE_FORCE_I_GAIN": "0.8",
                    "BRIDGE_FORCE_DAMPING": "0.7",
                    "BRIDGE_INTEGRAL_LIMIT_N_S": "0.6",
                    "BRIDGE_NORMAL_FILTER_ALPHA": "0.99",
                    "BRIDGE_RTDE_HZ": "125",
                    "BRIDGE_SENSOR_STALE_S": "9",
                    "BRIDGE_SOCKET_TIMEOUT_S": "8",
                    "MAX_NORMAL_FORCE_N": "50",
                    "MAX_FORCE_NORM_N": "60",
                    "MAX_TORQUE_NORM_NM": "4",
                    "BRIDGE_MOTION_LIMIT_M_S": "0.5",
                    "BRIDGE_TOTAL_LINEAR_LIMIT_M_S": "0.006",
                    "BRIDGE_NORMAL_VELOCITY_LIMIT_M_S": "0.5",
                    "BRIDGE_ANGULAR_LIMIT_RAD_S": "1.5",
                    "BRIDGE_NORMAL_MIN_FORCE_N": "2",
                    "STEP5D_PRELOAD_FILTERED_MAX_N": "99",
                    "STEP5D_PRELOAD_TIMEOUT_S": "9",
                    "STEP5D_STAGE25_CONTROL_MODE": "speedl_cartesian_oracle",
                }
            )

            completed = subprocess.run(
                ["bash", str(wrapper), "capture-ready"],
                cwd=sandbox,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("[caps] total_linear=0.004m/s angular=0.015rad/s hard_force=2/5N torque=3Nm", completed.stdout)
            self.assertIn("[tuning] preload filtered=0..2N raw=0..2N force_norm<=5N hold=0s", completed.stdout)


if __name__ == "__main__":
    unittest.main()
