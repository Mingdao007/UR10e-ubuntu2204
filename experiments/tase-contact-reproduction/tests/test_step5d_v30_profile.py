#!/usr/bin/env python3
"""Offline package/status checks for the inactive v30 candidate."""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_liveprep as build  # noqa: E402
import step5d_runtime_interface as interface  # noqa: E402
import upload_ur_tp_package as upload  # noqa: E402


class Step5dV30ProfileTest(unittest.TestCase):
    def test_v30_is_inactive_and_does_not_replace_current_v29(self) -> None:
        current = json.loads((ROOT / "config" / "current_stage.json").read_text(encoding="utf-8"))
        table = json.loads((ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
        v30 = next(row for row in table["stages"] if row.get("id") == interface.STEP5D_ABLATION_V30_STAGE_ID)

        self.assertEqual(current["program"], interface.STEP5D_ABLATION_V29_STAGE_ID)
        self.assertFalse(v30["active"])
        self.assertTrue(v30["blocked"])
        self.assertFalse(v30["current_binding"]["is_current"])
        self.assertIsNone(v30["package_delivery"]["controller_target"])
        self.assertFalse(v30["package_delivery"]["controller_uploaded"])
        self.assertFalse(v30["package_delivery"]["controller_readback_verified"])
        self.assertFalse(v30["canary_stop_register"]["enabled"])
        self.assertFalse(v30["canary_stop_register"]["armed"])
        self.assertEqual(v30["canary_stop_register"]["phases_s"], [2.0, 10.0, 60.0])
        self.assertEqual(v30["offline_acceptance"]["status"], "v30_offline_blocked")
        self.assertEqual(
            v30["local_analysis_evidence"]["offline_readiness"],
            "config/step5d_v30_offline_readiness.json",
        )

    def test_v30_local_package_is_generated_with_no_delivery_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = build.write_outputs(
                "2026-07-11T0100HKT_STEP5D_STRICT_RNN_ABLATION_V30",
                "2026-07-11T01:00:00+08:00",
                output_dir=Path(tmp),
                program=interface.STEP5D_ABLATION_V30_STAGE_ID,
            )
            script = Path(result["script"]).read_text(encoding="utf-8")
            txt = Path(result["txt"]).read_text(encoding="utf-8")
            marker = json.loads(Path(result["local_candidate_marker"]).read_text(encoding="utf-8"))

        self.assertTrue(result["local_only"])
        self.assertIn("STEP5D_STRICT_RNN_ABLATION_V30", script)
        self.assertIn(
            "# STAGE25_V30_SCAFFOLD: v29_minimal_fix_frame_aware_normal_contract_60s",
            script,
        )
        self.assertIn("v30 is an inactive local-only strict RNN candidate", txt)
        self.assertIn("DLS shadow diagnostics only", script + txt)
        self.assertIn("PURPOSE: v30 contact search", script)
        self.assertNotIn("PURPOSE: v31 contact search", script)
        self.assertIn("forbids DLS runtime fallback", txt)
        self.assertNotIn("remain explicit debug/fallback modes", script + txt)
        self.assertNotIn("explicit fallback/debug mode for v30", script + txt)
        self.assertIn("DEADLINE_OVERRUN_HOLD", script)
        self.assertIn("if not heartbeat_fresh:", script)
        self.assertIn(
            "speedj([0.0, 0.0, 0.0, 0.0, 0.0, 0.0]", script
        )
        self.assertIn("a repeated heartbeat is never consumed", txt)
        self.assertTrue(marker["local_only"])
        self.assertTrue(marker["not_delivered"])
        self.assertIn("no live bridge", marker["safety_boundary"])

    def test_v30_runtime_interface_never_reports_live_bridge_or_upload(self) -> None:
        runtime = interface.resolve_runtime_interface(
            program=interface.STEP5D_ABLATION_V30_STAGE_ID,
            root=ROOT,
            env={},
        )

        lines = interface.live_ready_lines(runtime, {"state": "MISS", "fingerprint_ok": False})
        rendered = "\n".join(lines)

        self.assertEqual(runtime.controller_target, "LOCAL_ONLY_NOT_DELIVERED")
        self.assertTrue(runtime.hard_contract["offline_candidate"])
        self.assertIn("phase=v30-offline-candidate", rendered)
        self.assertIn("upload=no", rendered)
        self.assertNotIn("phase=live-bridge", rendered)

    def test_v30_delivery_is_inactive_preparation_not_current_promotion(self) -> None:
        policy = upload.enforce_offline_candidate_delivery_block(
            interface.STEP5D_ABLATION_V30_STAGE_ID,
            root=ROOT,
        )

        self.assertIsNotNone(policy)
        assert policy is not None
        self.assertEqual(policy["status"], "inactive_prelive_delivery_preparation")
        self.assertFalse(policy["promotion_performed"])
        self.assertFalse(policy["program_start_performed"])
        self.assertFalse(policy["bridge_start_performed"])

    def test_v30_bridge_source_uses_contract_pipeline_but_refuses_live_start(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(encoding="utf-8")
        contract_source = (ROOT / "tools" / "step5d_control_contract.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("from step5d_control_contract import", source)
        self.assertIn("observation_v30 = Step5dObservation(", source)
        self.assertIn("raw_candidate_v30 = state.step5d_v30_policy.compute(", source)
        self.assertIn("step5d_v30_contract_pipeline(", source)
        self.assertIn("shared_step5d_v30_contract_pipeline(", source)
        self.assertIn("dls_shadow = compute_dls_shadow(", contract_source)
        self.assertIn("command = decision_to_register_command(", contract_source)
        self.assertIn("deferred_diagnostics.record(", contract_source)
        self.assertIn("v30 raw bridge is an inactive offline candidate", source)
        self.assertIn("apply_step5d_deadline_overrun_hold(bridge_values)", source)
        overrun_index = source.index("deadline_overrun_hold_active = bool(")
        self.assertLess(
            overrun_index,
            source.index("rtde.send_input_sample(", overrun_index),
        )

    def test_deadline_overrun_hold_clears_motion_without_masking_stop(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "apply_step5d_deadline_overrun_hold"
        )
        namespace = {
            "BRIDGE_INPUT_NAMES": [
                *(f"carrier_{index}" for index in range(6)),
                "step4e_cmd_valid",
            ],
            "STEP5D_STAGE25_JOINT_LAYOUT_CODE": 524.0,
        }
        exec(
            compile(
                ast.Module(body=[function], type_ignores=[]),
                "<deadline_overrun_hold>",
                "exec",
            ),
            namespace,
        )
        values = {
            **{f"carrier_{index}": 0.02 for index in range(6)},
            "step4e_cmd_valid": 0.0,
            "step4e_controller_state": 0.0,
            "heartbeat": 17.0,
            "stop_request": 3.0,
        }

        namespace["apply_step5d_deadline_overrun_hold"](values)

        self.assertEqual(
            [values[f"carrier_{index}"] for index in range(6)],
            [0.0] * 6,
        )
        self.assertEqual(values["step4e_cmd_valid"], 1.0)
        self.assertEqual(
            values["step4e_controller_state"],
            524.0,
        )
        self.assertEqual(values["heartbeat"], 17.0)
        self.assertEqual(values["stop_request"], 3.0)

    def test_v30_publish_history_requires_fresh_successful_rtde_send(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "finalize_step5d_publish_history"
        )
        namespace: dict[str, object] = {}
        exec(
            compile(
                ast.Module(body=[function], type_ignores=[]),
                "<finalize_step5d_publish_history>",
                "exec",
            ),
            namespace,
        )
        finalize = namespace["finalize_step5d_publish_history"]

        class RecordingSolver:
            def __init__(self) -> None:
                self.reset_count = 0

            def reset_state(self) -> None:
                self.reset_count += 1

        class State:
            def __init__(self) -> None:
                self.step5d_solver = RecordingSolver()
                self.step5d_last_qdot = (0.01,) * 6
                self.step5d_pending_solver_warm_start = False

        sent = State()
        self.assertTrue(
            finalize(
                sent,
                v30_contract_profile=True,
                deadline_overrun_hold_active=False,
                rtde_send_succeeded=True,
            )
        )
        self.assertEqual(sent.step5d_solver.reset_count, 0)
        self.assertEqual(sent.step5d_last_qdot, (0.01,) * 6)
        self.assertFalse(sent.step5d_pending_solver_warm_start)

        for label, deadline_overrun, send_succeeded in (
            ("deadline_overrun", True, True),
            ("rtde_send_failure_or_disconnected", False, False),
        ):
            with self.subTest(label=label):
                unpublished = State()
                self.assertFalse(
                    finalize(
                        unpublished,
                        v30_contract_profile=True,
                        deadline_overrun_hold_active=deadline_overrun,
                        rtde_send_succeeded=send_succeeded,
                    )
                )
                self.assertEqual(unpublished.step5d_solver.reset_count, 1)
                self.assertIsNone(unpublished.step5d_last_qdot)
                self.assertTrue(
                    unpublished.step5d_pending_solver_warm_start
                )

        frozen_v29 = State()
        self.assertTrue(
            finalize(
                frozen_v29,
                v30_contract_profile=False,
                deadline_overrun_hold_active=False,
                rtde_send_succeeded=False,
            )
        )
        self.assertEqual(frozen_v29.step5d_solver.reset_count, 0)
        self.assertEqual(frozen_v29.step5d_last_qdot, (0.01,) * 6)
        self.assertFalse(frozen_v29.step5d_pending_solver_warm_start)

    def test_v30_heartbeat_advances_from_publish_history_result(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(
            encoding="utf-8"
        )
        send_index = source.index("rtde.send_input_sample(")
        finalize_index = source.index(
            "fresh_candidate_published = finalize_step5d_publish_history(",
            send_index,
        )
        advance_index = source.index(
            "if fresh_candidate_published:",
            finalize_index,
        )
        heartbeat_index = source.index("heartbeat += 1.0", advance_index)

        self.assertLess(send_index, finalize_index)
        self.assertLess(finalize_index, advance_index)
        self.assertLess(advance_index, heartbeat_index)

    def test_v30_reference_is_governed_before_warm_start_and_policy(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(
            encoding="utf-8"
        )
        observation_index = source.index("observation_v30 = Step5dObservation(")
        governor_index = source.index(
            "observation_v30 = build_slew_compatible_reference(",
            observation_index,
        )
        warm_start_index = source.index(
            "apply_step5d_solver_warm_start_if_pending(",
            governor_index,
        )
        policy_index = source.index(
            "state.step5d_v30_policy.compute(",
            warm_start_index,
        )
        governed_target_index = source.index(
            'xdot_c=np.asarray(target_state["xdot_c"], dtype=float)',
            warm_start_index,
        )

        self.assertLess(observation_index, governor_index)
        self.assertLess(governor_index, warm_start_index)
        self.assertLess(warm_start_index, governed_target_index)
        self.assertLess(governed_target_index, policy_index)


if __name__ == "__main__":
    unittest.main()
