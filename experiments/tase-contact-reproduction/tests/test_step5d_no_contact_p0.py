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
from contact_semantics import twist_base_to_same_origin, twist_same_origin_to_base  # noqa: E402
from step5d_paper_outer_loop import Step5dOuterLoopState  # noqa: E402


P0_FIELDS = [
    "t_monotonic_s",
    "ur_output_double_register_35",
    "step4e_cmd_valid",
    "_step4e_normal_load_n",
    "force_norm_n",
    "_step5d_stage25_control_mode",
    "_step5d_stage25_echo_layout_tag",
    "_step5d_stage25_echo_consumed",
    "_step5d_stage25_echo_cmd_valid",
    "_step5d_intervention_reason",
    "_step5d_outer_xdot_limited_approach_normal_m_s",
    "_step5d_jqdot_raw_approach_normal_m_s",
    "_step5d_jqdot_cmd_approach_normal_m_s",
    "_step5d_qdot_max_abs_rad_s",
    "_step5d_qdot_cap_rad_s",
    "_step5d_constraint_residual_norm",
    "_step5d_lambda_norm",
    "_step5d_active_bounds_count",
    "_step5d_rnn_inner_iterations",
    "_step5d_rnn_backend",
    "_step5d_rnn_epsilon",
    "_step5d_rnn_sigr_exponent_r",
    "_step5d_p0_low_force_posture_policy",
    "_step5d_p0_low_force_posture_active",
    "_step5d_p0_posture_gain_scale",
    "_step5d_p0_effective_ko",
    "_step5d_p0_frame_transform_valid",
    "_step5d_p0_frame_transform_mode",
    "_step5d_p0_frame_transform_reason",
    "_step5d_p0_limited_tcp_vx_m_s",
    "_step5d_p0_limited_tcp_vy_m_s",
    "_step5d_p0_limited_tcp_vz_m_s",
    "_step5d_p0_limited_tcp_wx_rad_s",
    "_step5d_p0_limited_tcp_wy_rad_s",
    "_step5d_p0_limited_tcp_wz_rad_s",
    "_step5d_p0_limited_base_vx_m_s",
    "_step5d_p0_limited_base_vy_m_s",
    "_step5d_p0_limited_base_vz_m_s",
    "_step5d_p0_tcp_press_speed_m_s",
    "_step5d_p0_rnn_accepted",
    "_step5d_p0_rnn_reject_reason",
    "_step5d_p0_safe_hold_active",
    "_step5d_cmd_valid_reason",
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
                qdot=(0.002, 0.0015, -0.002666666666667, 0.004, -0.003, 0.020),
                solver_status=40.0,
                residual_norm=0.0001,
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


def _p0_large_outer(*_args: object, **_kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(
        xdot_c=np.array([0.0100, 0.0000, -0.00015, 0.0150, 0.0, 0.0]),
        next_state=Step5dOuterLoopState(),
        diagnostics={
            "outer_orientation_angle_rad": 0.0,
            "e_f": 0.0,
            "R_d_z_dot_R_cur_z": 1.0,
            "force_sign_convention": "step5_step6_positive_normal_load",
        },
    )


def _p0_oversized_outer(*_args: object, **_kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(
        xdot_c=np.array([0.0200, -0.0300, 0.0400, 0.0300, -0.0200, 0.0180]),
        next_state=Step5dOuterLoopState(),
        diagnostics={
            "outer_orientation_angle_rad": 0.0,
            "e_f": 0.0,
            "R_d_z_dot_R_cur_z": 1.0,
            "force_sign_convention": "step5_step6_positive_normal_load",
        },
    )


def _p0_upward_outer(*_args: object, **_kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(
        xdot_c=np.array([0.0100, 0.0100, 0.0200, 0.0, 0.0, 0.0]),
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
    outer_side_effect: object = _p0_fake_outer,
    rnn_target_side_effect: object | None = None,
    jacobian: np.ndarray | None = None,
    latest_zeroed_override: list[float] | None = None,
    tcp_pose_override: list[float] | None = None,
    solver: object | None = None,
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
    latest_zeroed = [0.0, 0.0, -12.0, 0.0, 0.0, 0.0] if latest_zeroed_override is None else latest_zeroed_override
    latest_output = {
        "actual_TCP_pose": [0.49, 0.14, 0.02, np.pi, 0.0, 0.0],
        "actual_TCP_speed": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "actual_q": [0.0] * 6,
        "actual_qd": [0.0] * 6,
        "output_double_register_35": 25.0,
    }
    if tcp_pose_override is not None:
        latest_output["actual_TCP_pose"] = tcp_pose_override
    state = _p0_no_contact_state(normal_acquired=normal_acquired)
    _p0_fake_runtime(state, args)
    if solver is not None:
        state.step5d_solver = solver
    rnn_target = {"shadow": True} if rnn_target_side_effect is None else rnn_target_side_effect
    with (
        patch.object(
            bridge,
            "ensure_step5d_liveprep_runtime",
            side_effect=AssertionError("hot path must use prewarmed Step5d runtime"),
        ),
        patch.object(bridge, "step5d_tcp_jacobian_base", return_value=np.eye(6) if jacobian is None else jacobian),
        patch.object(bridge, "step5d_omega_bounds", return_value=(np.full(6, -0.15), np.full(6, 0.15))),
        patch.object(bridge, "compute_step5d_outer_loop", side_effect=outer_side_effect),
        patch.object(
            bridge,
            "rnn_target_state_from_outer_loop",
            return_value=rnn_target if rnn_target_side_effect is None else None,
            side_effect=None if rnn_target_side_effect is None else rnn_target_side_effect,
        ),
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
                "step4e_cmd_valid": "1",
                "_step4e_normal_load_n": "0.300000",
                "force_norm_n": "0.800000",
                "_step5d_stage25_control_mode": "speedj_rnn_live",
                "_step5d_stage25_echo_layout_tag": "524.0",
                "_step5d_stage25_echo_consumed": "1",
                "_step5d_stage25_echo_cmd_valid": "1",
                "_step5d_intervention_reason": "solver_warm_start" if idx == 0 else "none",
                "_step5d_outer_xdot_limited_approach_normal_m_s": "0.000100000",
                "_step5d_jqdot_raw_approach_normal_m_s": "0.000095000",
                "_step5d_jqdot_cmd_approach_normal_m_s": "0.000094000",
                "_step5d_qdot_max_abs_rad_s": "0.000120000",
                "_step5d_qdot_cap_rad_s": "0.150000000",
                "_step5d_constraint_residual_norm": "0.000020000",
                "_step5d_lambda_norm": "0.012000000",
                "_step5d_active_bounds_count": "0",
                "_step5d_rnn_inner_iterations": "1024",
                "_step5d_rnn_backend": "cupy",
                "_step5d_rnn_epsilon": "0.010000000",
                "_step5d_rnn_sigr_exponent_r": "0.800000000",
                "_step5d_p0_low_force_posture_policy": "freeze_until_contact_v1",
                "_step5d_p0_low_force_posture_active": "1",
                "_step5d_p0_posture_gain_scale": "0.000000000",
                "_step5d_p0_effective_ko": "0.000000000",
                "_step5d_p0_frame_transform_valid": "1",
                "_step5d_p0_frame_transform_mode": "tcp_same_origin_v1",
                "_step5d_p0_frame_transform_reason": "ok",
                "_step5d_p0_limited_tcp_vx_m_s": "0.000000000",
                "_step5d_p0_limited_tcp_vy_m_s": "0.000000000",
                "_step5d_p0_limited_tcp_vz_m_s": "0.000100000",
                "_step5d_p0_limited_tcp_wx_rad_s": "0.000000000",
                "_step5d_p0_limited_tcp_wy_rad_s": "0.000000000",
                "_step5d_p0_limited_tcp_wz_rad_s": "0.000000000",
                "_step5d_p0_limited_base_vx_m_s": "0.000000000",
                "_step5d_p0_limited_base_vy_m_s": "0.000000000",
                "_step5d_p0_limited_base_vz_m_s": "-0.000100000",
                "_step5d_p0_tcp_press_speed_m_s": "0.000100000",
                "_step5d_p0_rnn_accepted": "1",
                "_step5d_p0_rnn_reject_reason": "ok",
                "_step5d_p0_safe_hold_active": "0",
                "_step5d_cmd_valid_reason": "rnn_accepted",
            }
        )
    return rows


def verify_p0_unit_run(run_dir: Path, **kwargs: object) -> dict[str, object]:
    kwargs.setdefault("min_stage25_accepted_duration_s", 0.0)
    return p0.verify_run_dir(run_dir, **kwargs)


P0_V7_STAGE_ID = "step5d_strict_rnn_no_contact_p0_v7"
P0_V7_SHA256 = {
    ".script": "2e22e7bc89355d7d01c6f0102bcce4103f104bce0a4f1bab72a044934745a297",
    ".txt": "2d88b3f6d670a9eaf8790c50b4286b1000d9c4121ba65d0c4fcf3a2e3ead8663",
    ".urp": "e06d7da1949a2f76ab57e6786296a6f65e8c2a6808223fe95ef9709b687de251",
}


def install_sandbox_p0_readback(config_root: Path, run_root: Path) -> Path:
    manifest_rel = f"runs/controller_readback_{P0_V7_STAGE_ID}_test/manifest.json"
    manifest_path = run_root / f"controller_readback_{P0_V7_STAGE_ID}_test" / "manifest.json"
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
                    "local": P0_V7_SHA256,
                    "controller": P0_V7_SHA256,
                    "readback": P0_V7_SHA256,
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
    capture["capture_authorized"] = True
    capture["delivery_mode"] = "full_upload_readback"
    capture["sha256"] = P0_V7_SHA256
    current_path.write_text(json.dumps(current), encoding="utf-8")

    table_path = config_root / "step5_stage_table.json"
    table = json.loads(table_path.read_text(encoding="utf-8"))
    row = next(row for row in table["stages"] if row.get("id") == iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
    row["package_delivery"]["controller_readback_manifest"] = manifest_rel
    row["package_delivery"]["controller_readback_status"] = "verified_gate_package"
    row["package_delivery"]["controller_readback_verified"] = True
    row["package_delivery"]["delivery_mode"] = "full_upload_readback"
    row["package_delivery"]["sha256"] = P0_V7_SHA256
    table_path.write_text(json.dumps(table), encoding="utf-8")
    return manifest_path


class Step5dNoContactP0Test(unittest.TestCase):
    def test_bridge_run_manifest_and_latest_pointer_are_written_before_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / f"bridge_{P0_V7_STAGE_ID}_test"
            run_dir.mkdir(parents=True)
            pointer_path = root / "runs" / "latest_run_pointer.json"
            args = SimpleNamespace(
                output_dir=run_dir,
                bridge_profile=P0_V7_STAGE_ID,
                rtde_hz=500.0,
                bridge_mode="line",
            )

            manifest = bridge.write_bridge_run_manifest(
                args,
                argv=["--bridge-profile", P0_V7_STAGE_ID, "--rtde-hz", "500"],
                pointer_path=pointer_path,
            )

            manifest_path = run_dir / "bridge_run_manifest.json"
            self.assertTrue(manifest_path.is_file())
            saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_manifest, manifest)
            self.assertEqual(saved_manifest["schema"], "bridge_run_manifest.v1")
            self.assertEqual(saved_manifest["run_dir"], str(run_dir.resolve()))
            self.assertEqual(saved_manifest["profile"], P0_V7_STAGE_ID)
            self.assertEqual(saved_manifest["rtde_hz"], 500.0)
            self.assertEqual(
                saved_manifest["argv"],
                ["--bridge-profile", P0_V7_STAGE_ID, "--rtde-hz", "500"],
            )
            self.assertEqual(pointer["schema"], "bridge_latest_run_pointer.v1")
            self.assertEqual(pointer["run_dir"], str(run_dir.resolve()))
            self.assertEqual(pointer["profile"], P0_V7_STAGE_ID)
            self.assertEqual(pointer["manifest"], str(manifest_path))
            self.assertGreaterEqual(pointer["started_at_epoch_s"], saved_manifest["started_at_epoch_s"])

    def test_no_contact_p0_package_enters_stage25_without_contact_acquire(self) -> None:
        self.assertEqual(iface.STEP5D_NO_CONTACT_P0_STAGE_ID, P0_V7_STAGE_ID)
        spec = liveprep.spec_for(iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        stamp = "2026-07-08T0600HKT_STEP5D_STRICT_RNN_NO_CONTACT_P0_V7"
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

    def test_no_contact_p0_v7_tp_script_is_v5_lifecycle_clone(self) -> None:
        v5 = (ROOT / "programs" / "step5" / "step5d" / "step5d_strict_rnn_no_contact_p0_v5.script").read_text(encoding="utf-8")
        v7 = (ROOT / "programs" / "step5" / "step5d" / f"{P0_V7_STAGE_ID}.script").read_text(encoding="utf-8")
        txt = (ROOT / "programs" / "step5" / "step5d" / f"{P0_V7_STAGE_ID}.txt").read_text(encoding="utf-8")
        spec = liveprep.spec_for(P0_V7_STAGE_ID)

        def normalize(script: str) -> str:
            return (
                script.replace("2026-07-07T1821HKT_STEP5D_STRICT_RNN_NO_CONTACT_P0_V5", "STAMP")
                .replace("2026-07-08T0600HKT_STEP5D_STRICT_RNN_NO_CONTACT_P0_V7", "STAMP")
                .replace("# GENERATED_AT_LOCAL: 2026-07-07T18:21:26+08:00", "# GENERATED_AT_LOCAL: GENERATED_AT")
                .replace("# GENERATED_AT_LOCAL: 2026-07-08T06:00:00+08:00", "# GENERATED_AT_LOCAL: GENERATED_AT")
                .replace("codex_step5d_strict_rnn_no_contact_p0_v5", "codex_step5d_strict_rnn_no_contact_p0")
                .replace("codex_step5d_strict_rnn_no_contact_p0_v7", "codex_step5d_strict_rnn_no_contact_p0")
            )

        self.assertEqual(normalize(v7), normalize(v5))
        self.assertIn("local joint_layout_code = 524.000", v7)
        self.assertIn("local qdot_cap_rad_s = 0.150", v7)
        self.assertIn("qdot cap: 0.150 rad/s", txt)
        self.assertIn("speedj acceleration: 0.050 rad/s^2", txt)
        self.assertIn("speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5]", v7)
        self.assertIn("codex_wait_for_bridge_ready(60.0)", v7)
        self.assertNotIn("codex_wait_for_sensor_ok(60.0)", v7)
        self.assertIn("local stage25_runtime_limit_s = 65.000", v7)
        self.assertNotIn("write_output_float_register(35, 24.0)", v7)
        self.assertNotIn("write_output_float_register(35, 25.3)", v7)
        self.assertNotIn("codex_step5d_down_search", v7)
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
        self.assertEqual(stage["guard"]["qdot_cap_rad_s"], 0.15)
        self.assertEqual(stage["guard"]["qdot_slew_rad_s2"], 0.0)
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

    def test_no_contact_p0_version_is_single_v7_across_current_runtime_wrapper_and_table(self) -> None:
        wrapper = (ROOT / "scripts" / "step5d-strict-rnn-p0.sh").read_text(encoding="utf-8")
        bridge_operator = (ROOT / "scripts" / "bridge-line-operator.sh").read_text(encoding="utf-8")
        current = json.loads((ROOT / "config" / "current_stage.json").read_text(encoding="utf-8"))
        stage = step5_table.step5_stage(iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        active_row = step5_table.step5_stage(current["current_stage_id"])
        active_p0 = active_row["package_delivery"]["strict_rnn_no_contact_p0"]
        capture = current["bridge_trigger"]["no_contact_p0_capture"]

        self.assertEqual(iface.STEP5D_NO_CONTACT_P0_STAGE_ID, P0_V7_STAGE_ID)
        self.assertIn(
            f'P0_PROFILE="${{STEP5D_P0_PROFILE_OVERRIDE:-{P0_V7_STAGE_ID}}}"',
            wrapper,
        )
        self.assertIn(
            f'STEP5D_NO_CONTACT_P0_PROFILE="{P0_V7_STAGE_ID}"',
            bridge_operator,
        )
        v8_wrapper = (ROOT / "scripts" / "step5d-strict-rnn-p0-v8.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'STEP5D_P0_PROFILE_OVERRIDE="step5d_strict_rnn_no_contact_p0_v8"',
            v8_wrapper,
        )
        self.assertEqual(capture["profile"], iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        self.assertIn("P0_PROFILE", wrapper)
        self.assertEqual(capture["controller_target"], stage["package_delivery"]["controller_target"])
        self.assertEqual(active_row["operator_lifecycle"]["no_contact_p0_expected_program"], capture["controller_target"])
        self.assertEqual(active_p0["program_basename"], iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        self.assertEqual(active_p0["controller_target"], capture["controller_target"])
        self.assertEqual(active_p0["controller_readback_manifest"], capture["controller_readback_manifest"])
        self.assertEqual(active_p0["sha256"], capture["sha256"])

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
        self.assertNotEqual(
            (
                values["step4e_cmd_vx_m_s"],
                values["step4e_cmd_vy_m_s"],
                values["step4e_cmd_vz_m_s"],
            ),
            (0.0, 0.0, 0.0),
        )
        self.assertLessEqual(abs(values["_step5d_predicted_tcp_vx_m_s"]), 0.010000001)
        self.assertLessEqual(abs(values["_step5d_predicted_tcp_vy_m_s"]), 0.010000001)
        self.assertLessEqual(abs(values["_step5d_predicted_tcp_vz_m_s"]), 0.020000001)
        self.assertEqual(values["_step5d_stage25_control_mode"], "speedj_rnn_live")
        self.assertIn("no_contact_p0_component_velocity_clamped", values["_step5d_intervention_reason"])
        self.assertNotIn("no_contact_p0_qdot_gate", values["_step5d_intervention_reason"])

    def test_no_contact_p0_ignores_contact_orientation_semantic_gate(self) -> None:
        values = _p0_no_contact_runtime_values(
            normal_acquired=False,
            sensor_ok=1.0,
            tcp_pose_override=[0.49, 0.14, 0.02, 0.0, 0.0, 0.0],
        )

        self.assertEqual(values["step4e_controller_state"], bridge.STEP5D_STAGE25_JOINT_LAYOUT_CODE)
        self.assertEqual(values["step4e_cmd_valid"], 1.0)
        self.assertEqual(values["_step5d_outer_orientation_error_rad"], 0.0)
        self.assertGreater(values["_step5d_contact_orientation_error_rad"], bridge.STEP5D_SEMANTIC_ORIENTATION_TOLERANCE_RAD)
        self.assertNotIn("Step5d semantic gate blocked", values.get("_step5d_solver_error", ""))

    def test_same_origin_twist_transform_uses_block_diagonal_rotation(self) -> None:
        rotation_base_from_tcp = np.diag([1.0, -1.0, -1.0])
        twist_tcp = np.array([0.0, 0.0, 0.020, 0.0, 0.010, 0.0])

        twist_base = twist_same_origin_to_base(twist_tcp, rotation_base_from_tcp)
        roundtrip_tcp = twist_base_to_same_origin(twist_base, rotation_base_from_tcp)

        np.testing.assert_allclose(twist_base, np.array([0.0, 0.0, -0.020, 0.0, -0.010, 0.0]))
        np.testing.assert_allclose(roundtrip_tcp, twist_tcp)

    def test_no_contact_p0_low_force_posture_policy_freezes_until_contact(self) -> None:
        low = bridge.step5d_no_contact_p0_low_force_posture_policy(normal_load_n=0.0, base_ko=5.0)
        mid = bridge.step5d_no_contact_p0_low_force_posture_policy(normal_load_n=1.5, base_ko=5.0)
        high = bridge.step5d_no_contact_p0_low_force_posture_policy(normal_load_n=2.0, base_ko=5.0)

        self.assertEqual(low["policy"], "freeze_until_contact_v1")
        self.assertTrue(low["active"])
        self.assertAlmostEqual(low["effective_ko"], 0.0)
        self.assertAlmostEqual(low["orientation_gain_scale"], 0.0)
        self.assertGreater(mid["effective_ko"], low["effective_ko"])
        self.assertLess(mid["effective_ko"], high["effective_ko"])
        self.assertFalse(high["active"])
        self.assertAlmostEqual(high["effective_ko"], 5.0)
        self.assertAlmostEqual(high["orientation_gain_scale"], 1.0)

    def test_no_contact_p0_press_only_output_is_rnn_target_compatible(self) -> None:
        outer = bridge.step5d_no_contact_p0_press_only_outer_output(
            reaction_normal_b=(0.0, 0.0, 1.0),
            force_error_n=0.0,
        )

        target = bridge.rnn_target_state_from_outer_loop(
            outer,
            J=np.eye(6),
            omega_minus=np.full(6, -0.15),
            omega_plus=np.full(6, 0.15),
            dt_s=0.002,
            epsilon=0.010,
            r=0.8,
        )

        self.assertTrue(target["cmd_valid"])
        np.testing.assert_allclose(target["xdot_c"], np.array([0.0, 0.0, -0.00015, 0.0, 0.0, 0.0]))

    def test_no_contact_p0_runtime_logs_posture_and_oracle_diagnostics(self) -> None:
        values = _p0_no_contact_runtime_values(
            normal_acquired=True,
            sensor_ok=1.0,
            latest_zeroed_override=[0.0, 0.0, -0.2, 0.0, 0.0, 0.0],
        )

        self.assertEqual(values["_step5d_p0_low_force_posture_policy"], "freeze_until_contact_v1")
        self.assertEqual(values["_step5d_p0_low_force_posture_active"], 1.0)
        self.assertAlmostEqual(values["_step5d_p0_effective_ko"], 0.0)
        self.assertAlmostEqual(values["_step5d_p0_posture_gain_scale"], 0.0)
        self.assertIn("_step5d_oracle_residual_norm", values)
        self.assertIn("_step5d_cmd_residual_norm", values)
        self.assertIn("_step5d_rnn_vs_oracle_qdot_norm", values)
        self.assertTrue(np.isfinite(values["_step5d_oracle_residual_norm"]))
        self.assertTrue(np.isfinite(values["_step5d_cmd_residual_norm"]))

    def test_no_contact_p0_runtime_uses_press_only_target_without_outer_loop(self) -> None:
        captured_solver_targets: list[dict[str, object]] = []

        class CaptureSolver:
            def reset_state(self) -> None:
                return None

            def warm_start(self, **_kwargs: object) -> None:
                return None

            def solve(self, *, target_state: dict[str, object], **_kwargs: object) -> SimpleNamespace:
                captured_solver_targets.append(target_state)
                return SimpleNamespace(
                    qdot=(0.0, 0.0, -0.00015, 0.0, 0.0, 0.0),
                    solver_status=40.0,
                    residual_norm=0.0100,
                    diagnostics={
                        "lambda_state": np.array([1.0, 0.0, 0.0, 0.0, 0.0, 0.0]),
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

        values = _p0_no_contact_runtime_values(
            normal_acquired=True,
            latest_zeroed_override=[0.0, 0.0, -0.2, 0.0, 0.0, 0.0],
            outer_side_effect=AssertionError("P0 no-contact must bypass force/admittance outer loop"),
            solver=CaptureSolver(),
        )

        self.assertEqual(len(captured_solver_targets), 1)
        np.testing.assert_allclose(
            np.asarray(captured_solver_targets[0]["xdot_c"], dtype=float),
            np.array([0.0, 0.0, -0.00015, 0.0, 0.0, 0.0]),
            atol=1e-12,
        )
        self.assertEqual(values["step4e_cmd_valid"], 1.0)
        self.assertAlmostEqual(values["_step5d_p0_limited_tcp_vx_m_s"], 0.0)
        self.assertAlmostEqual(values["_step5d_p0_limited_tcp_vy_m_s"], 0.0)
        self.assertAlmostEqual(values["_step5d_p0_limited_tcp_vz_m_s"], 0.00015)
        self.assertAlmostEqual(values["_step5d_p0_limited_base_vz_m_s"], -0.00015)
        self.assertAlmostEqual(values["_step5d_p0_limited_tcp_wx_rad_s"], 0.0)
        self.assertAlmostEqual(values["_step5d_p0_limited_tcp_wy_rad_s"], 0.0)
        self.assertAlmostEqual(values["_step5d_p0_limited_tcp_wz_rad_s"], 0.0)
        self.assertTrue(np.isfinite(values["_step5d_lambda_norm"]))
        self.assertTrue(np.isfinite(values["_step5d_constraint_residual_norm"]))
        self.assertIn("no_contact_p0_evidence:constraint_residual_norm_exceeds_p0_limit", values["_step5d_intervention_reason"])

    def test_no_contact_p0_qdot_gate_does_not_reject_legacy_total_tcp_norm(self) -> None:
        gate = bridge.step5d_no_contact_p0_qdot_acceptance_gate(
            qdot=(0.0035, 0.0035, -0.00014, 0.0, 0.0, 0.0),
            jacobian=np.eye(6),
            outer_xdot_limited=(0.0035, 0.0035, -0.00015, 0.0, 0.0, 0.0),
            reaction_normal_b=(0.0, 0.0, 1.0),
            residual_norm=0.0001,
            active_bounds_count=0,
            max_tcp_speed_m_s=0.004,
            max_normal_tracking_error_m_s=0.0005,
            max_residual_norm=0.001,
        )

        self.assertTrue(gate["accepted"])
        self.assertEqual(gate["reason"], "ok")
        self.assertEqual(tuple(gate["qdot"]), (0.0035, 0.0035, -0.00014, 0.0, 0.0, 0.0))
        self.assertGreater(gate["predicted_tcp_speed_m_s"], 0.004)

    def test_no_contact_p0_qdot_component_clamp_has_no_total_tcp_norm_cap(self) -> None:
        qdot, diagnostics = bridge.limit_step5d_no_contact_p0_qdot_command(
            (0.30, -0.20, 0.040, 0.030, -0.020, 0.018),
            np.eye(6),
            qdot_cap_rad_s=0.15,
        )
        predicted = np.eye(6) @ qdot

        self.assertTrue(diagnostics["component_clamp_active"])
        self.assertTrue(diagnostics["qdot_clip_active"])
        self.assertLessEqual(max(abs(float(value)) for value in qdot), 0.150000001)
        self.assertLessEqual(abs(predicted[0]), 0.010000001)
        self.assertLessEqual(abs(predicted[1]), 0.010000001)
        self.assertLessEqual(abs(predicted[2]), 0.020000001)
        self.assertLessEqual(max(abs(float(value)) for value in predicted[3:]), 0.015000001)
        self.assertGreater(np.linalg.norm(predicted[:3]), 0.004)

    def test_no_contact_p0_qdot_gate_rejects_nonfinite_residual_and_bounds(self) -> None:
        for residual_norm, active_bounds_count, reason in (
            (float("nan"), 0, "nonfinite_residual_norm"),
            (0.0, float("nan"), "nonfinite_active_bounds_count"),
        ):
            with self.subTest(reason=reason):
                gate = bridge.step5d_no_contact_p0_qdot_acceptance_gate(
                    qdot=(0.0, 0.0, -0.00014, 0.0, 0.0, 0.0),
                    jacobian=np.eye(6),
                    outer_xdot_limited=(0.0, 0.0, -0.00015, 0.0, 0.0, 0.0),
                    reaction_normal_b=(0.0, 0.0, 1.0),
                    residual_norm=residual_norm,
                    active_bounds_count=active_bounds_count,
                    max_tcp_speed_m_s=0.004,
                    max_normal_tracking_error_m_s=0.0005,
                    max_residual_norm=0.001,
                )

                self.assertFalse(gate["accepted"])
                self.assertEqual(gate["reason"], reason)
                self.assertEqual(tuple(gate["qdot"]), (0.0,) * 6)

    def test_no_contact_p0_qdot_gate_rejects_nonfinite_reaction_normal(self) -> None:
        gate = bridge.step5d_no_contact_p0_qdot_acceptance_gate(
            qdot=(0.0, 0.0, -0.00014, 0.0, 0.0, 0.0),
            jacobian=np.eye(6),
            outer_xdot_limited=(0.0, 0.0, -0.00015, 0.0, 0.0, 0.0),
            reaction_normal_b=(float("nan"), 0.0, 1.0),
            residual_norm=0.0001,
            active_bounds_count=0,
            max_tcp_speed_m_s=0.004,
            max_normal_tracking_error_m_s=0.0005,
            max_residual_norm=0.001,
        )

        self.assertFalse(gate["accepted"])
        self.assertEqual(gate["reason"], "nonfinite_or_bad_shape")
        self.assertEqual(tuple(gate["qdot"]), (0.0,) * 6)

    def test_no_contact_p0_qdot_gate_rejects_base_upward_escape(self) -> None:
        gate = bridge.step5d_no_contact_p0_qdot_acceptance_gate(
            qdot=(0.0, 0.0, 0.00002, 0.0, 0.0, 0.0),
            jacobian=np.eye(6),
            outer_xdot_limited=(0.0, 0.0, -0.00015, 0.0, 0.0, 0.0),
            reaction_normal_b=(0.0, 0.0, 1.0),
            residual_norm=0.0001,
            active_bounds_count=0,
            max_tcp_speed_m_s=0.004,
            max_normal_tracking_error_m_s=0.0005,
            max_residual_norm=0.001,
        )

        self.assertFalse(gate["accepted"])
        self.assertEqual(gate["reason"], "base_upward_escape")
        self.assertEqual(tuple(gate["qdot"]), (0.0,) * 6)

    def test_no_contact_p0_qdot_gate_preserves_bounded_qdot_for_evidence_rejections(self) -> None:
        cases = (
            {
                "qdot": (0.0, 0.0, -0.00014, 0.0, 0.0, 0.0),
                "outer": (0.0, 0.0, 0.00015, 0.0, 0.0, 0.0),
                "residual": 0.0001,
                "active_bounds": 0,
                "reason": "outer_approach_not_pressing",
            },
            {
                "qdot": (0.0, 0.0, -0.00014, 0.0, 0.0, 0.0),
                "outer": (0.0, 0.0, -0.00015, 0.0, 0.0, 0.0),
                "residual": 0.0100,
                "active_bounds": 0,
                "reason": "constraint_residual_norm_exceeds_p0_limit",
            },
            {
                "qdot": (0.0, 0.0, -0.00014, 0.0, 0.0, 0.0),
                "outer": (0.0, 0.0, -0.00015, 0.0, 0.0, 0.0),
                "residual": 0.0001,
                "active_bounds": 1,
                "reason": "active_bounds_exceeds_p0_limit",
            },
        )
        for case in cases:
            with self.subTest(reason=case["reason"]):
                gate = bridge.step5d_no_contact_p0_qdot_acceptance_gate(
                    qdot=case["qdot"],
                    jacobian=np.eye(6),
                    outer_xdot_limited=case["outer"],
                    reaction_normal_b=(0.0, 0.0, 1.0),
                    residual_norm=case["residual"],
                    active_bounds_count=case["active_bounds"],
                    max_tcp_speed_m_s=0.004,
                    max_normal_tracking_error_m_s=0.0005,
                    max_residual_norm=0.001,
                )

                self.assertFalse(gate["accepted"])
                self.assertEqual(gate["reason"], case["reason"])
                self.assertEqual(tuple(gate["qdot"]), case["qdot"])

    def test_no_contact_p0_qdot_gate_allows_small_aligned_command(self) -> None:
        gate = bridge.step5d_no_contact_p0_qdot_acceptance_gate(
            qdot=(0.0, 0.0, -0.00014, 0.0, 0.0, 0.0),
            jacobian=np.eye(6),
            outer_xdot_limited=(0.0, 0.0, -0.00015, 0.0, 0.0, 0.0),
            reaction_normal_b=(0.0, 0.0, 1.0),
            residual_norm=0.0001,
            active_bounds_count=0,
            max_tcp_speed_m_s=0.004,
            max_normal_tracking_error_m_s=0.0005,
            max_residual_norm=0.001,
        )

        self.assertTrue(gate["accepted"])
        self.assertEqual(tuple(gate["qdot"]), (0.0, 0.0, -0.00014, 0.0, 0.0, 0.0))
        self.assertAlmostEqual(gate["normal_tracking_error_m_s"], 0.00001)

    def test_no_contact_p0_press_only_target_is_already_joint_feasible(self) -> None:
        values = _p0_no_contact_runtime_values(
            normal_acquired=True,
            sensor_ok=1.0,
            jacobian=np.eye(6) * 0.05,
        )

        self.assertEqual(values["step4e_controller_state"], bridge.STEP5D_STAGE25_JOINT_LAYOUT_CODE)
        self.assertEqual(values["_step5d_stage25_control_mode"], "speedj_rnn_live")
        self.assertEqual(values["_step5d_qdot_cap_rad_s"], 0.15)
        self.assertLess(values["_step5d_jinv_xdot_inf_over_qdot_cap"], 1.0)
        self.assertEqual(values["_step5d_outer_xdot_limiter_active"], 0.0)
        self.assertLess(values["_step5d_outer_xdot_limited_norm"], 0.004)
        self.assertAlmostEqual(values["_step5d_outer_xdot_limited_norm"], 0.00015)
        self.assertIn("_step5d_outer_xdot_joint_feasible_norm", values)
        self.assertAlmostEqual(values["_step5d_outer_xdot_joint_feasible_norm"], values["_step5d_outer_xdot_limited_norm"])
        self.assertAlmostEqual(values["_step5d_xdot_feasibility_scale"], 1.0)
        self.assertEqual(values["_step5d_xdot_feasibility_scale_active"], 0.0)

    def test_no_contact_p0_component_clamps_oversized_outer_xdot(self) -> None:
        rotation_base_from_tcp = np.diag([1.0, -1.0, -1.0])
        xdot, active, diagnostics = bridge.limit_step5d_no_contact_p0_xdot_components(
            _p0_oversized_outer().xdot_c,
            rotation_base_from_tcp=rotation_base_from_tcp,
            return_diagnostics=True,
        )
        legacy_base_clip = np.array([0.010, -0.010, 0.020, 0.015, -0.015, 0.015])
        limited_tcp = twist_base_to_same_origin(xdot, rotation_base_from_tcp)

        self.assertTrue(active)
        self.assertEqual(diagnostics["mode"], "tcp_same_origin_v1")
        self.assertTrue(diagnostics["valid"])
        self.assertLessEqual(abs(float(limited_tcp[0])), 0.010000001)
        self.assertLessEqual(abs(float(limited_tcp[1])), 0.010000001)
        self.assertGreaterEqual(float(limited_tcp[2]), -1e-12)
        self.assertLessEqual(float(limited_tcp[2]), 0.020000001)
        self.assertLessEqual(float(xdot[2]), 1e-12)
        self.assertFalse(np.allclose(xdot, legacy_base_clip))

    def test_no_contact_p0_xdot_limiter_requires_tcp_rotation(self) -> None:
        with self.assertRaisesRegex(ValueError, "rotation_base_from_tcp"):
            bridge.limit_step5d_no_contact_p0_xdot_components(
                _p0_oversized_outer().xdot_c,
                return_diagnostics=True,
            )

    def test_no_contact_p0_ignores_upward_outer_candidate_and_keeps_press_only_target(self) -> None:
        values = _p0_no_contact_runtime_values(
            normal_acquired=True,
            sensor_ok=1.0,
            outer_side_effect=_p0_upward_outer,
            latest_zeroed_override=[0.0, 0.0, -0.2, 0.0, 0.0, 0.0],
        )

        self.assertEqual(values["step4e_controller_state"], bridge.STEP5D_STAGE25_JOINT_LAYOUT_CODE)
        self.assertEqual(values["step4e_cmd_valid"], 1.0)
        self.assertAlmostEqual(values["_step5d_p0_limited_tcp_vz_m_s"], 0.00015)
        self.assertLess(values["_step5d_p0_limited_base_vz_m_s"], 0.0)
        self.assertNotIn("p0_frame_transform:tcp_unload_or_upward_escape", values["_step5d_intervention_reason"])
        self.assertEqual(values["_step5d_rnn_backend"], "cupy")
        self.assertEqual(values["_step5d_rnn_inner_iterations"], 1024.0)
        self.assertAlmostEqual(values["_step5d_rnn_epsilon"], 0.010)
        self.assertAlmostEqual(values["_step5d_rnn_sigr_exponent_r"], 0.8)

    def test_no_contact_p0_does_not_apply_bridge_side_qdot_slew(self) -> None:
        with patch.object(
            bridge,
            "limit_step5d_qdot_slew",
            side_effect=AssertionError("P0 must rely on RNN dynamics plus qdot cap, not bridge-side slew"),
        ):
            values = _p0_no_contact_runtime_values(normal_acquired=True, sensor_ok=1.0)

        self.assertEqual(values["_step5d_qdot_slew_limiter_active"], 0.0)
        self.assertEqual(values["_step5d_qdot_cap_rad_s"], 0.15)

    def test_no_contact_p0_prewarm_metadata_runs_before_rtde_open(self) -> None:
        metadata = bridge.step5d_runtime_prewarm_metadata(iface.STEP5D_NO_CONTACT_P0_STAGE_ID)

        self.assertTrue(metadata["enabled"])
        self.assertEqual(metadata["status"], "not_required")
        self.assertTrue(metadata["before_socket_connect"])
        self.assertTrue(metadata["before_rtde_open"])

    def test_no_contact_p0_bridge_default_uses_v7_tuned_rnn_inner_loop(self) -> None:
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
            ]
        )

        self.assertEqual(args.step5d_epsilon, 0.010)
        self.assertEqual(args.step5d_sigr_exponent_r, 0.8)
        self.assertEqual(args.step5d_rnn_inner_iterations, 1024)
        self.assertEqual(args.step5d_rnn_backend, "cupy")

    def test_no_contact_p0_dashboard_watch_is_preflight_only(self) -> None:
        p0 = bridge.step5d_dashboard_watch_metadata(
            iface.STEP5D_NO_CONTACT_P0_STAGE_ID,
            skip_dashboard_preflight=False,
            disable_dashboard_program_watch=False,
            timeout_s=1.0,
        )
        liveprep = bridge.step5d_dashboard_watch_metadata(
            bridge.STEP5D_LIVEPREP_V24_STAGE_ID,
            skip_dashboard_preflight=False,
            disable_dashboard_program_watch=False,
            timeout_s=1.0,
        )

        self.assertFalse(p0["enabled"])
        self.assertEqual(p0["mode"], "preflight_only_for_no_contact_p0")
        self.assertTrue(liveprep["enabled"])
        self.assertEqual(liveprep["mode"], "runtime_dashboard_watch")

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
        self.assertEqual(args.step5d_qdot_limit_rad_s, 0.15)
        self.assertEqual(args.max_normal_force_n, 2.0)
        self.assertEqual(args.max_force_norm_n, 5.0)
        self.assertEqual(args.max_torque_norm_nm, 3.0)

    def test_worktree_no_contact_p0_triplet_matches_current_stage_metadata(self) -> None:
        stem = ROOT / "programs" / "step5" / "step5d" / iface.STEP5D_NO_CONTACT_P0_STAGE_ID
        files = {ext: stem.with_suffix(ext) for ext in (".script", ".txt", ".urp")}
        current = json.loads((ROOT / "config" / "current_stage.json").read_text(encoding="utf-8"))
        capture = current["bridge_trigger"]["no_contact_p0_capture"]

        self.assertFalse(capture["passed"])
        if capture["capture_authorized"]:
            self.assertTrue(capture["controller_readback_verified"])
            self.assertEqual(capture["delivery_mode"], "full_upload_readback")
            self.assertIsNotNone(capture["controller_readback_manifest"])
        else:
            self.assertTrue(capture["archived_not_gate_for_v29"])
            self.assertEqual(capture["status"], "abandoned_archived_not_v29_gate")
        self.assertEqual(capture["local_triplet"], f"programs/step5/step5d/{P0_V7_STAGE_ID}")
        self.assertEqual(
            capture["controller_target"],
            f"/programs/andyl/kunwei/step5/{P0_V7_STAGE_ID}.urp",
        )
        for ext, path in files.items():
            self.assertTrue(path.exists(), path)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), capture["sha256"][ext])

        spec = liveprep.spec_for(iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        liveprep.validate_package(
            files[".script"].read_text(encoding="utf-8"),
            files[".txt"].read_text(encoding="utf-8"),
            files[".urp"].read_bytes(),
            "STEP5D_STRICT_RNN_NO_CONTACT_P0_V7",
            spec,
        )

    def test_passes_when_first_speedj_rnn_tick_has_warm_start_and_same_normal_sign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, good_rows())

            result = p0.verify_run_dir(run_dir, min_stage25_accepted_duration_s=0.0)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["first_tick"]["intervention_reason"], "solver_warm_start")
        self.assertGreater(result["first_tick"]["jqdot_raw_approach_normal_m_s"], 0.0)
        self.assertEqual(result["blockers"], [])

    def test_fails_when_stage25_accepted_duration_is_shorter_than_success_target(self) -> None:
        rows = good_rows()
        rows[-1]["t_monotonic_s"] = "24.520000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("stage25_accepted_duration_below_success_target", result["blockers"])
        self.assertLess(result["metrics"]["stage25_accepted_duration_s"], 60.0)

    def test_fails_when_accepted_duration_is_sparse_not_continuous(self) -> None:
        rows = good_rows()
        rows[0]["t_monotonic_s"] = "1.000000"
        rows[1]["t_monotonic_s"] = "61.200000"
        rows[2]["step4e_cmd_valid"] = "0"
        rows[3]["step4e_cmd_valid"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("stage25_accepted_duration_continuity_gap", result["blockers"])

    def test_fails_when_accepted_rows_are_not_consumed_by_stage25(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_stage25_echo_consumed"] = "1"
        for row in rows[1:]:
            row["_step5d_stage25_echo_consumed"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("accepted_speedj_rnn_rows_not_consumed_by_stage25", result["blockers"])

    def test_entry_echo_lag_before_first_consumed_row_is_not_unconsumed_failure(self) -> None:
        rows = good_rows()
        for idx, row in enumerate(rows):
            row["_step5d_stage25_echo_consumed"] = "0" if idx < 2 else "1"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertTrue(result["entry_window"]["stage25_consumed_seen"])
        self.assertEqual(result["entry_window"]["stage25_consumed_first_row_index"], 2)
        self.assertNotIn("accepted_speedj_rnn_rows_not_consumed_by_stage25", result["blockers"])

    def test_evidence_rejected_rows_keep_cmd_valid_and_fail_on_verifier_evidence(self) -> None:
        rows = good_rows()
        for row in rows:
            row["_step5d_p0_rnn_accepted"] = "0"
            row["_step5d_p0_rnn_reject_reason"] = "constraint_residual_norm_exceeds_p0_limit"
            row["_step5d_p0_safe_hold_active"] = "1"
            row["_step5d_constraint_residual_norm"] = "0.003"
            row["step4e_cmd_valid"] = "1"
            row["_step5d_stage25_echo_consumed"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_constraint_residual_norm_exceeds_limit", result["blockers"])
        self.assertIn("no_accepted_speedj_rnn_live_rows", result["blockers"])
        self.assertNotIn("accepted_speedj_rnn_tick_constraint_residual_norm_exceeds_limit", result["blockers"])
        self.assertNotIn("accepted_speedj_rnn_rows_not_consumed_by_stage25", result["blockers"])
        self.assertEqual(result["metrics"]["accepted_speedj_rnn_live_rows"], 0)

    def test_fails_when_low_force_posture_evidence_is_missing(self) -> None:
        rows = good_rows()
        for row in rows:
            row.pop("_step5d_p0_low_force_posture_active")
            row.pop("_step5d_p0_posture_gain_scale")
            row.pop("_step5d_p0_effective_ko")
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("p0_low_force_posture_evidence_missing", result["blockers"])

    def test_fails_when_low_force_posture_gain_scale_does_not_prove_weak_target(self) -> None:
        rows = good_rows()
        for row in rows:
            row["_step5d_p0_posture_gain_scale"] = "1.000000000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("p0_low_force_posture_gain_scale_exceeds_limit", result["blockers"])

    def test_fails_when_no_accepted_speedj_rnn_rows_exist(self) -> None:
        rows = good_rows()
        for row in rows:
            row["step4e_cmd_valid"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("no_accepted_speedj_rnn_live_rows", result["blockers"])

    def test_fails_when_frame_transform_provenance_is_missing(self) -> None:
        rows = good_rows()
        for row in rows:
            for field in (
                "_step5d_p0_frame_transform_valid",
                "_step5d_p0_frame_transform_mode",
                "_step5d_p0_limited_tcp_vz_m_s",
                "_step5d_p0_limited_tcp_wx_rad_s",
                "_step5d_p0_limited_base_vz_m_s",
                "_step5d_p0_tcp_press_speed_m_s",
            ):
                row.pop(field, None)
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("p0_frame_transform_evidence_missing", result["blockers"])

    def test_fails_when_limited_tcp_angular_component_exceeds_cap(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_p0_limited_tcp_wy_rad_s"] = "0.030000000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("p0_limited_tcp_component_exceeds_cap", result["blockers"])

    def test_fails_when_limited_base_velocity_points_upward(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_p0_limited_base_vz_m_s"] = "0.020000000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("p0_limited_base_vz_points_upward", result["blockers"])

    def test_fails_when_gpu_backend_evidence_is_missing_or_wrong(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_rnn_backend"] = "numpy"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("p0_rnn_backend_not_cupy", result["blockers"])

    def test_fails_when_rnn_tuning_evidence_mismatches_v6_contract(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_rnn_inner_iterations"] = "512"
        rows[1]["_step5d_rnn_epsilon"] = "0.020000000"
        rows[2]["_step5d_rnn_sigr_exponent_r"] = "1.000000000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("p0_rnn_inner_iterations_below_min", result["blockers"])
        self.assertIn("p0_rnn_epsilon_mismatch", result["blockers"])
        self.assertIn("p0_rnn_sigr_exponent_r_mismatch", result["blockers"])

    def test_fails_without_solver_warm_start_on_first_speedj_rnn_tick(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_intervention_reason"] = "none"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_missing_solver_warm_start", result["blockers"])

    def test_fails_when_raw_jqdot_unloads_while_outer_presses(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_jqdot_raw_approach_normal_m_s"] = "-0.000300000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

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

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_outer_not_pressing", result["blockers"])

    def test_fails_when_cmd_jqdot_unloads_while_outer_presses(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_jqdot_cmd_approach_normal_m_s"] = "-0.000300000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_cmd_press_unload_mismatch", result["blockers"])

    def test_fails_when_speedj_rnn_rows_are_not_stage25(self) -> None:
        rows = good_rows()
        for row in rows:
            row["ur_output_double_register_35"] = "24.0"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("no_stage25_speedj_rnn_live_rows", result["blockers"])

    def test_fails_when_any_speedj_rnn_row_is_outside_stage25(self) -> None:
        rows = good_rows()
        rows.append(good_rows()[0] | {"ur_output_double_register_35": "24.0"})
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("non_stage25_speedj_rnn_live_rows_present", result["blockers"])

    def test_allows_stage25_consumed_echo_after_entry_latency(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_stage25_echo_consumed"] = "0"
        rows[1]["_step5d_stage25_echo_consumed"] = "0.6"
        rows[2]["_step5d_stage25_echo_consumed"] = "1"
        rows[0]["step4e_cmd_valid"] = "0"
        rows[1]["step4e_cmd_valid"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertTrue(result["ok"], result["blockers"])
        self.assertNotIn("first_speedj_rnn_tick_not_consumed_by_stage25", result["blockers"])
        self.assertEqual(result["entry_window"]["stage25_consumed_seen"], True)

    def test_allows_stage25_consumed_echo_at_entry_window_boundary(self) -> None:
        rows = good_rows()
        while len(rows) < p0.ENTRY_ECHO_WINDOW_ROWS:
            rows.append(good_rows()[0] | {"t_monotonic_s": f"{1.0 + 0.002 * len(rows):.6f}"})
        for row in rows:
            row["_step5d_stage25_echo_consumed"] = "0"
            row["step4e_cmd_valid"] = "0"
        rows[p0.ENTRY_ECHO_WINDOW_ROWS - 1]["_step5d_stage25_echo_consumed"] = "1"
        rows[p0.ENTRY_ECHO_WINDOW_ROWS - 1]["step4e_cmd_valid"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertTrue(result["ok"], result["blockers"])
        self.assertEqual(result["entry_window"]["stage25_consumed_first_row_index"], p0.ENTRY_ECHO_WINDOW_ROWS - 1)

    def test_fails_when_stage25_consumed_echo_arrives_after_entry_window(self) -> None:
        rows = good_rows()
        while len(rows) <= p0.ENTRY_ECHO_WINDOW_ROWS:
            rows.append(good_rows()[0] | {"t_monotonic_s": f"{1.0 + 0.002 * len(rows):.6f}"})
        for row in rows:
            row["_step5d_stage25_echo_consumed"] = "0"
        rows[p0.ENTRY_ECHO_WINDOW_ROWS]["_step5d_stage25_echo_consumed"] = "1"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("stage25_entry_window_not_consumed_by_stage25", result["blockers"])

    def test_fails_when_entry_window_never_consumes_stage25_echo(self) -> None:
        rows = good_rows()
        for row in rows:
            row["_step5d_stage25_echo_consumed"] = "0"
        rows[1]["_step5d_stage25_echo_consumed"] = "0.6"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("stage25_entry_window_not_consumed_by_stage25", result["blockers"])

    def test_fails_when_run_is_not_no_contact(self) -> None:
        rows = good_rows()
        rows[0]["_step4e_normal_load_n"] = "5.100000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("normal_load_exceeds_no_contact_limit", result["blockers"])

    def test_fails_when_first_load_or_force_evidence_is_nonfinite(self) -> None:
        rows = good_rows()
        rows[0]["_step4e_normal_load_n"] = "nan"
        rows[0]["force_norm_n"] = "nan"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_missing_normal_load_evidence", result["blockers"])
        self.assertIn("first_speedj_rnn_tick_missing_force_norm_evidence", result["blockers"])

    def test_fails_when_first_lambda_evidence_is_nonfinite(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_lambda_norm"] = "nan"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_missing_lambda_norm", result["blockers"])

    def test_fails_when_qdot_cap_evidence_is_missing(self) -> None:
        rows = good_rows()
        for row in rows:
            row["_step5d_qdot_cap_rad_s"] = ""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("qdot_cap_evidence_missing", result["blockers"])
        self.assertEqual(result["limits"]["qdot_cap_source"], "default")

    def test_fails_when_qdot_max_abs_evidence_is_missing(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_qdot_max_abs_rad_s"] = ""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_missing_qdot_max_abs", result["blockers"])
        self.assertIn("accepted_speedj_rnn_tick_missing_qdot_max_abs", result["blockers"])

    def test_fails_when_active_bounds_evidence_is_fractional(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_active_bounds_count"] = "0.4"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = verify_p0_unit_run(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_missing_active_bounds_count", result["blockers"])

    def test_fails_when_accepted_window_hits_rail_active_bounds_or_high_residual(self) -> None:
        cases = [
            ("_step5d_active_bounds_count", "1", "accepted_speedj_rnn_tick_active_bounds_exceeds_limit"),
            ("_step5d_constraint_residual_norm", "0.003", "accepted_speedj_rnn_tick_constraint_residual_norm_exceeds_limit"),
            ("_step5d_qdot_max_abs_rad_s", "0.150000000", "accepted_speedj_rnn_tick_qdot_hits_rail"),
        ]
        for field, value, blocker in cases:
            with self.subTest(blocker=blocker):
                rows = good_rows()
                rows[1][field] = value
                with tempfile.TemporaryDirectory() as tmp:
                    run_dir = Path(tmp)
                    write_p0_run(run_dir, rows)

                    result = verify_p0_unit_run(run_dir)

                self.assertFalse(result["ok"])
                self.assertIn(blocker, result["blockers"])
                self.assertIn("accepted_command_rail_fraction", result["metrics"])
                self.assertEqual(result["limits"]["qdot_rail_threshold_rad_s"], 0.15 - 1e-9)

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
                    "--min-stage25-accepted-duration-s",
                    "0",
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
        self.assertIn(P0_V7_STAGE_ID, script)
        self.assertIn("BRIDGE_ALLOW_NO_CONTACT_P0_CAPTURE=1", script)
        self.assertIn("verify_step5d_no_contact_p0.py", script)
        self.assertIn("step5d_no_contact_p0_summary", script)
        self.assertIn("export_stage_env.py", script)
        self.assertNotIn("BRIDGE_DURATION_S=180", script)
        self.assertNotIn("BRIDGE_MOTION_LIMIT_M_S=0.004", script)
        self.assertNotIn("BRIDGE_FORCE_I_GAIN=0.00001", script)
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
            run_dir = sandbox / "runs" / f"bridge_{P0_V7_STAGE_ID}_fake"
            scripts_dir.mkdir()
            tools_dir.mkdir()
            run_dir.mkdir(parents=True)
            shutil.copytree(ROOT / "config", sandbox / "config")
            install_sandbox_p0_readback(sandbox / "config", sandbox / "runs")
            wrapper = scripts_dir / "step5d-strict-rnn-p0.sh"
            wrapper.write_text((ROOT / "scripts" / "step5d-strict-rnn-p0.sh").read_text(encoding="utf-8"), encoding="utf-8")
            wrapper.chmod(0o755)
            for helper in ("export_stage_env.py", "step5d_runtime_interface.py", "tase_protocol_table.py"):
                shutil.copy2(ROOT / "tools" / helper, tools_dir / helper)
            bridge_spy = scripts_dir / "bridge-line-operator.sh"
            bridge_spy.write_text(
                f"""#!/usr/bin/env bash
set -euo pipefail
env >"{sandbox / 'bridge_env.txt'}"
printf '%s\\n' "$@" >>"{sandbox / 'bridge_argv.txt'}"
if [[ "${{1:-}}" == "prep-long-checks" ]]; then
  exit 0
fi
python3 - <<'PY'
import json
import time
from pathlib import Path

run_dir = Path({str(run_dir)!r})
manifest_path = run_dir / "bridge_run_manifest.json"
started = time.time()
manifest = {{
    "schema": "bridge_run_manifest.v1",
    "run_dir": str(run_dir),
	    "profile": "{P0_V7_STAGE_ID}",
    "started_at_epoch_s": started,
    "pid": 12345,
    "argv": ["line-bridge-fast"],
}}
manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
pointer = {{
    "schema": "bridge_latest_run_pointer.v1",
    "run_dir": str(run_dir),
	    "profile": "{P0_V7_STAGE_ID}",
    "started_at_epoch_s": started,
    "pid": 12345,
    "manifest": str(manifest_path),
}}
(Path({str(sandbox)!r}) / "runs" / "latest_run_pointer.json").write_text(json.dumps(pointer), encoding="utf-8")
PY
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
            self.assertEqual(
                (sandbox / "bridge_argv.txt").read_text(encoding="utf-8").splitlines(),
                ["prep-long-checks", "line-bridge-fast"],
            )
            bridge_env = dict(
                line.split("=", 1)
                for line in (sandbox / "bridge_env.txt").read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(bridge_env["BRIDGE_PROFILE"], P0_V7_STAGE_ID)
            self.assertEqual(bridge_env["BRIDGE_ALLOW_NO_CONTACT_P0_CAPTURE"], "1")
            self.assertEqual(bridge_env["BRIDGE_REQUIRE_PREPLAY_STOPPED"], "1")
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
            self.assertEqual(bridge_env["STEP5D_QDOT_LIMIT_RAD_S"], "0.150")
            self.assertEqual(bridge_env["STEP5D_QDOT_SLEW_RAD_S2"], "0.000")
            self.assertEqual(bridge_env["STEP5D_EPSILON"], "0.010")
            self.assertEqual(bridge_env["STEP5D_SIGR_EXPONENT_R"], "0.800")
            self.assertEqual(bridge_env["STEP5D_RNN_INNER_ITERATIONS"], "1024")
            self.assertEqual(bridge_env["STEP5D_RNN_BACKEND"], "cupy")
            self.assertEqual(bridge_env["STEP5D_PRELOAD_FILTERED_MAX_N"], "2.0")
            verifier_args = (sandbox / "verifier_argv.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(verifier_args, [str(run_dir), "--output", str(run_dir / "step5d_no_contact_p0_summary.json")])
            self.assertTrue((run_dir / "step5d_no_contact_p0_summary.json").exists())
            self.assertNotIn("bridge output:", completed.stdout)
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
                f"refusing no-contact P0: current capture profile is step5d_strict_rnn_no_contact_p0_v2, expected {P0_V7_STAGE_ID}",
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
                f"runs/controller_readback_{P0_V7_STAGE_ID}_LOCAL_PENDING_READBACK/missing_manifest.json"
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
                f"refusing no-contact P0: manifest missing: runs/controller_readback_{P0_V7_STAGE_ID}_LOCAL_PENDING_READBACK/missing_manifest.json",
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

    def test_portable_failed_v3_projection_is_rejected_with_no_speedj_rnn_live_rows(self) -> None:
        # The original v3 run is retained outside sparse worktrees.  This
        # tracked projection preserves the decisive failure field so the
        # regression remains portable and deterministic.
        run_dir = ROOT / "tests" / "fixtures" / "p0_v3_no_speedj"
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
            self.assertIn("[caps] legacy_total_linear_debug=0.004m/s angular=0.015rad/s hard_force=2/5N torque=3Nm", completed.stdout)
            self.assertIn("[tuning] preload filtered=0..2N raw=0..2N force_norm<=5N hold=0s", completed.stdout)


if __name__ == "__main__":
    unittest.main()
