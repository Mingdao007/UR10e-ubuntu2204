#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import run_ur10e_gazebo_v2_same_run as supervisor  # noqa: E402
import ur10e_gazebo_v2_runtime_adapter as runtime  # noqa: E402


class GazeboV2RuntimeAdapterTest(unittest.TestCase):
    def test_supervisor_strips_scm_tokens_without_mutating_source(self) -> None:
        source = {
            "PATH": "/usr/bin",
            "ROS_DOMAIN_ID": "229",
            "IGN_PARTITION": "isolated",
            "GH_TOKEN": "secret-a",
            "GITHUB_PAT": "secret-b",
            "GITLAB_TOKEN": "secret-c",
        }
        sanitized = supervisor.sanitized_environment(source)
        self.assertEqual(sanitized["PATH"], "/usr/bin")
        self.assertEqual(sanitized["ROS_DOMAIN_ID"], "229")
        self.assertFalse(set(supervisor.SCM_SECRET_ENV) & set(sanitized))
        self.assertEqual(source["GH_TOKEN"], "secret-a")

    def test_velocity_controller_inventory_accepts_ros2_bracket_format(self) -> None:
        text = (
            "joint_state_broadcaster[joint_state_broadcaster/JointStateBroadcaster] active\n"
            "gazebo_v2_velocity_controller[velocity_controllers/JointGroupVelocityController] active\n"
            "gazebo_v2_effort_surrogate_controller[effort_controllers/JointGroupEffortController] inactive\n"
        )
        self.assertTrue(supervisor.velocity_controller_inventory_pass(text))
        self.assertFalse(
            supervisor.velocity_controller_inventory_pass(
                text.replace(
                    "effort_controllers/JointGroupEffortController] inactive",
                    "effort_controllers/JointGroupEffortController] active",
                )
            )
        )

    def test_direct_server_spec_is_supervisor_owned_and_token_sanitized(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gazebo_v2_prefixes_") as tmp:
            root = Path(tmp)
            prefixes = {
                "ur10e_example_controllers": root / "own",
                "ur_description": root / "description",
                "ign_ros2_control": root / "control",
            }
            world = (
                prefixes["ur10e_example_controllers"]
                / "share"
                / "ur10e_example_controllers"
                / "worlds"
                / "ur10e_gazebo_v2_fortress.sdf"
            )
            world.parent.mkdir(parents=True)
            world.write_text("<sdf version='1.9'/>", encoding="utf-8")
            prefixes["ur_description"].mkdir(parents=True)
            prefixes["ign_ros2_control"].mkdir(parents=True)
            command, env, binding = supervisor.build_gazebo_server_spec(
                {"GH_TOKEN": "secret", "IGN_PARTITION": "run-a"},
                package_prefixes=prefixes,
            )
            self.assertEqual(command, ["ign", "gazebo", str(world.resolve()), "-r", "-s", "--headless-rendering"])
            self.assertNotIn("GH_TOKEN", env)
            self.assertIn(str(prefixes["ign_ros2_control"] / "lib"), env["IGN_GAZEBO_SYSTEM_PLUGIN_PATH"])
            self.assertTrue(binding["server_started_directly_by_supervisor"])

    @unittest.skipUnless(hasattr(os, "killpg"), "POSIX process groups required")
    def test_terminate_verifies_group_after_leader_exits(self) -> None:
        script = """
import os
import signal
import time

ready_read, ready_write = os.pipe()
child = os.fork()
if child == 0:
    os.close(ready_read)
    os.setpgid(0, 0)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.write(ready_write, b"1")
    os.close(ready_write)
    time.sleep(30)
else:
    os.close(ready_write)
    os.read(ready_read, 1)
    os.close(ready_read)
    print(child, flush=True)
    def exit_on_interrupt(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGINT, exit_on_interrupt)
    time.sleep(30)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.assertIsNotNone(process.stdout)
        child_pid = int(process.stdout.readline().strip())
        try:
            residuals = supervisor._terminate(process, timeout_s=0.2)
            self.assertEqual(residuals, {})
            self.assertNotIn(child_pid, supervisor._live_process_group_members(process.pid))
            self.assertEqual(supervisor._live_owned_session(process.pid), {})
        finally:
            process.stdout.close()
            for process_group_id in supervisor._live_owned_session(process.pid):
                try:
                    os.killpg(process_group_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_contact_canonicalization_preserves_curved_normal_and_attached_body(self) -> None:
        normal = [0.1, 0.2, math.sqrt(0.95)]
        row = runtime.canonical_contact_row(
            run_id="run-a",
            sim_time_s=1.0,
            collision1="step5_contact_surface::surface::collision",
            collision2="ur10e_gazebo_v2::real_aligned_eoat_visual_stack::eoat_contact_pad_collision",
            body1_wrench=[-value for value in normal] + [0.0, 0.0, 0.0],
            body2_wrench=normal + [0.0, 0.0, 0.0],
            contact_normal_world=normal,
            raw_frame_id="gazebo_world",
        )
        self.assertEqual(row["selected_body_side"], 2)
        self.assertEqual(row["comparison_frame"], "base")
        self.assertEqual(row["comparison_origin"], "eoat_joint_child")
        self.assertNotEqual(row["reaction_normal"], [0.0, 0.0, 1.0])
        self.assertAlmostEqual(sum(value * value for value in row["reaction_normal"]), 1.0)
        self.assertGreater(row["normal_force_projection_n"], 0.0)

    def test_unknown_contact_and_ft_frames_fail_closed(self) -> None:
        with self.assertRaisesRegex(runtime.RuntimeContractError, "contact_wrench_frame_unknown"):
            runtime.canonical_contact_row(
                run_id="run-a",
                sim_time_s=1.0,
                collision1="step5_contact_surface::surface::collision",
                collision2="ur10e_gazebo_v2::real_aligned_eoat_visual_stack::eoat_contact_pad_collision",
                body1_wrench=[0.0] * 6,
                body2_wrench=[0.0, 0.0, 2.0, 0.0, 0.0, 0.0],
                contact_normal_world=[0.0, 0.0, 1.0],
                raw_frame_id="mystery_link",
            )
        with self.assertRaisesRegex(runtime.RuntimeContractError, "ft_wrench_frame_unknown"):
            runtime.canonical_ft_row(
                run_id="run-a",
                sim_time_s=1.0,
                raw_wrench_sensor=[0.0, 0.0, 2.0, 0.0, 0.0, 0.0],
                tare_wrench_sensor=[0.0] * 6,
                rotation_base_from_sensor=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                raw_frame_id="mystery_link",
                reaction_normal_base=[0.0, 0.0, 1.0],
            )

    def test_ft_canonicalization_binds_tare_frame_sign_and_origin(self) -> None:
        row = runtime.canonical_ft_row(
            run_id="run-a",
            sim_time_s=1.0,
            raw_wrench_sensor=[0.0, 0.0, 3.0, 0.0, 0.0, 0.0],
            tare_wrench_sensor=[0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
            rotation_base_from_sensor=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            raw_frame_id="real_aligned_eoat_visual_stack",
            reaction_normal_base=[0.0, 0.0, 1.0],
        )
        self.assertEqual(row["comparison_wrench_on_eoat"], [0.0, 0.0, 2.0, 0.0, 0.0, 0.0])
        self.assertEqual(row["comparison_origin"], "eoat_joint_child")
        self.assertEqual(row["reaction_normal_source"], "same_run_gazebo_contact_message_normal")

    def test_shift_wrench_origin_uses_force_moment_arm(self) -> None:
        shifted = runtime.shift_wrench_origin(
            [1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
        )
        self.assertEqual(shifted, [1.0, 0.0, 0.0, 0.0, 0.0, 1.0])

    def test_runtime_manifest_requires_prewarm_response_and_common_time_window(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gazebo_v2_runtime_manifest_") as tmp:
            run_dir = Path(tmp)
            bindings = runtime.materialize_source_bindings(run_dir)
            ticks = [
                {
                    "run_id": "run-a",
                    "sequence": index,
                    "sim_time_s": index * 0.002,
                }
                for index in range(3)
            ]
            (run_dir / "tick_trace.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in ticks), encoding="utf-8"
            )
            for name in ("native_contact.jsonl", "native_ft.jsonl"):
                (run_dir / name).write_text(
                    "".join(
                        json.dumps({"run_id": "run-a", "sim_time_s": stamp}) + "\n"
                        for stamp in (0.0, 0.002)
                    ),
                    encoding="utf-8",
                )
            (run_dir / "camera_manifest.json").write_text(
                json.dumps(
                    {
                        "run_id": "run-a",
                        "views": {
                            name: {"sim_time_s": 0.002}
                            for name in runtime.CAMERA_VIEWS
                        },
                    }
                ),
                encoding="utf-8",
            )
            payload = {
                "schema": runtime.RUNTIME_SCHEMA,
                "run_id": "run-a",
                "backend": "velocity",
                "status": "complete",
                "profile": {
                    "id": runtime.PROFILE_ID,
                    "backend": "cupy",
                    "inner_iterations": 512,
                    "epsilon": 0.010,
                    "sigr_exponent_r": 0.8,
                    "qdot_cap_rad_s": 0.05,
                    "dls_runtime_fallback_allowed": False,
                },
                "prewarm": {
                    "complete": True,
                    "branch": "unmeasured_execute_path_no_command_publish",
                    "execute_count": runtime.PREWARM_EXECUTE_COUNT,
                    "required_execute_count": runtime.PREWARM_EXECUTE_COUNT,
                    "commands_published": False,
                    "solver_state_reset_after": True,
                },
                "source_bindings": bindings,
                "counters": {
                    "tick_count": 3,
                    "command_publish_count": 3,
                    "command_response_count": 3,
                    "native_ft_row_count": 2,
                    "native_contact_row_count": 2,
                    "camera_view_count": 4,
                    "sequence_gap_count": 0,
                    "period_miss_count": 0,
                    "nonfinite_count": 0,
                    "command_publish_failure_count": 0,
                    "command_response_mismatch_count": 0,
                    "rejected_nonzero_command_count": 0,
                },
                "all_rejected_commands_exact_zero": True,
                "controller_delivery_proven": True,
                "blockers": [],
            }
            self.assertEqual(runtime.validate_runtime_manifest(run_dir, payload, run_id="run-a", backend="velocity"), [])

            payload["prewarm"]["execute_count"] = 1
            payload["counters"]["command_response_count"] = 2
            issues = runtime.validate_runtime_manifest(run_dir, payload, run_id="run-a", backend="velocity")
            self.assertIn("runtime_manifest:prewarm_contract_invalid", issues)
            self.assertIn("runtime_manifest:command_response_count_mismatch", issues)


if __name__ == "__main__":
    unittest.main()
