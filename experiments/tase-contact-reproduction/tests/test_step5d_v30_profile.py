#!/usr/bin/env python3
"""Offline package/status checks for the inactive v30 candidate."""

from __future__ import annotations

import ast
import json
import math
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_liveprep as build  # noqa: E402
import kunwei_rtde_bridge as bridge  # noqa: E402
import step5d_runtime_interface as interface  # noqa: E402
import upload_ur_tp_package as upload  # noqa: E402
from step5d_control_contract import RegisterCommand  # noqa: E402


class Step5dV30ProfileTest(unittest.TestCase):
    def test_v30_is_inactive_and_does_not_replace_current_program(self) -> None:
        current = json.loads((ROOT / "config" / "current_stage.json").read_text(encoding="utf-8"))
        table = json.loads((ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
        v30 = next(row for row in table["stages"] if row.get("id") == interface.STEP5D_ABLATION_V30_STAGE_ID)

        self.assertNotEqual(current["program"], interface.STEP5D_ABLATION_V30_STAGE_ID)
        self.assertFalse(v30["active"])
        self.assertTrue(v30["blocked"])
        self.assertFalse(v30["current_binding"]["is_current"])
        delivery = v30["package_delivery"]
        self.assertEqual(delivery["status"], "local_offline_candidate_pending_fresh_readback")
        self.assertEqual(
            delivery["planned_controller_target"],
            "/programs/andyl/kunwei/step5/step5d_strict_rnn_ablation_v30.urp",
        )
        self.assertFalse(delivery["controller_uploaded"])
        self.assertFalse(delivery["controller_readback_verified"])
        self.assertIsNone(delivery["controller_readback_manifest"])
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
        self.assertIn("DEADLINE_OVERRUN_LAST_COMMAND_HOLD", script)
        self.assertIn("if not heartbeat_fresh:", script)
        self.assertIn("local stage25_have_accepted_command = False", script)
        self.assertIn(
            "write_output_float_register(47, stage25_command_consumed)\n            sync()",
            script,
        )
        self.assertIn("reuses only", txt)
        self.assertTrue(marker["local_only"])
        self.assertTrue(marker["not_delivered"])
        self.assertIn("no live bridge", marker["safety_boundary"])

    def test_v30_runtime_interface_uses_real_tp_target_but_remains_inactive(self) -> None:
        runtime = interface.resolve_runtime_interface(
            program=interface.STEP5D_ABLATION_V30_STAGE_ID,
            root=ROOT,
            env={},
        )

        lines = interface.live_ready_lines(
            runtime,
            readiness={"state": "MISS", "fingerprint_ok": False},
        )
        rendered = "\n".join(lines)

        self.assertEqual(
            runtime.controller_target,
            "/programs/andyl/kunwei/step5/step5d_strict_rnn_ablation_v30.urp",
        )
        self.assertTrue(runtime.hard_contract["offline_candidate"])
        self.assertFalse(runtime.hard_contract["controller_readback_verified"])
        self.assertIn("phase=v30-offline-candidate", rendered)
        self.assertIn("upload=required-after-build", rendered)
        self.assertNotIn("phase=live-bridge", rendered)

    def test_v30_completed_delivery_remains_inactive_not_current_promotion(self) -> None:
        policy = upload.enforce_offline_candidate_delivery_block(
            interface.STEP5D_ABLATION_V30_STAGE_ID,
            root=ROOT,
        )

        self.assertIsNone(policy)
        table = json.loads(
            (ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8")
        )
        v30 = next(
            row
            for row in table["stages"]
            if row.get("id") == interface.STEP5D_ABLATION_V30_STAGE_ID
        )
        self.assertEqual(
            v30["package_delivery"]["status"],
            "local_offline_candidate_pending_fresh_readback",
        )
        self.assertFalse(v30["promotion_gate"]["current_promotion_allowed"])
        self.assertFalse(v30["promotion_gate"]["bridge_start_allowed"])
        self.assertFalse(v30["promotion_gate"]["contact_run_allowed"])

    def test_v30_bridge_source_uses_contract_pipeline_but_refuses_live_start(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(encoding="utf-8")
        contract_source = (ROOT / "tools" / "step5d_control_contract.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("from step5d_control_contract import", source)
        self.assertIn("observation_v30 = Step5dObservation(", source)
        self.assertIn("control_step_v30 = step5d_v30_bridge_control_step(", source)
        self.assertIn("shared_step5d_v30_control_step(", source)
        self.assertIn("step5d_v30_contract_pipeline(", source)
        self.assertIn("shared_step5d_v30_contract_pipeline(", source)
        self.assertIn("dls_shadow = compute_dls_shadow(", contract_source)
        self.assertIn("command = decision_to_register_command(", contract_source)
        self.assertIn("deferred_diagnostics.record(", contract_source)
        self.assertIn("v30 raw bridge is an inactive offline candidate", source)
        self.assertIn("publish_action = step5d_publish_action(", source)
        self.assertIn(
            "elif deadline_overrun_hold_active:\n                        deadline_overrun_hold_total += 1",
            source,
        )
        overrun_index = source.index("deadline_overrun_detected = bool(")
        self.assertLess(
            overrun_index,
            source.index("rtde.send_input_sample(", overrun_index),
        )
        self.assertIn(
            'if publish_action == "stop":\n                        apply_step5d_explicit_stop_packet',
            source,
        )

    def test_p0_late_accepted_command_publishes_and_only_missing_work_holds(self) -> None:
        accepted = {
            **{name: 0.01 for name in bridge.BRIDGE_INPUT_NAMES[:6]},
            "step4e_cmd_valid": 1.0,
            "step4e_controller_state": 524.0,
        }
        held = dict(accepted)

        for compute_ms in (2.544, 50.858):
            with self.subTest(compute_ms=compute_ms):
                self.assertEqual(
                    bridge.step5d_publish_action(
                        accepted,
                        robot_stage=25.0,
                        v30_contract_profile=True,
                        stop_dominant=False,
                        schedule_late=compute_ms >= 2.0,
                        publish_guard_approved_late_command=True,
                        last_published_command=None,
                    ),
                    "fresh_command",
                )

        rejected = dict(accepted, step4e_cmd_valid=0.0)
        self.assertEqual(
            bridge.step5d_publish_action(
                rejected,
                robot_stage=25.0,
                v30_contract_profile=True,
                stop_dominant=False,
                schedule_late=True,
                publish_guard_approved_late_command=True,
                last_published_command=held,
            ),
            "hold_last",
        )
        self.assertEqual(
            bridge.step5d_publish_action(
                rejected,
                robot_stage=25.0,
                v30_contract_profile=True,
                stop_dominant=False,
                schedule_late=True,
                publish_guard_approved_late_command=True,
                last_published_command=None,
            ),
            "startup_invalid",
        )

    def test_contact_v30_retains_strict_late_candidate_policy(self) -> None:
        accepted = {
            **{name: 0.01 for name in bridge.BRIDGE_INPUT_NAMES[:6]},
            "step4e_cmd_valid": 1.0,
            "step4e_controller_state": 524.0,
        }
        self.assertEqual(
            bridge.step5d_publish_action(
                accepted,
                robot_stage=25.0,
                v30_contract_profile=True,
                stop_dominant=False,
                schedule_late=True,
                publish_guard_approved_late_command=False,
                last_published_command=None,
            ),
            "startup_invalid",
        )
        self.assertEqual(
            bridge.step5d_publish_action(
                accepted,
                robot_stage=25.0,
                v30_contract_profile=True,
                stop_dominant=False,
                schedule_late=True,
                publish_guard_approved_late_command=False,
                last_published_command=accepted,
            ),
            "hold_last",
        )

    def test_stage_aware_transport_preserves_scaffold_before_joint_stage(self) -> None:
        base = {name: 0.0 for name in bridge.BRIDGE_INPUT_NAMES[:6]}
        cases = (
            (25.05, {**base, "step4e_cmd_valid": 1.0, "step4e_controller_state": 33.0}, "scaffold"),
            (25.30, {**base, "step4e_cmd_valid": 1.0, "step4e_controller_state": 521.0}, "preload"),
            (25.95, {**base, "step4e_cmd_valid": 0.0, "step4e_controller_state": 522.0}, "qdot_clear"),
            (25.00, {**base, "step4e_cmd_valid": 1.0, "step4e_controller_state": 524.0}, "fresh_command"),
        )
        for robot_stage, packet, expected_action in cases:
            with self.subTest(robot_stage=robot_stage):
                before = dict(packet)
                action = bridge.step5d_publish_action(
                    packet,
                    robot_stage=robot_stage,
                    v30_contract_profile=True,
                    stop_dominant=False,
                    schedule_late=False,
                    publish_guard_approved_late_command=True,
                    last_published_command=None,
                )
                self.assertEqual(action, expected_action)
                self.assertEqual(packet, before)

        invalid_joint = {**base, "step4e_cmd_valid": 1.0, "step4e_controller_state": 33.0}
        self.assertEqual(
            bridge.step5d_publish_action(
                invalid_joint,
                robot_stage=25.0,
                v30_contract_profile=True,
                stop_dominant=False,
                schedule_late=False,
                publish_guard_approved_late_command=True,
                last_published_command=None,
            ),
            "startup_invalid",
        )
        bridge.apply_step5d_unpublished_startup_packet(invalid_joint)
        self.assertEqual(invalid_joint["step4e_controller_state"], 0.0)
        self.assertEqual(invalid_joint["step4e_cmd_valid"], 0.0)

    def test_event_loop_waits_until_io_or_release_without_busy_spin(self) -> None:
        class FakeSocket:
            def __init__(self, fd: int) -> None:
                self.fd = fd

            def fileno(self) -> int:
                return self.fd

        sensor = FakeSocket(11)
        rtde = type("FakeRtde", (), {"sock": FakeSocket(12)})()
        calls: list[tuple[list[object], float]] = []

        def fake_select(
            readers: list[object], _writers: list[object], _errors: list[object], timeout: float
        ) -> tuple[list[object], list[object], list[object]]:
            calls.append((readers, timeout))
            return [], [], []

        bridge.wait_for_bridge_io_or_deadline(
            sensor,
            rtde,
            next_write_s=10.002,
            now_s=10.0005,
            select_fn=fake_select,
        )
        self.assertEqual(calls[0][0], [sensor, rtde.sock])
        self.assertAlmostEqual(calls[0][1], 0.0015, places=9)

        calls.clear()
        self.assertEqual(
            bridge.wait_for_bridge_io_or_deadline(
                sensor,
                rtde,
                next_write_s=10.0,
                now_s=10.001,
                select_fn=fake_select,
            ),
            0.0,
        )
        self.assertEqual(calls, [])

    def test_deadline_overrun_hold_reuses_last_command_without_masking_stop(self) -> None:
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
            "Mapping": Mapping,
            "STEP5D_HELD_COMMAND_NAMES": (
                *(f"carrier_{index}" for index in range(6)),
                "step4e_cmd_valid",
                "step4e_controller_state",
            ),
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

        held = {
            **{f"carrier_{index}": 0.01 * (index + 1) for index in range(6)},
            "step4e_cmd_valid": 1.0,
            "step4e_controller_state": 524.0,
        }
        namespace["apply_step5d_deadline_overrun_hold"](values, held)

        self.assertEqual(
            [values[f"carrier_{index}"] for index in range(6)],
            [held[f"carrier_{index}"] for index in range(6)],
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
        namespace: dict[str, object] = {
            "Mapping": Mapping,
            "BRIDGE_INPUT_NAMES": tuple(f"carrier_{index}" for index in range(6)),
            "STEP5D_STAGE25_JOINT_LAYOUT_CODE": 524,
        }
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
                self.step5d_v30_sequence = 8

        held_command = {
            **{f"carrier_{index}": 0.02 * (index + 1) for index in range(6)},
            "step4e_cmd_valid": 1.0,
            "step4e_controller_state": 524.0,
        }

        sent = State()
        self.assertTrue(
            finalize(
                sent,
                v30_contract_profile=True,
                deadline_overrun_hold_active=False,
                rtde_send_succeeded=True,
                command_publishable=True,
                last_published_command=held_command,
                last_published_sequence=7,
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
                        command_publishable=not deadline_overrun,
                        last_published_command=held_command,
                        last_published_sequence=7,
                    )
                )
                self.assertEqual(unpublished.step5d_solver.reset_count, 1)
                self.assertEqual(
                    unpublished.step5d_last_qdot,
                    tuple(held_command[f"carrier_{index}"] for index in range(6)),
                )
                self.assertEqual(unpublished.step5d_v30_sequence, 7)
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
                command_publishable=False,
                last_published_command=None,
                last_published_sequence=7,
            )
        )
        self.assertEqual(frozen_v29.step5d_solver.reset_count, 0)
        self.assertEqual(frozen_v29.step5d_last_qdot, (0.01,) * 6)
        self.assertFalse(frozen_v29.step5d_pending_solver_warm_start)

        startup_miss = State()
        self.assertFalse(
            finalize(
                startup_miss,
                v30_contract_profile=True,
                deadline_overrun_hold_active=False,
                rtde_send_succeeded=True,
                command_publishable=False,
                last_published_command=None,
                last_published_sequence=0,
            )
        )
        self.assertIsNone(startup_miss.step5d_last_qdot)
        self.assertEqual(startup_miss.step5d_v30_sequence, 0)

    def test_v30_startup_health_heartbeat_is_precommand_only(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "step5d_startup_health_heartbeat_published"
        )
        namespace = {"Mapping": Mapping}
        exec(
            compile(
                ast.Module(body=[function], type_ignores=[]),
                "<step5d_startup_health_heartbeat_published>",
                "exec",
            ),
            namespace,
        )
        published = namespace["step5d_startup_health_heartbeat_published"]

        self.assertTrue(
            published(
                v30_contract_profile=True,
                rtde_send_succeeded=True,
                command_publishable=False,
                stop_dominant=False,
                last_published_command=None,
            )
        )
        for override in (
            {"v30_contract_profile": False},
            {"rtde_send_succeeded": False},
            {"command_publishable": True},
            {"stop_dominant": True},
            {"last_published_command": {"step4e_cmd_valid": 1.0}},
        ):
            args = {
                "v30_contract_profile": True,
                "rtde_send_succeeded": True,
                "command_publishable": False,
                "stop_dominant": False,
                "last_published_command": None,
            }
            args.update(override)
            with self.subTest(override=override):
                self.assertFalse(published(**args))

    def test_v30_qdot_clear_packet_is_preserved_only_at_layout_522(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "step5d_qdot_clear_packet_publishable"
        )
        namespace = {
            "Mapping": Mapping,
            "STEP5D_QDOT_CLEAR_MODE_CODE": 522.0,
            "STEP5D_QDOT_CLEAR_ZERO_TOL_RAD_S": 0.0005,
            "BRIDGE_INPUT_NAMES": tuple(f"carrier_{index}" for index in range(6)),
        }
        exec(
            compile(
                ast.Module(body=[function], type_ignores=[]),
                "<step5d_qdot_clear_packet_publishable>",
                "exec",
            ),
            namespace,
        )
        publishable = namespace["step5d_qdot_clear_packet_publishable"]
        packet = {
            **{f"carrier_{index}": 0.0 for index in range(6)},
            "step4e_cmd_valid": 0.0,
            "step4e_controller_state": 522.0,
        }
        self.assertTrue(
            publishable(
                packet,
                v30_contract_profile=True,
                stop_dominant=False,
            )
        )
        for override, packet_override in (
            ({"v30_contract_profile": False}, {}),
            ({"stop_dominant": True}, {}),
            ({}, {"step4e_cmd_valid": 1.0}),
            ({}, {"step4e_controller_state": 524.0}),
            ({}, {"carrier_0": 0.001}),
        ):
            args = {
                "v30_contract_profile": True,
                "stop_dominant": False,
            }
            args.update(override)
            candidate = dict(packet)
            candidate.update(packet_override)
            with self.subTest(override=override, packet_override=packet_override):
                self.assertFalse(publishable(candidate, **args))

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
        startup_index = source.index("elif startup_health_published:", heartbeat_index)
        startup_heartbeat_index = source.index("heartbeat += 1.0", startup_index)

        self.assertLess(send_index, finalize_index)
        self.assertLess(finalize_index, advance_index)
        self.assertLess(advance_index, heartbeat_index)
        self.assertLess(heartbeat_index, startup_index)
        self.assertLess(startup_index, startup_heartbeat_index)

    def test_v30_reference_is_governed_before_warm_start_and_policy(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(
            encoding="utf-8"
        )
        contract_source = (ROOT / "tools" / "step5d_control_contract.py").read_text(
            encoding="utf-8"
        )
        observation_index = source.index("observation_v30 = Step5dObservation(")
        control_step_index = source.index(
            "control_step_v30 = step5d_v30_bridge_control_step(",
            observation_index,
        )
        prepare_argument_index = source.index(
            "prepare_policy=prepare_v30_policy",
            control_step_index,
        )
        seam_start = contract_source.index("def step5d_v30_control_step(")
        governor_index = contract_source.index(
            "governed_observation = build_slew_compatible_reference(",
            seam_start,
        )
        prepare_index = contract_source.index(
            "prepare_policy(governed_observation)",
            governor_index,
        )
        policy_index = contract_source.index(
            "policy.compute(governed_observation)",
            prepare_index,
        )

        self.assertLess(observation_index, control_step_index)
        self.assertLess(control_step_index, prepare_argument_index)
        self.assertLess(governor_index, prepare_index)
        self.assertLess(prepare_index, policy_index)

    def test_v30_exception_stop_publish_precedes_break_and_transport_close(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(
            encoding="utf-8"
        )
        compute_index = source.index("step4e_values = compute_bridge_values(")
        publish_index = source.index(
            "publish_step5d_v30_exception_stop(",
            compute_index,
        )
        break_index = source.index("break", publish_index)
        finally_index = source.index("finally:", break_index)
        close_index = source.index("close_rtde_bridge(rtde)", finally_index)

        self.assertLess(compute_index, publish_index)
        self.assertLess(publish_index, break_index)
        self.assertLess(break_index, finally_index)
        self.assertLess(finally_index, close_index)

    def test_exception_stop_packet_is_exact_zero_layout_524_and_sent_first(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        functions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name
            in {
                "build_step5d_v30_exception_stop_packet",
                "publish_step5d_v30_exception_stop",
            }
        ]
        base_names = [
            "normal_force_n",
            "force_norm_n",
            "heartbeat",
            "sensor_ok",
            "stop_request",
            "target_force_n",
            "torque_norm_nm",
            "fx_n_zeroed",
            "fy_n_zeroed",
            "fz_n_zeroed",
            "mx_nm_zeroed",
            "my_nm_zeroed",
            "mz_nm_zeroed",
        ]
        bridge_names = [
            *(f"carrier_{index}" for index in range(6)),
            "step4e_cmd_valid",
            "step4e_progress_m",
            "step4e_force_error_n",
            "step4e_orientation_error_rad",
            "step4e_controller_state",
        ]
        namespace: dict[str, Any] = {
            "Any": Any,
            "RegisterCommand": RegisterCommand,
            "math": math,
            "time": time,
            "STEP5D_STAGE25_JOINT_LAYOUT_CODE": 524.0,
            "INPUT_FIELDS": [
                *(f"input_double_register_{index}" for index in range(24, 37)),
                *(f"input_double_register_{index}" for index in range(37, 48)),
            ],
            "INPUT_NAMES": base_names + bridge_names,
            "BRIDGE_INPUT_NAMES": bridge_names,
            "rtde_error_name": lambda exc: f"{type(exc).__name__}: {exc}",
        }
        exec(
            compile(
                ast.Module(body=functions, type_ignores=[]),
                "<v30_exception_stop>",
                "exec",
            ),
            namespace,
        )
        call_order: list[str] = []

        class RecordingRTDE:
            def send_input_sample(
                self,
                _recipe_id: int,
                _type_names: list[str],
                values: list[float],
            ) -> None:
                call_order.append("send")
                self.values = values

        class InjectedPolicyFailure(Exception):
            pass

        rtde = RecordingRTDE()
        command, packet, event = namespace[
            "publish_step5d_v30_exception_stop"
        ](
            rtde,
            recipe_id=1,
            type_names=["DOUBLE"] * len(namespace["INPUT_NAMES"]),
            heartbeat=17.0,
            original_error=InjectedPolicyFailure("synthetic policy failure"),
        )
        call_order.append("close")

        self.assertEqual(call_order, ["send", "close"])
        self.assertIsNotNone(command)
        self.assertIsNotNone(packet)
        assert command is not None and packet is not None
        self.assertEqual(command.qdot, (0.0,) * 6)
        self.assertFalse(command.cmd_valid)
        self.assertEqual(command.layout_code, 524.0)
        self.assertTrue(command.stop_request)
        self.assertEqual([packet[name] for name in bridge_names[:6]], [0.0] * 6)
        self.assertEqual(packet["step4e_cmd_valid"], 0.0)
        self.assertEqual(packet["step4e_controller_state"], 524.0)
        self.assertEqual(packet["stop_request"], 1.0)
        self.assertTrue(event["stop_publish_succeeded"])
        self.assertIn("InjectedPolicyFailure", event["original_error"])

    def test_exception_stop_publish_failure_records_both_errors(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        functions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name
            in {
                "build_step5d_v30_exception_stop_packet",
                "publish_step5d_v30_exception_stop",
            }
        ]
        input_names = [
            "normal_force_n",
            "force_norm_n",
            "heartbeat",
            "sensor_ok",
            "stop_request",
            "target_force_n",
            "torque_norm_nm",
            "fx_n_zeroed",
            "fy_n_zeroed",
            "fz_n_zeroed",
            "mx_nm_zeroed",
            "my_nm_zeroed",
            "mz_nm_zeroed",
            *(f"carrier_{index}" for index in range(6)),
            "step4e_cmd_valid",
            "step4e_progress_m",
            "step4e_force_error_n",
            "step4e_orientation_error_rad",
            "step4e_controller_state",
        ]
        namespace: dict[str, Any] = {
            "Any": Any,
            "RegisterCommand": RegisterCommand,
            "math": math,
            "time": time,
            "STEP5D_STAGE25_JOINT_LAYOUT_CODE": 524.0,
            "INPUT_FIELDS": [f"input_double_register_{index}" for index in range(24, 48)],
            "INPUT_NAMES": input_names,
            "BRIDGE_INPUT_NAMES": input_names[13:],
            "rtde_error_name": lambda exc: f"{type(exc).__name__}: {exc}",
        }
        exec(
            compile(
                ast.Module(body=functions, type_ignores=[]),
                "<v30_exception_stop_failure>",
                "exec",
            ),
            namespace,
        )

        class FailingRTDE:
            def send_input_sample(self, *_args: object) -> None:
                raise OSError("synthetic stop transport failure")

        command, packet, event = namespace[
            "publish_step5d_v30_exception_stop"
        ](
            FailingRTDE(),
            recipe_id=1,
            type_names=["DOUBLE"] * len(input_names),
            heartbeat=3.0,
            original_error=RuntimeError("synthetic reference failure"),
        )

        self.assertIsNotNone(command)
        self.assertIsNotNone(packet)
        self.assertFalse(event["stop_publish_succeeded"])
        self.assertIn("RuntimeError: synthetic reference failure", event["original_error"])
        self.assertIn("OSError: synthetic stop transport failure", event["stop_publish_error"])


if __name__ == "__main__":
    unittest.main()
