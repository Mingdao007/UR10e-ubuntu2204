#!/usr/bin/env python3
"""Deterministic timing acceptance tests for the v30 offline harness."""

from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import run_step5d_v30_remote_timing as remote_timing  # noqa: E402
import build_step5d_v30_remote_timing_bundle as timing_bundle  # noqa: E402
import step5c_strict_rnn as strict_rnn  # noqa: E402
from step5d_paper_outer_loop import (  # noqa: E402
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
)
from step5d_v30_timing import (  # noqa: E402
    SOURCE_BINDING_FILES,
    TimingThresholds,
    summarize_preaggregated,
    summarize_timing,
)


class Step5dV30TimingTest(unittest.TestCase):
    def test_synthetic_raw_arrays_are_diagnostic_without_provenance(self) -> None:
        summary = summarize_timing(
            solver_ms=[1.2] * 9_999 + [1.7],
            tick_ms=[1.7] * 30_000,
            safe_hold_ms=[0.08] * 30_000,
            first_post_warm_ms=1.7,
            thresholds=TimingThresholds(),
        )

        self.assertFalse(summary["overall_pass"])
        self.assertFalse(summary["acceptance_eligible"])
        self.assertEqual(summary["classification"], "diagnostic_only_not_acceptance")
        self.assertIn(
            "raw_array_timing_missing_acceptance_provenance", summary["blockers"]
        )
        self.assertEqual(summary["solver"]["deadline_miss_count"], 0)
        self.assertEqual(summary["full_tick"]["deadline_miss_count"], 0)
        self.assertEqual(summary["safe_hold"]["deadline_miss_count"], 0)

    def test_any_two_ms_sample_or_nonfinite_blocks_acceptance(self) -> None:
        summary = summarize_timing(
            solver_ms=[1.0, 2.0],
            tick_ms=[1.0, float("nan")],
            safe_hold_ms=[2.01],
            first_post_warm_ms=1.0,
        )

        self.assertFalse(summary["overall_pass"])
        self.assertGreaterEqual(len(summary["blockers"]), 3)
        self.assertEqual(summary["solver"]["deadline_miss_count"], 1)
        self.assertEqual(summary["full_tick"]["nonfinite_count"], 1)
        self.assertEqual(summary["safe_hold"]["deadline_miss_count"], 1)

    def test_raw_array_cli_is_diagnostic_without_provenance(self) -> None:
        payload = {
            "solver_ms": [1.0] * 10_000,
            "tick_ms": [1.0] * 30_000,
            "safe_hold_ms": [0.1] * 30_000,
            "first_post_warm_ms": 1.0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw-arrays.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "step5d_v30_timing.py"), str(path)],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 3)
        result = json.loads(completed.stdout)
        self.assertFalse(result["acceptance_eligible"])
        self.assertIn(
            "raw_array_timing_missing_acceptance_provenance", result["blockers"]
        )

    def test_remote_harness_is_stdout_only_and_has_no_robot_transport_import(self) -> None:
        source = inspect.getsource(remote_timing)

        self.assertIn("--solver-samples", source)
        self.assertIn("--tick-samples", source)
        self.assertIn("--pace-500hz", source)
        self.assertIn("prepare_rows(rows)", source)
        self.assertIn("gc.disable()", source)
        self.assertIn("wait_until(release)", source)
        self.assertGreaterEqual(source.count("wait_until(release)"), 2)
        self.assertIn("safe_hold_schedule_start = time.perf_counter()", source)
        self.assertIn('"safe_hold_schedule_deadline_miss_count"', source)
        self.assertIn('"elapsed_safe_hold_wall_s"', source)
        self.assertIn('include_diagnostics="compact"', source)
        self.assertIn('"full_tick_control_diagnostics"', source)
        self.assertIn('"accepted_count"', source)
        self.assertIn('"deadline_miss_diagnostics"', source)
        self.assertIn('"max_consecutive"', source)
        self.assertIn("policy.compute(observation)", source)
        self.assertIn("step5d_v30_contract_pipeline(", source)
        self.assertIn("step5d_tcp_jacobian_base(model_bundle, q, tcp_offset)", source)
        self.assertIn("print(json.dumps(payload", source)
        for forbidden in ("RTDEClient", "dashboard_exchange", "socket.connect", "subprocess", "write_text", "write_bytes"):
            self.assertNotIn(forbidden, source)

    def test_cupy_staging_is_fixed_and_page_locked(self) -> None:
        source = inspect.getsource(strict_rnn.StrictTaseRnnSolver._init_cupy_backend)
        kernel = inspect.getsource(strict_rnn.StrictTaseRnnSolver._cupy_solve_kernel)
        solve = inspect.getsource(strict_rnn.StrictTaseRnnSolver._solve_cupy)
        equivalence = inspect.getsource(
            strict_rnn.StrictTaseRnnSolver.validate_cupy_parallel_equivalence
        )

        self.assertIn("alloc_pinned_memory", source)
        self.assertIn("np.frombuffer", source)
        self.assertIn("Stream(", source)
        self.assertIn("non_blocking=True", source)
        self.assertNotIn("np.empty(54", source)
        self.assertNotIn("np.empty(48", source)
        self.assertIn("threadIdx.x", kernel)
        self.assertGreaterEqual(kernel.count("__syncthreads()"), 4)
        self.assertIn("(6,)", solve)
        self.assertIn("stream=self._cupy_stream", solve)
        self.assertIn("np.array_equal", equivalence)
        self.assertIn("parallel strict-RNN kernel failed", equivalence)
        component = inspect.getsource(strict_rnn.StrictTaseRnnSolver.solve_component_timed)
        cupy_solve = inspect.getsource(strict_rnn.StrictTaseRnnSolver._solve_cupy)
        self.assertIn("diagnostic path is intentionally separate", component)
        self.assertIn("capture_components", cupy_solve)
        self.assertIn("blocking=False", cupy_solve)
        self.assertIn("cuda_kernel_ms", cupy_solve)
        self.assertIn("host_completion_wait_ms", cupy_solve)

    def test_remote_bundle_embeds_local_sources_without_remote_write(self) -> None:
        bundle = timing_bundle.build_bundle()

        for module_name in (
            "contact_semantics",
            "step5c_strict_rnn",
            "step5d_paper_outer_loop",
            "step5d_control_contract",
            "step5d_runtime_interface",
            "step5c_calibrated_kinematics_audit",
            "kunwei_rtde_bridge",
        ):
            self.assertIn(f"_install_v30_module('{module_name}'", bundle)
        self.assertIn("'__v30_source_delivery__': 'stdin_bundle'", bundle)
        self.assertIn("'__v30_auxiliary_source_binding__'", bundle)
        for field in (
            "bundler_sha256",
            "aggregator_sha256",
            "readiness_builder_sha256",
        ):
            self.assertIn(field, SOURCE_BINDING_FILES)
            self.assertIn(field, bundle)
            source_path = ROOT / SOURCE_BINDING_FILES[field]
            self.assertIn(
                hashlib.sha256(source_path.read_bytes()).hexdigest(), bundle
            )
        self.assertNotIn("write_text", inspect.getsource(timing_bundle))
        self.assertNotIn("write_bytes", inspect.getsource(timing_bundle))

    def test_component_outliers_are_aggregated_without_discard(self) -> None:
        source = inspect.getsource(remote_timing)

        self.assertIn("component_outlier_values = np.empty", source)
        self.assertIn('"all_samples_retained_in_aggregates": True', source)
        self.assertIn('"outlier_samples_discarded_from_aggregates": False', source)

    def test_compact_outer_loop_is_control_equivalent(self) -> None:
        config = Step5dOuterLoopConfig(
            kp=4.0,
            ko=0.5,
            kf=1.0,
            Md_scalar=240.0,
            Bd_scalar=11_000.0,
            force_target_n=12.0,
            delay_T_s=0.002,
        )
        state = Step5dOuterLoopState()
        inputs = Step5dOuterLoopInputs(
            tcp_pose_base=(0.4, -0.1, 0.3, 3.141592653589793, 0.0, 0.0),
            tcp_speed_base=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            force_tcp_n=(0.0, 0.0, 8.0),
            x_pd_base=(0.401, -0.1, 0.3),
            xdot_pd_base=(0.001, 0.0, 0.0),
            dt_s=0.002,
            control_reaction_normal_base=(0.0, 0.0, -1.0),
        )

        full = compute_step5d_outer_loop(config, state, inputs)
        compact = compute_step5d_outer_loop(config, state, inputs, include_diagnostics=False)

        self.assertEqual(compact.xdot_c, full.xdot_c)
        self.assertEqual(compact.next_state, full.next_state)
        self.assertEqual(compact.cmd_valid, full.cmd_valid)
        self.assertEqual(compact.diagnostics, {})

    def test_compact_remote_payload_is_accepted_without_raw_samples(self) -> None:
        payload = {
            "schema_version": "step5d_v30_remote_timing_raw_v1",
            "source_binding": {
                "delivery": "stdin_bundle",
                **{field: "1" * 64 for field in SOURCE_BINDING_FILES},
            },
            "artifact_binding": {
                name: {"sha256": "2" * 64}
                for name in (
                    "replay_csv",
                    "paper_truth",
                    "stage_table",
                    "calibration_yaml",
                    "ur_xacro",
                )
            },
            "profile": {
                "backend": "cupy",
                "inner_iterations": 128,
                "epsilon": 0.01,
                "sigr_exponent_r": 0.8,
                "qdot_cap_rad_s": 0.05,
                "control_hz": 500.0,
            },
            "precompile_outside_control_loop": True,
            "cupy_host_staging_pinned": True,
            "cupy_dedicated_stream": True,
            "cupy_stream_priority": -1,
            "cupy_parallel_equivalence": {
                "samples": 100,
                "bitwise_equal": True,
                "max_abs_difference": 0.0,
                "parallel_block_threads": 6,
            },
            "cupy_precompile_ms": 430.0,
            "model_prepare_ms": 20.0,
            "first_post_warm_ms": 1.7,
            "solver": {"samples": 10000, "nonfinite_count": 0, "mean_ms": 1.2, "p95_ms": 1.3, "p99_ms": 1.4, "max_ms": 1.7, "compute_deadline_miss_count": 0},
            "full_tick": {"samples": 30000, "nonfinite_count": 0, "mean_ms": 1.5, "p95_ms": 1.6, "p99_ms": 1.7, "max_ms": 1.9, "compute_deadline_miss_count": 0},
            "safe_hold": {"samples": 30000, "nonfinite_count": 0, "mean_ms": 0.05, "p95_ms": 0.06, "p99_ms": 0.07, "max_ms": 0.1, "compute_deadline_miss_count": 0},
            "full_tick_schedule_deadline_miss_count": 0,
            "full_tick_schedule_max_lateness_ms": 0.0,
            "safe_hold_schedule_deadline_miss_count": 0,
            "safe_hold_schedule_max_lateness_ms": 0.0,
            "paced_500hz": True,
            "pacing_provenance": {
                "clock": "time.perf_counter",
                "control_hz": 500.0,
                "period_s": 0.002,
                "full_tick_release_policy": "absolute",
                "safe_hold_release_policy": "independent_absolute",
            },
            "elapsed_full_tick_wall_s": 60.0,
            "elapsed_safe_hold_wall_s": 60.0,
            "full_tick_reason_counts": {"ok": 30000},
            "safe_hold_reason_counts": {"outer_approach_not_pressing": 30000},
            "deadline_miss_diagnostics": {
                "solver_compute": {"total": 0, "retained_indices": [], "overflowed": False},
                "full_tick_compute": {"total": 0, "retained_indices": [], "overflowed": False, "max_consecutive": 0},
                "full_tick_schedule": {"total": 0, "retained_indices": [], "overflowed": False, "max_consecutive": 0},
                "safe_hold_compute": {"total": 0, "retained_indices": [], "overflowed": False, "max_consecutive": 0},
                "safe_hold_schedule": {"total": 0, "retained_indices": [], "overflowed": False, "max_consecutive": 0},
            },
            "runtime_path_source": "kunwei_rtde_bridge.step5d_v30_contract_pipeline",
            "full_tick_deferred_diagnostics": {"count": 30000, "overflowed": False},
            "safe_hold_deferred_diagnostics": {"count": 30000, "overflowed": False},
            "safety_boundary": ["no bridge start"],
        }

        result = summarize_preaggregated(
            payload,
            expected_source_binding={field: "1" * 64 for field in SOURCE_BINDING_FILES},
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )

        self.assertTrue(result["overall_pass"])
        self.assertEqual(result["solver"]["samples"], 10000)
        self.assertEqual(result["full_tick"]["deadline_miss_count"], 0)

        payload["full_tick"]["compute_deadline_miss_count"] = 5
        payload["full_tick"]["max_ms"] = 2.16
        payload["full_tick_schedule_deadline_miss_count"] = 5
        payload["full_tick_schedule_max_lateness_ms"] = 0.17
        payload["deadline_miss_diagnostics"]["full_tick_compute"].update(
            {"total": 5, "retained_indices": [10, 20, 30, 40, 50], "max_consecutive": 1}
        )
        payload["deadline_miss_diagnostics"]["full_tick_schedule"].update(
            {"total": 5, "retained_indices": [10, 20, 30, 40, 50], "max_consecutive": 1}
        )
        degraded = summarize_preaggregated(
            payload,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertFalse(degraded["overall_pass"])
        self.assertTrue(
            degraded["deadline_robustness"]["timing_degraded_candidate"]
        )
        self.assertFalse(
            degraded["deadline_robustness"]["degraded_fail_closed_pass"]
        )
        payload["controller_stale_hold_fault_evidence"] = {
            "pass": True,
            "stale_tick_command": "exact_zero_qdot_not_consumed",
        }
        absorbed = summarize_preaggregated(
            payload,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertTrue(
            absorbed["deadline_robustness"]["degraded_fail_closed_pass"]
        )
        payload["full_tick"]["compute_deadline_miss_count"] = 7
        payload["full_tick_schedule_deadline_miss_count"] = 7
        payload["deadline_miss_diagnostics"]["full_tick_compute"].update(
            {"total": 7, "retained_indices": [10, 20, 30, 40, 50, 60, 70]}
        )
        payload["deadline_miss_diagnostics"]["full_tick_schedule"].update(
            {"total": 7, "retained_indices": [10, 20, 30, 40, 50, 60, 70]}
        )
        rejected_tail = summarize_preaggregated(
            payload,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertFalse(
            rejected_tail["deadline_robustness"]["timing_degraded_candidate"]
        )

        unbound = summarize_preaggregated(payload)
        self.assertFalse(unbound["overall_pass"])
        self.assertIn(
            "remote_timing_expected_source_binding_missing", unbound["blockers"]
        )

    def test_compact_remote_payload_fails_on_schedule_miss(self) -> None:
        payload = {
            "schema_version": "step5d_v30_remote_timing_raw_v1",
            "source_binding": {
                "delivery": "stdin_bundle",
                **{field: "1" * 64 for field in SOURCE_BINDING_FILES},
            },
            "artifact_binding": {
                name: {"sha256": "2" * 64}
                for name in (
                    "replay_csv",
                    "paper_truth",
                    "stage_table",
                    "calibration_yaml",
                    "ur_xacro",
                )
            },
            "profile": {"backend": "cupy", "inner_iterations": 128, "epsilon": 0.01, "sigr_exponent_r": 0.8, "qdot_cap_rad_s": 0.05, "control_hz": 500.0},
            "precompile_outside_control_loop": True,
            "cupy_host_staging_pinned": True,
            "cupy_dedicated_stream": True,
            "cupy_stream_priority": -1,
            "cupy_parallel_equivalence": {
                "samples": 100,
                "bitwise_equal": True,
                "max_abs_difference": 0.0,
                "parallel_block_threads": 6,
            },
            "first_post_warm_ms": 1.0,
            "solver": {"samples": 10000, "nonfinite_count": 0, "p99_ms": 1.0, "max_ms": 1.1, "compute_deadline_miss_count": 0},
            "full_tick": {"samples": 30000, "nonfinite_count": 0, "p99_ms": 1.0, "max_ms": 1.1, "compute_deadline_miss_count": 0},
            "safe_hold": {"samples": 30000, "nonfinite_count": 0, "p99_ms": 0.1, "max_ms": 0.2, "compute_deadline_miss_count": 0},
            "full_tick_schedule_deadline_miss_count": 1,
            "full_tick_schedule_max_lateness_ms": 0.1,
            "safe_hold_schedule_deadline_miss_count": 0,
            "safe_hold_schedule_max_lateness_ms": 0.0,
            "full_tick_reason_counts": {"ok": 30000},
            "safe_hold_reason_counts": {"outer_approach_not_pressing": 30000},
            "runtime_path_source": "kunwei_rtde_bridge.step5d_v30_contract_pipeline",
            "full_tick_deferred_diagnostics": {"count": 30000, "overflowed": False},
            "safe_hold_deferred_diagnostics": {"count": 30000, "overflowed": False},
            "paced_500hz": True,
            "pacing_provenance": {
                "clock": "time.perf_counter",
                "control_hz": 500.0,
                "period_s": 0.002,
                "full_tick_release_policy": "absolute",
                "safe_hold_release_policy": "independent_absolute",
            },
            "elapsed_full_tick_wall_s": 60.0,
            "elapsed_safe_hold_wall_s": 60.0,
        }

        result = summarize_preaggregated(payload)

        self.assertFalse(result["overall_pass"])
        self.assertIn("full_tick_schedule_deadline_miss", result["blockers"])

    def test_compact_remote_payload_requires_independent_safe_hold_schedule(self) -> None:
        payload = {
            "schema_version": "step5d_v30_remote_timing_raw_v1",
            "source_binding": {
                "delivery": "stdin_bundle",
                **{field: "1" * 64 for field in SOURCE_BINDING_FILES},
            },
            "artifact_binding": {
                name: {"sha256": "2" * 64}
                for name in (
                    "replay_csv",
                    "paper_truth",
                    "stage_table",
                    "calibration_yaml",
                    "ur_xacro",
                )
            },
            "profile": {
                "backend": "cupy",
                "inner_iterations": 128,
                "epsilon": 0.01,
                "sigr_exponent_r": 0.8,
                "qdot_cap_rad_s": 0.05,
                "control_hz": 500.0,
            },
            "precompile_outside_control_loop": True,
            "cupy_host_staging_pinned": True,
            "cupy_dedicated_stream": True,
            "cupy_parallel_equivalence": {
                "samples": 100,
                "bitwise_equal": True,
                "max_abs_difference": 0.0,
                "parallel_block_threads": 6,
            },
            "first_post_warm_ms": 1.0,
            "solver": {"samples": 10000, "nonfinite_count": 0, "p99_ms": 1.0, "max_ms": 1.1, "compute_deadline_miss_count": 0},
            "full_tick": {"samples": 30000, "nonfinite_count": 0, "p99_ms": 1.0, "max_ms": 1.1, "compute_deadline_miss_count": 0},
            "safe_hold": {"samples": 30000, "nonfinite_count": 0, "p99_ms": 0.1, "max_ms": 0.2, "compute_deadline_miss_count": 0},
            "full_tick_schedule_deadline_miss_count": 0,
            "full_tick_schedule_max_lateness_ms": 0.0,
            "paced_500hz": True,
            "elapsed_full_tick_wall_s": 60.0,
            "full_tick_reason_counts": {"ok": 30000},
            "safe_hold_reason_counts": {"outer_approach_not_pressing": 30000},
            "runtime_path_source": "kunwei_rtde_bridge.step5d_v30_contract_pipeline",
            "full_tick_deferred_diagnostics": {"count": 30000, "overflowed": False},
            "safe_hold_deferred_diagnostics": {"count": 30000, "overflowed": False},
        }

        result = summarize_preaggregated(payload)

        self.assertFalse(result["acceptance_eligible"])
        self.assertIn(
            "safe_hold_schedule_deadline_miss_count_missing", result["blockers"]
        )
        self.assertIn("safe_hold_wall_duration_not_60s", result["blockers"])
        self.assertIn(
            "independent_absolute_500hz_pacing_provenance_missing",
            result["blockers"],
        )


if __name__ == "__main__":
    unittest.main()
