#!/usr/bin/env python3
"""Deterministic timing acceptance tests for the v30 offline harness."""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np


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
    def test_inner_iteration_override_is_explicitly_diagnostic_and_bound(self) -> None:
        canonical = remote_timing.build_profile_selection(None)
        explicit_canonical = remote_timing.build_profile_selection(512)
        candidate = remote_timing.build_profile_selection(256)

        self.assertTrue(canonical["acceptance_profile_eligible"])
        self.assertFalse(canonical["diagnostic_override_requested"])
        self.assertEqual(canonical["effective_profile"], remote_timing.PROFILE)
        self.assertFalse(explicit_canonical["acceptance_profile_eligible"])
        self.assertTrue(explicit_canonical["diagnostic_override_requested"])
        self.assertEqual(
            explicit_canonical["effective_profile_sha256"],
            canonical["effective_profile_sha256"],
        )
        self.assertNotEqual(
            explicit_canonical["selection_sha256"],
            canonical["selection_sha256"],
        )
        self.assertEqual(candidate["effective_profile"]["inner_iterations"], 256)
        self.assertEqual(candidate["effective_profile"]["epsilon"], 0.010)
        self.assertEqual(candidate["effective_profile"]["sigr_exponent_r"], 0.8)
        self.assertEqual(candidate["effective_profile"]["qdot_cap_rad_s"], 0.05)
        self.assertEqual(candidate["effective_profile"]["backend"], "cupy")
        self.assertFalse(
            candidate["fixed_contract"]["dls_runtime_fallback_allowed"]
        )

    def test_inner_iteration_cli_allows_only_the_bounded_sweep(self) -> None:
        parser = remote_timing.build_argument_parser()
        for iterations in (128, 256, 512):
            args = parser.parse_args(
                ["--replay-csv", "trace.csv", "--inner-iterations", str(iterations)]
            )
            self.assertEqual(args.inner_iterations, iterations)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(
                ["--replay-csv", "trace.csv", "--inner-iterations", "1024"]
            )

    def test_solver_effective_profile_mismatch_fails_closed(self) -> None:
        effective = dict(remote_timing.PROFILE)
        effective["inner_iterations"] = 512
        with self.assertRaisesRegex(ValueError, "solver-effective"):
            remote_timing.build_profile_selection(
                256,
                effective_profile=effective,
            )

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
        self.assertIn('"reference_ramp_active_count"', source)
        self.assertIn('"raw_to_governed_twist_error_norm"', source)
        self.assertIn('"execute_path_proven"', source)
        self.assertIn("step5d_v30_contract_pipeline(", source)
        self.assertIn("step5d_tcp_jacobian_base(model_bundle, q, tcp_offset)", source)
        self.assertIn("print(json.dumps(payload", source)
        self.assertIn('"nvidia-smi"', source)
        self.assertIn('"gpu_device"', source)
        solver_loop = source[
            source.index("for index in range(args.solver_samples):") :
            source.index("first_post_warm_ms =")
        ]
        self.assertIn("time.sleep(SOLVER_BATCH_YIELD_S)", solver_loop)
        self.assertLess(
            solver_loop.index("time.sleep(SOLVER_BATCH_YIELD_S)"),
            solver_loop.index("started = time.perf_counter()"),
        )
        full_loop = source[
            source.index("for index in range(args.tick_samples):") :
            source.index("full_tick_elapsed_wall_s =")
        ]
        self.assertIn("build_slew_compatible_reference(", full_loop)
        self.assertIn("policy.compute(governed_observation)", full_loop)
        self.assertIn("step5d_v30_contract_pipeline(\n                governed_observation", full_loop)
        self.assertLess(
            full_loop.index("build_slew_compatible_reference("),
            full_loop.index("solver.warm_start("),
        )
        self.assertLess(
            full_loop.index("build_slew_compatible_reference("),
            full_loop.index("policy.compute(governed_observation)"),
        )
        safe_hold_loop = source[
            source.index("for index in range(args.safe_hold_samples):") :
            source.index("safe_hold_elapsed_wall_s =")
        ]
        self.assertNotIn("build_slew_compatible_reference(", safe_hold_loop)
        self.assertIn("policy.compute(observation)", safe_hold_loop)
        for forbidden in ("RTDEClient", "dashboard_exchange", "socket.connect", "write_text", "write_bytes"):
            self.assertNotIn(forbidden, source)

    def test_deferred_summary_proves_execute_path_and_reference_ramp(self) -> None:
        fields = (
            "accepted",
            "reference_ramp_active",
            "reference_ramp_scale",
            "residual_norm",
            "desired_approach_m_s",
            "predicted_approach_m_s",
            *(f"raw_desired_twist_{index}" for index in range(6)),
            *(f"governed_desired_twist_{index}" for index in range(6)),
        )
        field = {name: index for index, name in enumerate(fields)}
        numeric = np.zeros((2, len(fields)), dtype=float)
        numeric[:, field["accepted"]] = 1.0
        numeric[:, field["reference_ramp_scale"]] = (0.25, 1.0)
        numeric[:, field["reference_ramp_active"]] = (1.0, 0.0)
        numeric[:, field["desired_approach_m_s"]] = 0.001
        numeric[:, field["predicted_approach_m_s"]] = (-0.0001, 0.0009)
        numeric[0, field["raw_desired_twist_0"]] = 1.0
        numeric[0, field["governed_desired_twist_0"]] = 0.25
        numeric[1, field["raw_desired_twist_0"]] = 1.0
        numeric[1, field["governed_desired_twist_0"]] = 1.0
        buffer = SimpleNamespace(
            count=2,
            numeric=numeric,
            actions=["execute", "execute"],
        )

        summary = remote_timing.deferred_control_summary(buffer, fields)

        self.assertEqual(summary["accepted_count"], 2)
        self.assertEqual(summary["execute_count"], 2)
        self.assertEqual(summary["safe_hold_count"], 0)
        self.assertTrue(summary["execute_path_proven"])
        self.assertEqual(summary["reference_ramp_active_count"], 1)
        self.assertEqual(summary["reference_ramp_scale"]["min"], 0.25)
        self.assertEqual(
            summary["raw_to_governed_twist_error_norm"]["max"],
            0.75,
        )
        self.assertEqual(summary["normal_sign_mismatch"]["total_count"], 1)
        self.assertTrue(summary["normal_sign_mismatch"]["first_tick"])
        self.assertEqual(summary["normal_sign_mismatch"]["first_index"], 0)

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
                    "profile_selection",
                    "stage_table",
                    "calibration_yaml",
                    "ur_xacro",
                )
            },
            "profile": {
                "backend": "cupy",
                "inner_iterations": 512,
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
            "solver_microbenchmark_pacing": {
                "mode": "unmeasured_fixed_batch_yield",
                "batch_size": 100,
                "yield_s": 0.002,
                "yield_included_in_single_solve_latency": False,
                "reason": "avoid_linux_sched_fifo_runtime_throttling_during_10k_stress",
                "full_tick_loop_affected": False,
                "safe_hold_loop_affected": False,
            },
            "solver": {"samples": 10000, "nonfinite_count": 0, "mean_ms": 1.2, "p95_ms": 1.3, "p99_ms": 1.4, "max_ms": 1.7, "compute_deadline_miss_count": 0},
            "full_tick": {"samples": 30000, "nonfinite_count": 0, "mean_ms": 1.5, "p95_ms": 1.6, "p99_ms": 1.7, "max_ms": 1.9, "compute_deadline_miss_count": 0},
            "safe_hold": {"samples": 30000, "nonfinite_count": 0, "mean_ms": 0.05, "p95_ms": 0.06, "p99_ms": 0.07, "max_ms": 0.1, "compute_deadline_miss_count": 0},
            "full_tick_schedule_deadline_miss_count": 0,
            "full_tick_schedule_max_lateness_ms": 0.0,
            "safe_hold_schedule_deadline_miss_count": 0,
            "safe_hold_schedule_max_lateness_ms": 0.0,
            "paced_500hz": True,
            "runtime_environment": {
                "nice": 0,
                "scheduler_policy": 1,
                "scheduler_priority": 20,
                "scheduler_limits": {"rtprio": [99, 99]},
                "cpu_affinity": [0, 1],
                "python_executable": "/usr/bin/python3",
                "python_version": "3.10.12",
                "pythonpath": "/tmp/step5d_gpu_np124",
                "cuda": {
                    "runtime_version": 12090,
                    "driver_version": 13020,
                    "nvrtc_version": [12, 9],
                },
                "versions": {
                    "numpy": "1.24.4",
                    "cupy": "13.6.0",
                    "pinocchio": "2.6.21",
                },
                "thread_environment": {
                    "OPENBLAS_NUM_THREADS": "1",
                    "OMP_NUM_THREADS": "1",
                    "MKL_NUM_THREADS": "1",
                    "NUMEXPR_NUM_THREADS": "1",
                },
            },
            "gpu_device": {
                "device_id": 0,
                "name": "NVIDIA RTX 5070 Ti",
                "compute_capability": [12, 0],
                "total_memory_bytes": 17171480576,
            },
            "nvidia_smi": {
                "capture_scope": "outside_measured_solver_and_500hz_loops",
                "start": {
                    "ok": True,
                    "values": {
                        "driver_version": "595.71.05",
                        "name": "NVIDIA RTX 5070 Ti",
                        "pci.bus_id": "00000000:01:00.0",
                        "clocks.current.sm": "210",
                        "clocks.current.memory": "405",
                        "temperature.gpu": "35",
                        "utilization.gpu": "0",
                        "power.draw": "20.0",
                        "persistence_mode": "Enabled",
                    },
                },
                "end": {
                    "ok": True,
                    "values": {
                        "driver_version": "595.71.05",
                        "name": "NVIDIA RTX 5070 Ti",
                        "pci.bus_id": "00000000:01:00.0",
                        "clocks.current.sm": "210",
                        "clocks.current.memory": "405",
                        "temperature.gpu": "36",
                        "utilization.gpu": "0",
                        "power.draw": "21.0",
                        "persistence_mode": "Enabled",
                    },
                },
            },
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
            "full_tick_control_diagnostics": {
                "samples": 30000,
                "accepted_count": 30000,
                "execute_count": 30000,
                "safe_hold_count": 0,
                "execute_path_proven": True,
                "reference_ramp_active_count": 113,
                "reference_ramp_scale": {"min": 0.01, "max": 1.0},
                "raw_to_governed_twist_error_norm": {"max": 0.012},
            },
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
        payload["profile_selection"] = remote_timing.build_profile_selection(None)
        payload["profile_sha256"] = payload["profile_selection"][
            "effective_profile_sha256"
        ]

        result = summarize_preaggregated(
            payload,
            expected_source_binding={field: "1" * 64 for field in SOURCE_BINDING_FILES},
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )

        self.assertTrue(result["overall_pass"])
        self.assertEqual(result["solver"]["samples"], 10000)
        self.assertEqual(result["full_tick"]["deadline_miss_count"], 0)
        self.assertEqual(
            result["runtime_scheduling_classification"],
            "production_sched_fifo_priority_20",
        )

        wrong_scheduler = json.loads(json.dumps(payload))
        wrong_scheduler["runtime_environment"].update(
            {"nice": 19, "scheduler_policy": 2, "scheduler_priority": 1}
        )
        wrong_scheduler_result = summarize_preaggregated(
            wrong_scheduler,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertIn(
            "runtime_timing_process_priority_degraded",
            wrong_scheduler_result["blockers"],
        )

        diagnostic_512 = json.loads(json.dumps(payload))
        diagnostic_512["profile_selection"] = (
            remote_timing.build_profile_selection(512)
        )
        diagnostic_512["profile_sha256"] = diagnostic_512[
            "profile_selection"
        ]["effective_profile_sha256"]
        diagnostic_result = summarize_preaggregated(
            diagnostic_512,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertFalse(diagnostic_result["overall_pass"])
        self.assertIn(
            "remote_timing_diagnostic_profile_override",
            diagnostic_result["blockers"],
        )
        self.assertFalse(
            diagnostic_result["deadline_robustness"][
                "timing_degraded_candidate"
            ]
        )

        no_execute = json.loads(json.dumps(payload))
        no_execute["full_tick_reason_counts"] = {
            "constraint_residual_norm_exceeded": 30000
        }
        no_execute["full_tick_control_diagnostics"].update(
            {
                "accepted_count": 0,
                "execute_count": 0,
                "safe_hold_count": 30000,
                "execute_path_proven": False,
            }
        )
        control_rejected = summarize_preaggregated(
            no_execute,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertFalse(control_rejected["overall_pass"])
        self.assertFalse(
            control_rejected["deadline_robustness"]["timing_degraded_candidate"]
        )

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
        payload["deadline_miss_diagnostics"]["full_tick_schedule"][
            "max_consecutive"
        ] = 3
        clustered = summarize_preaggregated(
            payload,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertFalse(
            clustered["deadline_robustness"]["timing_degraded_candidate"]
        )
        payload["deadline_miss_diagnostics"]["full_tick_schedule"][
            "max_consecutive"
        ] = 2
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
            "profile": {"backend": "cupy", "inner_iterations": 512, "epsilon": 0.01, "sigr_exponent_r": 0.8, "qdot_cap_rad_s": 0.05, "control_hz": 500.0},
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
            "runtime_environment": {
                "nice": 0,
                "scheduler_policy": 1,
                "scheduler_priority": 20,
                "scheduler_limits": {"rtprio": [99, 99]},
                "cpu_affinity": [0, 1],
                "python_executable": "/usr/bin/python3",
                "python_version": "3.10.12",
                "pythonpath": "/tmp/step5d_gpu_np124",
                "cuda": {
                    "runtime_version": 12090,
                    "driver_version": 13020,
                    "nvrtc_version": [12, 9],
                },
                "versions": {
                    "numpy": "1.24.4",
                    "cupy": "13.6.0",
                    "pinocchio": "2.6.21",
                },
                "thread_environment": {
                    "OPENBLAS_NUM_THREADS": "1",
                    "OMP_NUM_THREADS": "1",
                    "MKL_NUM_THREADS": "1",
                    "NUMEXPR_NUM_THREADS": "1",
                },
            },
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
                "inner_iterations": 512,
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
            "runtime_environment": {
                "nice": 0,
                "scheduler_policy": 1,
                "scheduler_priority": 20,
                "scheduler_limits": {"rtprio": [99, 99]},
                "cpu_affinity": [0, 1],
                "python_executable": "/usr/bin/python3",
                "python_version": "3.10.12",
                "pythonpath": "/tmp/step5d_gpu_np124",
                "cuda": {
                    "runtime_version": 12090,
                    "driver_version": 13020,
                    "nvrtc_version": [12, 9],
                },
                "versions": {
                    "numpy": "1.24.4",
                    "cupy": "13.6.0",
                    "pinocchio": "2.6.21",
                },
                "thread_environment": {
                    "OPENBLAS_NUM_THREADS": "1",
                    "OMP_NUM_THREADS": "1",
                    "MKL_NUM_THREADS": "1",
                    "NUMEXPR_NUM_THREADS": "1",
                },
            },
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
