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
    EXPECTED_PIPELINE_WARMUP,
    EXPECTED_SOLVER_MICROBENCHMARK_PACING,
    SOURCE_BINDING_FILES,
    SOLVER_BATCH_REENTRY_BOUNDARIES,
    SOLVER_BATCH_REENTRY_SAMPLES,
    TimingThresholds,
    summarize_preaggregated,
    summarize_timing,
)


class Step5dV30TimingTest(unittest.TestCase):
    def test_bounded_hold_lateness_budget_stays_below_one_control_tick(self) -> None:
        thresholds = TimingThresholds()

        self.assertEqual(thresholds.bounded_hold_schedule_lateness_max_ms, 1.5)
        self.assertLess(
            thresholds.bounded_hold_schedule_lateness_max_ms,
            thresholds.hard_deadline_ms,
        )

    def test_formal_solver_batch_reentry_contract_is_exact(self) -> None:
        self.assertEqual(remote_timing.SOLVER_BATCH_SIZE, 100)
        self.assertEqual(SOLVER_BATCH_REENTRY_SAMPLES, 99)
        self.assertEqual(len(SOLVER_BATCH_REENTRY_BOUNDARIES), 99)
        self.assertEqual(SOLVER_BATCH_REENTRY_BOUNDARIES[0], 100)
        self.assertEqual(SOLVER_BATCH_REENTRY_BOUNDARIES[-1], 9900)
        self.assertEqual(
            EXPECTED_SOLVER_MICROBENCHMARK_PACING[
                "reentry_sample_boundaries"
            ],
            list(range(100, 10_000, 100)),
        )

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

    def test_formal_capture_cannot_omit_complete_raw_samples(self) -> None:
        canonical = remote_timing.build_profile_selection(None)
        diagnostic = remote_timing.build_profile_selection(512)

        self.assertTrue(
            remote_timing.formal_acceptance_raw_capture_required(
                profile_selection=canonical,
                solver_samples=10_000,
                tick_samples=30_000,
                safe_hold_samples=30_000,
                paced_500hz=True,
            )
        )
        self.assertFalse(
            remote_timing.formal_acceptance_raw_capture_required(
                profile_selection=diagnostic,
                solver_samples=10_000,
                tick_samples=30_000,
                safe_hold_samples=30_000,
                paced_500hz=True,
            )
        )
        self.assertFalse(
            remote_timing.formal_acceptance_raw_capture_required(
                profile_selection=canonical,
                solver_samples=9_999,
                tick_samples=30_000,
                safe_hold_samples=30_000,
                paced_500hz=True,
            )
        )

        raw = remote_timing.indexed_raw_timing_samples(
            solver_ms=[0.1, 0.2],
            full_tick_ms=[0.3],
            safe_hold_ms=[0.4],
        )
        self.assertEqual(raw["lanes"]["solver"]["sample_indices"], [0, 1])
        self.assertEqual(raw["lanes"]["solver"]["elapsed_ms"], [0.1, 0.2])

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

    def test_remote_harness_is_stdout_only_and_prohibits_transport_use(self) -> None:
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
        self.assertIn('"solver_batch_reentry_ms"', source)
        self.assertIn('"solver_batch_reentry_compute"', source)
        self.assertIn('"max_consecutive"', source)
        self.assertIn('"reference_ramp_active_count"', source)
        self.assertIn('"raw_to_governed_twist_error_norm"', source)
        self.assertIn('"execute_path_proven"', source)
        self.assertIn('"unmeasured_pipeline_warmup"', source)
        self.assertIn('"commands_published": False', source)
        self.assertIn('"control_state_reset_after": True', source)
        self.assertIn("wait_until(warmup_release)", source)
        self.assertIn("wait_until(safe_warmup_release)", source)
        self.assertIn('"post_warmup_sleep_s": 0.0', source)
        self.assertIn('"measurement_follows_immediately": True', source)
        self.assertIn("sys.addaudithook(reject_network_transport)", source)
        self.assertIn('"network_transport_tripwire"', source)
        self.assertIn("step5d_v30_contract_pipeline(", source)
        self.assertIn("step5d_tcp_jacobian_base(model_bundle, q, tcp_offset)", source)
        self.assertIn("print(json.dumps(payload", source)
        self.assertIn('"nvidia-smi"', source)
        self.assertIn('"gpu_device"', source)
        self.assertIn('"step5d_v30_remote_timing_raw_v3"', source)
        self.assertIn("formal_acceptance_raw_capture_required(", source)
        self.assertIn('payload["raw_timing_samples"]', source)
        solver_loop = source[
            source.index("for index in range(args.solver_samples):") :
            source.index("first_post_warm_ms =")
        ]
        self.assertIn("time.sleep(SOLVER_BATCH_YIELD_S)", solver_loop)
        self.assertLess(
            solver_loop.index("time.sleep(SOLVER_BATCH_YIELD_S)"),
            solver_loop.index("reentry_started = time.perf_counter()"),
        )
        self.assertLess(
            solver_loop.index("reentry_started = time.perf_counter()"),
            solver_loop.index("steady_started = time.perf_counter()"),
        )
        self.assertIn(
            "solver_batch_reentry_count = (args.solver_samples - 1) // SOLVER_BATCH_SIZE",
            source,
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
        for forbidden in (
            "RTDEClient(",
            "dashboard_exchange(",
            ".connect(",
            "write_text",
            "write_bytes",
        ):
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

    def test_remote_payload_is_accepted_only_with_complete_indexed_raw_samples(self) -> None:
        solver_ms = [1.7] + [1.2] * 9_999
        full_tick_ms = [1.5] * 30_000
        safe_hold_ms = [0.05] * 30_000
        payload = {
            "schema_version": "step5d_v30_remote_timing_raw_v3",
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
            "unmeasured_pipeline_warmup": {
                **EXPECTED_PIPELINE_WARMUP,
                "execute_elapsed_wall_s": 2.0,
                "safe_hold_elapsed_wall_s": 0.2,
                "elapsed_wall_s": 2.2,
                "execute_schedule_deadline_miss_count": 3,
                "execute_schedule_max_lateness_ms": 4.2,
                "safe_hold_schedule_deadline_miss_count": 0,
                "safe_hold_schedule_max_lateness_ms": 0.0,
            },
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
                "mode": "unmeasured_fixed_batch_yield_with_measured_reentry",
                "batch_size": 100,
                "yield_s": 0.002,
                "yield_included_in_single_solve_latency": False,
                "steady_samples": 10000,
                "steady_samples_per_batch": 100,
                "measured_reentry_after_each_yield": True,
                "reentry_samples": 99,
                "reentry_sample_boundaries": list(range(100, 10000, 100)),
                "reentry_included_in_steady_solver_summary": False,
                "all_reentry_samples_retained_raw": True,
                "reason": "avoid_linux_sched_fifo_runtime_throttling_during_10k_stress",
                "full_tick_loop_affected": False,
                "safe_hold_loop_affected": False,
            },
            "solver": remote_timing.distribution(solver_ms),
            "solver_batch_reentry_ms": [0.8] * 98 + [2.1],
            "solver_batch_reentry": remote_timing.distribution(
                [0.8] * 98 + [2.1]
            ),
            "full_tick": remote_timing.distribution(full_tick_ms),
            "safe_hold": remote_timing.distribution(safe_hold_ms),
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
                "linux_sched_rt_bandwidth": {
                    "period_us": 1000000,
                    "runtime_us": 950000,
                    "capture": "read_only_procfs",
                },
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
                "solver_batch_reentry_compute": {
                    "total": 1,
                    "retained_indices": [98],
                    "capacity": 99,
                    "overflowed": False,
                },
                "full_tick_compute": {"total": 0, "retained_indices": [], "overflowed": False, "max_consecutive": 0},
                "full_tick_schedule": {"total": 0, "retained_indices": [], "overflowed": False, "max_consecutive": 0},
                "safe_hold_compute": {"total": 0, "retained_indices": [], "overflowed": False, "max_consecutive": 0},
                "safe_hold_schedule": {"total": 0, "retained_indices": [], "overflowed": False, "max_consecutive": 0},
            },
            "runtime_path_source": "kunwei_rtde_bridge.step5d_v30_contract_pipeline",
            "network_transport_tripwire": {
                "installed": True,
                "prohibited_events": [
                    "socket.bind",
                    "socket.connect",
                    "socket.getaddrinfo",
                    "socket.sendto",
                ],
                "violations": [],
            },
            "full_tick_deferred_diagnostics": {"count": 30000, "overflowed": False},
            "safe_hold_deferred_diagnostics": {"count": 30000, "overflowed": False},
            "raw_sample_capture": {
                "formal_acceptance_required": True,
                "explicit_diagnostic_request": False,
                "included": True,
                "format": remote_timing.RAW_TIMING_SAMPLES_SCHEMA,
                "expected_formal_counts": {
                    "solver": 10_000,
                    "full_tick": 30_000,
                    "safe_hold": 30_000,
                },
            },
            "raw_timing_samples": remote_timing.indexed_raw_timing_samples(
                solver_ms=solver_ms,
                full_tick_ms=full_tick_ms,
                safe_hold_ms=safe_hold_ms,
            ),
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
        self.assertTrue(result["pipeline_warmup_contract_proven"])
        self.assertEqual(result["solver"]["samples"], 10000)
        self.assertEqual(result["solver"]["p50_ms"], 1.2)
        self.assertEqual(
            result["solver"]["p95_ms"],
            remote_timing.distribution(solver_ms)["p95_ms"],
        )
        self.assertEqual(
            result["solver"]["p99_ms"],
            remote_timing.distribution(solver_ms)["p99_ms"],
        )
        self.assertEqual(result["solver"]["max_ms"], 1.7)
        self.assertTrue(
            result["raw_timing_evidence"]["summary_recomputed_independently"]
        )
        self.assertTrue(
            result["raw_timing_evidence"]["lanes"]["full_tick"][
                "exact_count_proven"
            ]
        )
        self.assertEqual(result["solver_batch_reentry"]["samples"], 99)
        self.assertEqual(result["solver_batch_reentry"]["deadline_miss_count"], 1)
        self.assertEqual(
            result["solver_batch_reentry_evidence"]["deadline_miss_indices"],
            [98],
        )
        self.assertTrue(result["solver_batch_reentry_evidence"]["raw_samples_bound"])
        self.assertFalse(
            result["solver_batch_reentry_evidence"][
                "hard_solver_deadline_gate_applied"
            ]
        )
        self.assertTrue(
            result["solver_batch_reentry_evidence"][
                "full_tick_zero_miss_required_for_hard_acceptance"
            ]
        )
        self.assertEqual(result["full_tick"]["deadline_miss_count"], 0)
        self.assertEqual(
            result["runtime_scheduling_classification"],
            "production_sched_fifo_priority_20",
        )
        self.assertFalse(
            result["deadline_robustness"]["bounded_hold_timing_candidate"]
        )

        drifted_summary = json.loads(json.dumps(payload))
        drifted_summary["solver"]["p50_ms"] += 0.01
        drifted_result = summarize_preaggregated(
            drifted_summary,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertFalse(drifted_result["overall_pass"])
        self.assertIn(
            "solver_summary_binding_mismatch",
            drifted_result["blockers"],
        )

        deleted_sample = json.loads(json.dumps(payload))
        deleted_lane = deleted_sample["raw_timing_samples"]["lanes"]["full_tick"]
        deleted_lane["sample_indices"].pop()
        deleted_lane["elapsed_ms"].pop()
        deleted_result = summarize_preaggregated(
            deleted_sample,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertFalse(deleted_result["overall_pass"])
        self.assertIn(
            "full_tick_raw_sample_count_invalid",
            deleted_result["blockers"],
        )
        self.assertIn(
            "full_tick_summary_binding_mismatch",
            deleted_result["blockers"],
        )

        reordered_sample = json.loads(json.dumps(payload))
        reordered_indices = reordered_sample["raw_timing_samples"]["lanes"][
            "safe_hold"
        ]["sample_indices"]
        reordered_indices[0], reordered_indices[1] = (
            reordered_indices[1],
            reordered_indices[0],
        )
        reordered_result = summarize_preaggregated(
            reordered_sample,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertFalse(reordered_result["overall_pass"])
        self.assertIn(
            "safe_hold_raw_sample_order_invalid",
            reordered_result["blockers"],
        )

        drifted_first = json.loads(json.dumps(payload))
        drifted_first["first_post_warm_ms"] = 1.6
        drifted_first_result = summarize_preaggregated(
            drifted_first,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertFalse(drifted_first_result["overall_pass"])
        self.assertIn(
            "first_post_warm_summary_binding_mismatch",
            drifted_first_result["blockers"],
        )

        nonfinite_raw = json.loads(json.dumps(payload))
        nonfinite_solver = nonfinite_raw["raw_timing_samples"]["lanes"][
            "solver"
        ]["elapsed_ms"]
        nonfinite_solver[10] = float("nan")
        nonfinite_raw["solver"] = remote_timing.distribution(nonfinite_solver)
        nonfinite_result = summarize_preaggregated(
            nonfinite_raw,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertFalse(nonfinite_result["overall_pass"])
        self.assertEqual(nonfinite_result["solver"]["nonfinite_count"], 1)
        self.assertIn("solver_nonfinite_timing", nonfinite_result["blockers"])

        no_warmup = json.loads(json.dumps(payload))
        no_warmup.pop("unmeasured_pipeline_warmup")
        no_warmup_result = summarize_preaggregated(
            no_warmup,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertFalse(no_warmup_result["overall_pass"])
        self.assertIn(
            "full_pipeline_warmup_contract_missing_or_invalid",
            no_warmup_result["blockers"],
        )

        type_confused_warmup = json.loads(json.dumps(payload))
        type_confused_warmup["unmeasured_pipeline_warmup"].update(
            {
                "outside_measured_loops": 1,
                "commands_published": 0,
                "execute_count": 1000.0,
            }
        )
        type_confused_result = summarize_preaggregated(
            type_confused_warmup,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertIn(
            "full_pipeline_warmup_contract_missing_or_invalid",
            type_confused_result["blockers"],
        )

        invalid_warmups = {
            "nan_elapsed": {"elapsed_wall_s": float("nan")},
            "physically_too_short": {
                "execute_elapsed_wall_s": 1.0,
                "safe_hold_elapsed_wall_s": 0.1,
                "elapsed_wall_s": 1.1,
            },
            "reset_not_proven": {"solver_state_reset_after": False},
            "schedule_count_overflow": {
                "execute_schedule_deadline_miss_count": 1001,
                "execute_schedule_max_lateness_ms": 1.0,
            },
        }
        for label, mutation in invalid_warmups.items():
            with self.subTest(warmup_tamper=label):
                tampered = json.loads(json.dumps(payload))
                tampered["unmeasured_pipeline_warmup"].update(mutation)
                tampered_result = summarize_preaggregated(
                    tampered,
                    expected_source_binding={
                        field: "1" * 64 for field in SOURCE_BINDING_FILES
                    },
                    expected_replay_sha256="2" * 64,
                    expected_paper_truth_sha256="2" * 64,
                )
                self.assertIn(
                    "full_pipeline_warmup_contract_missing_or_invalid",
                    tampered_result["blockers"],
                )
                self.assertFalse(
                    tampered_result["deadline_robustness"][
                        "bounded_hold_timing_candidate"
                    ]
                )

        tampered_reentry = json.loads(json.dumps(payload))
        tampered_reentry["solver_batch_reentry_ms"][98] = 0.7
        tampered_result = summarize_preaggregated(
            tampered_reentry,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertFalse(tampered_result["overall_pass"])
        self.assertIn(
            "solver_batch_reentry_summary_binding_mismatch",
            tampered_result["blockers"],
        )
        self.assertIn(
            "solver_batch_reentry_miss_diagnostics_unbound",
            tampered_result["blockers"],
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
                "bounded_hold_timing_candidate"
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
            control_rejected["deadline_robustness"][
                "bounded_hold_timing_candidate"
            ]
        )

        full_tick_raw = payload["raw_timing_samples"]["lanes"]["full_tick"][
            "elapsed_ms"
        ]
        miss_indices = list(range(100, 110)) + list(range(200, 29200, 100))
        self.assertEqual(len(miss_indices), 300)
        for miss_index in miss_indices:
            full_tick_raw[miss_index] = 2.16
        payload["full_tick"] = remote_timing.distribution(full_tick_raw)
        payload["full_tick_schedule_deadline_miss_count"] = 300
        payload["full_tick_schedule_max_lateness_ms"] = 0.17
        payload["deadline_miss_diagnostics"]["full_tick_compute"].update(
            {
                "total": 300,
                "retained_indices": miss_indices,
                "max_consecutive": 10,
            }
        )
        payload["deadline_miss_diagnostics"]["full_tick_schedule"].update(
            {
                "total": 300,
                "retained_indices": miss_indices,
                "max_consecutive": 10,
            }
        )
        bounded_without_contract = summarize_preaggregated(
            payload,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertFalse(bounded_without_contract["overall_pass"])
        self.assertTrue(
            bounded_without_contract["deadline_robustness"][
                "bounded_hold_timing_candidate"
            ]
        )
        self.assertFalse(
            bounded_without_contract["deadline_robustness"][
                "bounded_last_command_hold_pass"
            ]
        )
        payload["controller_stale_hold_fault_evidence"] = {
            "pass": True,
            "stale_tick_command": "last_published_guard_approved_qdot_consumed",
            "late_candidate_policy": "discard_without_publish",
            "same_heartbeat_republished": True,
            "solver_history_restored_to_held_qdot": True,
            "stop_dominates_hold": True,
            "held_tick_counts_as_consumed": True,
            "continuous_stale_stop_s": 0.020,
            "max_consecutive_held_ticks": 10,
            "miss_ratio_max": 0.01,
        }
        bounded = summarize_preaggregated(
            payload,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertTrue(bounded["overall_pass"])
        self.assertFalse(bounded["deadline_robustness"]["hard_realtime_pass"])
        self.assertTrue(
            bounded["deadline_robustness"]["bounded_last_command_hold_pass"]
        )
        payload["deadline_miss_diagnostics"]["full_tick_schedule"][
            "max_consecutive"
        ] = 11
        clustered = summarize_preaggregated(
            payload,
            expected_source_binding={
                field: "1" * 64 for field in SOURCE_BINDING_FILES
            },
            expected_replay_sha256="2" * 64,
            expected_paper_truth_sha256="2" * 64,
        )
        self.assertFalse(
            clustered["deadline_robustness"]["bounded_hold_timing_candidate"]
        )
        payload["deadline_miss_diagnostics"]["full_tick_schedule"][
            "max_consecutive"
        ] = 10
        full_tick_raw[29999] = 2.16
        miss_indices.append(29999)
        payload["full_tick"] = remote_timing.distribution(full_tick_raw)
        payload["full_tick_schedule_deadline_miss_count"] = 301
        payload["deadline_miss_diagnostics"]["full_tick_compute"].update(
            {"total": 301, "retained_indices": miss_indices}
        )
        payload["deadline_miss_diagnostics"]["full_tick_schedule"].update(
            {"total": 301, "retained_indices": miss_indices}
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
            rejected_tail["deadline_robustness"][
                "bounded_hold_timing_candidate"
            ]
        )

        unbound = summarize_preaggregated(payload)
        self.assertFalse(unbound["overall_pass"])
        self.assertIn(
            "remote_timing_expected_source_binding_missing", unbound["blockers"]
        )

    def test_compact_remote_payload_fails_on_schedule_miss(self) -> None:
        payload = {
            "schema_version": "step5d_v30_remote_timing_raw_v2",
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
            "schema_version": "step5d_v30_remote_timing_raw_v2",
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
