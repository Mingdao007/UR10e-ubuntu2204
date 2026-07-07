#!/usr/bin/env python3
"""Focused tests for the Step5d Stage25 ablation routes."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_liveprep as liveprep  # noqa: E402
import kunwei_rtde_bridge as bridge  # noqa: E402
import step5d_runtime_interface as iface  # noqa: E402
from step5d_paper_outer_loop import Step5dOuterLoopState  # noqa: E402


def ablation_args(program: str, mode: str) -> object:
    return bridge.parse_args(
        [
            "--no-start-command",
            "--skip-dashboard-preflight",
            "--step4e-mode",
            "line",
            "--step4e-version",
            program,
            "--step4e-path-shape",
            "cycloid",
            "--step5d-stage25-control-mode",
            mode,
        ]
    )


def v25_args(mode: str) -> object:
    return ablation_args(iface.STEP5D_ABLATION_V25_STAGE_ID, mode)


def acquired_state() -> bridge.BridgeState:
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


def fake_runtime(state: bridge.BridgeState, _args: object) -> None:
    state.step5d_model_bundle = SimpleNamespace(
        model=SimpleNamespace(
            lowerPositionLimit=np.full(6, -math.pi),
            upperPositionLimit=np.full(6, math.pi),
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


def fake_outer(*_args: object, **_kwargs: object) -> SimpleNamespace:
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


def compute_ablation_values(program: str, mode: str, *, jacobian: np.ndarray | None = None) -> dict[str, object]:
    args = ablation_args(program, mode)
    state = acquired_state()
    fake_runtime(state, args)
    latest_output = {
        "actual_TCP_pose": [0.49, 0.14, 0.02, math.pi, 0.0, 0.0],
        "actual_TCP_speed": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "actual_q": [0.0] * 6,
        "actual_qd": [0.0] * 6,
        "output_double_register_35": 25.0,
    }
    with (
        patch.object(
            bridge,
            "ensure_step5d_liveprep_runtime",
            side_effect=AssertionError("hot path must use prewarmed Step5d runtime"),
        ),
        patch.object(bridge, "step5d_tcp_jacobian_base", return_value=np.eye(6) if jacobian is None else jacobian),
        patch.object(bridge, "step5d_omega_bounds", return_value=(np.full(6, -0.05), np.full(6, 0.05))),
        patch.object(bridge, "compute_step5d_outer_loop", side_effect=fake_outer),
        patch.object(bridge, "rnn_target_state_from_outer_loop", return_value={"shadow": True}),
    ):
        return bridge.compute_bridge_values(
            args,
            [0.0, 0.0, -12.0, 0.0, 0.0, 0.0],
            latest_output,
            1.0,
            state,
            0.002,
        )


def compute_v25_values(mode: str) -> dict[str, object]:
    return compute_ablation_values(iface.STEP5D_ABLATION_V25_STAGE_ID, mode)


class Step5dV25AblationTest(unittest.TestCase):
    def test_builder_generates_v25_multimode_stage25_layout(self) -> None:
        stamp = "2026-07-03T1000HKT_STEP5D_STRICT_RNN_ABLATION_V25"
        spec = liveprep.spec_for(iface.STEP5D_ABLATION_V25_STAGE_ID)
        geom = liveprep.line_cfg(liveprep.load_json(liveprep.CONFIG_PATH))
        frame = liveprep.load_safe_frame(spec)
        script = liveprep.build_script(stamp, "2026-07-03T10:00:00+08:00", geom, frame, spec)
        txt = liveprep.build_txt(stamp, spec)
        urp = liveprep.build_urp(script, spec.program_name, liveprep.CONTROLLER_DIR)

        self.assertEqual(spec.program_name, "step5d_strict_rnn_ablation_v25")
        liveprep.validate_package(script, txt, urp, stamp, spec)
        self.assertIn("local stage25_layout_tag = read_input_float_register(47)", script)
        self.assertIn("local cartesian_angular_cap_rad_s = 0.150", script)
        self.assertIn("speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy, cmd_wz]", script)
        self.assertIn("speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5]", script)
        self.assertIn("Stage 25.0 supports Cartesian speedl layout", txt)
        self.assertIn("filtered normal_load between 10.5 N and 12.8 N", txt)
        self.assertIn("raw normal_load is sanity-checked between 9.5 N and 13.5 N", txt)
        self.assertIn("Stage 25.95 requires the bridge to clear registers 37..47", txt)
        self.assertNotIn("step5d_strict_rnn_liveprep_v24", script + txt)

    def test_builder_generates_v26_strict_rnn_default_with_small_angular_cap(self) -> None:
        stamp = "2026-07-03T1000HKT_STEP5D_STRICT_RNN_ABLATION_V26"
        spec = liveprep.spec_for(iface.STEP5D_ABLATION_V26_STAGE_ID)
        geom = liveprep.line_cfg(liveprep.load_json(liveprep.CONFIG_PATH))
        frame = liveprep.load_safe_frame(spec)
        script = liveprep.build_script(stamp, "2026-07-03T10:00:00+08:00", geom, frame, spec)
        txt = liveprep.build_txt(stamp, spec)
        urp = liveprep.build_urp(script, spec.program_name, liveprep.CONTROLLER_DIR)

        self.assertEqual(spec.program_name, "step5d_strict_rnn_ablation_v26")
        liveprep.validate_package(script, txt, urp, stamp, spec)
        self.assertIn("local cartesian_angular_cap_rad_s = 0.015", script)
        self.assertIn("v26 first live mode defaults to speedl_cartesian_oracle", txt)
        self.assertIn("joint-feasibility-scaled", txt)
        self.assertIn("filtered normal_load between 7.0 N and 18.0 N", txt)
        self.assertIn("raw normal_load is sanity-checked between 5.0 N and 20.0 N", txt)
        self.assertIn("local line_entry_default_normal_load_min_n = 7.000", script)
        self.assertIn("local line_entry_default_normal_load_max_n = 18.000", script)
        self.assertIn("local line_entry_recovery_normal_load_max_n = 24.000", script)
        self.assertNotIn("step5d_strict_rnn_ablation_v25", script + txt)

    def test_runtime_interface_defaults_to_speedl_cartesian_oracle_for_v25(self) -> None:
        runtime = iface.resolve_runtime_interface(
            program=iface.STEP5D_ABLATION_V25_STAGE_ID,
            root=ROOT,
            env={},
        )

        self.assertEqual(runtime.program, "step5d_strict_rnn_ablation_v25")
        self.assertEqual(runtime.stage25_control_mode, "speedl_cartesian_oracle")
        self.assertEqual(runtime.preload_gate.filtered_min_n, 10.5)
        self.assertEqual(runtime.preload_gate.filtered_max_n, 12.8)
        self.assertEqual(runtime.preload_gate.raw_min_n, 9.5)
        self.assertEqual(runtime.preload_gate.raw_max_n, 13.5)
        self.assertIn("cartesian", runtime.register_contract["stage25_0"])
        self.assertIn("joint", runtime.register_contract["stage25_0"])

    def test_runtime_interface_defaults_to_speedl_cartesian_oracle_for_v26(self) -> None:
        runtime = iface.resolve_runtime_interface(
            program=iface.STEP5D_ABLATION_V26_STAGE_ID,
            root=ROOT,
            env={},
        )

        self.assertEqual(runtime.program, "step5d_strict_rnn_ablation_v26")
        self.assertEqual(runtime.stage25_control_mode, "speedl_cartesian_oracle")
        self.assertEqual(runtime.bridge_defaults.angular_limit_rad_s, 0.015)
        self.assertEqual(runtime.preload_gate.filtered_min_n, 7.0)
        self.assertEqual(runtime.preload_gate.filtered_max_n, 18.0)
        self.assertEqual(runtime.preload_gate.raw_min_n, 5.0)
        self.assertEqual(runtime.preload_gate.raw_max_n, 20.0)
        self.assertEqual(runtime.preload_gate.recovery_normal_load_max_n, 24.0)
        self.assertIn("v25/v26", runtime.register_contract["stage25_0"])

    def test_speedl_cartesian_oracle_writes_cartesian_layout_and_keeps_rnn_shadow(self) -> None:
        values = compute_v25_values("speedl_cartesian_oracle")

        self.assertEqual(values["step4e_controller_state"], bridge.STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE)
        self.assertEqual(values["step4e_cmd_vx_m_s"], 0.0010)
        self.assertEqual(values["step4e_cmd_vy_m_s"], 0.0015)
        self.assertEqual(values["step4e_cmd_vz_m_s"], -0.0020)
        self.assertEqual(values["step4e_cmd_wx_rad_s"], 0.0100)
        self.assertEqual(values["step4e_cmd_wy_rad_s"], -0.0200)
        self.assertEqual(values["step4e_cmd_wz_rad_s"], 0.0300)
        self.assertEqual(values["_step5d_stage25_control_mode"], "speedl_cartesian_oracle")
        self.assertEqual(values["_step5d_rnn_raw_qd0_rad_s"], 0.020)
        self.assertEqual(values["_step5d_qdot_max_abs_rad_s"], 0.020)
        self.assertNotEqual(values["step4e_cmd_vx_m_s"], values["_step5d_rnn_raw_qd0_rad_s"])

    def test_joint_modes_write_joint_layout(self) -> None:
        dls = compute_v25_values("speedj_dls_oracle")
        rnn = compute_v25_values("speedj_rnn_live")

        self.assertEqual(dls["step4e_controller_state"], bridge.STEP5D_STAGE25_JOINT_LAYOUT_CODE)
        self.assertEqual(rnn["step4e_controller_state"], bridge.STEP5D_STAGE25_JOINT_LAYOUT_CODE)
        self.assertEqual(rnn["step4e_cmd_vx_m_s"], 0.020)
        self.assertEqual(rnn["step4e_cmd_wz_rad_s"], 0.002)
        self.assertNotEqual(dls["step4e_cmd_vx_m_s"], 0.0010)
        self.assertEqual(dls["_step5d_stage25_control_mode"], "speedj_dls_oracle")
        self.assertEqual(rnn["_step5d_stage25_control_mode"], "speedj_rnn_live")

    def test_v26_speedj_rnn_live_scales_xdot_for_joint_feasibility(self) -> None:
        values = compute_ablation_values(
            iface.STEP5D_ABLATION_V26_STAGE_ID,
            "speedj_rnn_live",
            jacobian=np.eye(6) * 0.1,
        )

        self.assertEqual(values["step4e_controller_state"], bridge.STEP5D_STAGE25_JOINT_LAYOUT_CODE)
        self.assertEqual(values["_step5d_stage25_control_mode"], "speedj_rnn_live")
        self.assertEqual(values["_step5d_qdot_cap_rad_s"], 0.05)
        self.assertEqual(values["_step5d_jinv_xdot_solve_status"], "ok")
        self.assertGreater(values["_step5d_jinv_xdot_inf_over_qdot_cap"], 1.0)
        self.assertLess(values["_step5d_xdot_feasibility_scale"], 1.0)
        self.assertEqual(values["_step5d_xdot_feasibility_scale_active"], 1.0)
        self.assertEqual(values["_step5d_lambda_state_0"], 3.0)

    def test_joint_feasibility_scaler_restores_qdot_margin(self) -> None:
        xdot = np.array([0.0, 0.0, 0.0, 0.015, 0.0, 0.0])
        scaled, diag = bridge.scale_step5d_xdot_for_joint_feasibility(
            xdot,
            np.eye(6) * 0.1,
            qdot_cap_rad_s=0.05,
        )

        self.assertAlmostEqual(diag["jinv_xdot_inf_rad_s"], 0.15, places=12)
        self.assertAlmostEqual(diag["jinv_xdot_inf_over_qdot_cap"], 3.0, places=12)
        self.assertAlmostEqual(diag["xdot_feasibility_scale"], 0.3, places=12)
        self.assertTrue(diag["xdot_feasibility_scale_active"])
        np.testing.assert_allclose(scaled, xdot * 0.3, atol=1e-12)


if __name__ == "__main__":
    unittest.main()
