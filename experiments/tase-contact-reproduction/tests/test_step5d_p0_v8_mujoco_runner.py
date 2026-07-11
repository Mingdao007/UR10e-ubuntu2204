#!/usr/bin/env python3
"""Focused tests for the deterministic MuJoCo P0 v8 runner."""

from __future__ import annotations

import inspect
import gc
import json
import math
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_control_contract import STRICT_RNN_SOLVER_OK_STATUS, ZERO6  # noqa: E402
from step5d_simulator_adapter import (  # noqa: E402
    FrameLineage,
    SimulationCommand,
    SimulatorState,
)
import run_step5d_p0_v8_mujoco as runner  # noqa: E402


IDENTITY6 = tuple(
    tuple(float(row == column) for column in range(6)) for row in range(6)
)


class FakeSolver:
    def __init__(self) -> None:
        self.config = SimpleNamespace(epsilon=0.010, sigr_exponent_r=0.8)
        self._cp = SimpleNamespace(__version__="fake-cupy")
        self.cupy_busy_poll_completion = True
        self.cupy_host_staging_pinned = True
        self.cupy_dedicated_stream = True
        self.reset_count = 0
        self.warm_count = 0

    def reset_state(self) -> None:
        self.reset_count += 1

    def warm_start(self, **kwargs: object) -> None:
        self.warm_count += 1
        self.last_warm = kwargs

    def solve(self, *, actual_q: object, actual_qd: object, target_state: dict[str, object]) -> object:
        del actual_q, actual_qd
        desired = tuple(float(value) for value in target_state["xdot_c"])  # type: ignore[index]
        return SimpleNamespace(
            qdot=desired,
            solver_status=STRICT_RNN_SOLVER_OK_STATUS,
            residual_norm=0.0,
            diagnostics={
                "active_bounds_mask": (False,) * 6,
                "xdot_c": desired,
                "backend": "fake_test_only",
            },
        )


class FakePlant:
    def __init__(self) -> None:
        self.time_s = 0.0
        self.commands: list[object] = []
        self.mujoco = SimpleNamespace(__version__="fake-mujoco")
        self.manifest = {
            "calibration_hash": "calib_test",
            "outputs": {"no_contact_velocity": {"sha256": "2" * 64}},
            "claim_boundary": {"physics_provenance": "geometry_provisional"},
            "blockers": ["fake_plant_not_physics"],
        }

    def reset(self) -> None:
        self.time_s = 0.0
        self.commands = []

    def read_state(self, *, sequence: int, wall_time_s: float) -> SimulatorState:
        return SimulatorState(
            engine="mujoco",
            engine_version="fake",
            sequence=sequence,
            sim_time_s=self.time_s,
            wall_time_s=wall_time_s,
            observation_age_s=0.0,
            q=ZERO6,
            qd=ZERO6,
            tcp_pose=ZERO6,
            tcp_twist=ZERO6,
            wrench=ZERO6,
            command_jacobian=IDENTITY6,
            desired_twist=(0.0, 0.0, 0.0001, 0.0, 0.0, 0.0),
            reaction_normal=(0.0, 0.0, -1.0),
            approach_normal=(0.0, 0.0, 1.0),
            frame_lineage=FrameLineage(
                command_frame="base",
                pose_frame="base",
                twist_frame="base",
                wrench_frame="base",
                jacobian_frame="base",
                normal_frame="base",
                transform_chain=("fake_world->base",),
                sha256="1" * 64,
            ),
            calibration_hash="calib_test",
            model_hash="2" * 64,
            omega_minus=(-0.05,) * 6,
            omega_plus=(0.05,) * 6,
        )

    def write_command(self, command: object) -> None:
        self.commands.append(command)
        self.time_s += 0.002


class Step5dP0V8MujocoRunnerTest(unittest.TestCase):
    def test_deadline_is_classified_before_sink_as_exact_zero_stop(self) -> None:
        command = SimulationCommand(
            engine="mujoco",
            sequence=7,
            mode="joint_velocity",
            qdot=(0.0, 0.0, 0.0001, 0.0, 0.0, 0.0),
            accepted=True,
            action="execute",
            reason="ok",
            stop_request=False,
            command_bytes_sha256=runner.simulation_command_sha256(
                (0.0, 0.0, 0.0001, 0.0, 0.0, 0.0),
                cmd_valid=True,
                stop_request=False,
            ),
        )

        on_time, rejected = runner.classify_control_deadline(
            command,
            control_elapsed_ms=1.999999,
        )
        late, late_rejected = runner.classify_control_deadline(
            command,
            control_elapsed_ms=2.0,
        )

        self.assertIs(on_time, command)
        self.assertFalse(rejected)
        self.assertTrue(late_rejected)
        self.assertEqual(late.qdot, ZERO6)
        self.assertFalse(late.accepted)
        self.assertEqual(late.action, "stop")
        self.assertEqual(late.reason, runner.CONTROL_DEADLINE_REASON)
        self.assertTrue(late.stop_request)
        self.assertEqual(
            late.command_bytes_sha256,
            runner.simulation_command_sha256(
                ZERO6,
                cmd_valid=False,
                stop_request=True,
            ),
        )

    def test_production_path_prewarm_is_fixed_paced_complete_and_no_output(self) -> None:
        plant = FakePlant()
        solver = FakeSolver()
        clock = [0.0, *[
            index / runner.PREWARM_CONTROL_HZ
            for index in range(runner.PREWARM_EXECUTE_TICKS)
        ]]
        with (
            mock.patch.object(runner, "wait_until") as wait_until,
            mock.patch.object(runner.time, "perf_counter", side_effect=clock),
        ):
            result = runner.run_production_path_prewarm(
                plant=plant,
                solver=solver,
            )

        self.assertTrue(result.passed)
        self.assertEqual(
            result.execute_tick_count,
            runner.PREWARM_EXECUTE_TICKS,
        )
        self.assertEqual(
            result.accepted_tick_count,
            runner.PREWARM_EXECUTE_TICKS,
        )
        self.assertEqual(
            result.dls_shadow_count,
            runner.PREWARM_EXECUTE_TICKS,
        )
        self.assertEqual(result.dls_runtime_fallback_count, 0)
        self.assertEqual(result.command_sink_write_count, 0)
        self.assertEqual(result.register_command_generation_count, 1000)
        self.assertEqual(plant.commands, [])
        self.assertEqual(wait_until.call_count, runner.PREWARM_EXECUTE_TICKS - 1)
        self.assertEqual(
            result.deferred_diagnostic_count,
            runner.PREWARM_EXECUTE_TICKS,
        )
        self.assertEqual(result.burst_interval_count, 0)
        self.assertAlmostEqual(result.min_inter_release_s, 0.002)
        self.assertAlmostEqual(result.elapsed_release_span_s, 1.998)

        reset = runner.reset_after_production_path_prewarm(
            plant=plant,
            solver=solver,
        )
        self.assertTrue(reset["simulator_state_reset_after_prewarm"])
        self.assertTrue(reset["solver_state_reset_after_prewarm"])
        self.assertEqual(reset["post_reset_unmeasured_execute_tick_count"], 0)
        self.assertEqual(plant.commands, [])

    def test_numeric_thread_environment_is_fail_closed(self) -> None:
        with mock.patch.dict(
            runner.os.environ,
            runner.NUMERIC_THREAD_ENV_CONTRACT,
            clear=True,
        ):
            self.assertEqual(
                runner.require_numeric_thread_environment(),
                runner.NUMERIC_THREAD_ENV_CONTRACT,
            )
        invalid = dict(runner.NUMERIC_THREAD_ENV_CONTRACT)
        invalid["OPENBLAS_NUM_THREADS"] = "2"
        with mock.patch.dict(runner.os.environ, invalid, clear=True):
            with self.assertRaisesRegex(RuntimeError, "thread counts all set to 1"):
                runner.require_numeric_thread_environment()

    def test_runner_exit_code_requires_final_hard_control_gate(self) -> None:
        self.assertEqual(
            runner.runner_exit_code(
                control_diagnostic_pass=True,
                complete=False,
                control_hard_gate_pass=False,
            ),
            0,
        )
        self.assertEqual(
            runner.runner_exit_code(
                control_diagnostic_pass=True,
                complete=True,
                control_hard_gate_pass=False,
            ),
            4,
        )
        self.assertEqual(
            runner.runner_exit_code(
                control_diagnostic_pass=False,
                complete=True,
                control_hard_gate_pass=True,
            ),
            3,
        )

    def test_prefault_numeric_buffers_materializes_all_entries(self) -> None:
        first = np.empty((17, 74), dtype=np.float64)
        second = np.empty((17, 6, 6), dtype=np.float64)

        completed = runner.prefault_numeric_buffers(first, second)

        self.assertTrue(completed)
        self.assertTrue(np.array_equal(first, np.zeros_like(first)))
        self.assertTrue(np.array_equal(second, np.zeros_like(second)))

    def test_release_spin_window_is_bounded_by_one_control_period(self) -> None:
        plant = FakePlant()
        solver = FakeSolver()
        with self.assertRaisesRegex(ValueError, "spin window"):
            runner.run_nominal_phase(
                plant=plant,
                solver=solver,
                spec=runner.PhaseSpec(duration_s=2.0, sequence_index=0),
                release_spin_window_s=0.0021,
            )

    def test_short_fake_phase_uses_integer_schedule_and_shared_adapter(self) -> None:
        plant = FakePlant()
        solver = FakeSolver()
        gc_before = gc.isenabled()

        result = runner.run_nominal_phase(
            plant=plant,
            solver=solver,
            spec=runner.PhaseSpec(duration_s=0.006, sequence_index=0),
        )

        self.assertEqual(result.tick_count, 3)
        self.assertEqual(result.physics_tick_count, 12)
        self.assertEqual(result.dbil_tick_count, 2)
        self.assertEqual(result.accepted_tick_count, 3)
        self.assertTrue(result.control_path_pass)
        self.assertEqual(result.sim_time_drift_s, 0.0)
        self.assertEqual(result.first_sequence, 0)
        self.assertEqual(result.last_sequence, 2)
        self.assertTrue(np.all(result.qdot[:, 2] == 0.0001))
        self.assertEqual(result.release_lateness_ms.shape, (3,))
        self.assertEqual(result.absolute_finish_lateness_ms.shape, (3,))
        self.assertEqual(result.control_compute_ms.shape, (3,))
        self.assertEqual(result.oracle_snapshot_ms.shape, (3,))
        self.assertEqual(result.command_apply_and_physics_ms.shape, (3,))
        self.assertEqual(result.cycle_wall_ms.shape, (3,))
        self.assertTrue(result.trace_buffers_prefaulted)
        self.assertTrue(np.all(result.release_lateness_ms >= 0.0))
        self.assertTrue(np.all(result.absolute_finish_lateness_ms >= 0.0))
        self.assertEqual(result.deferred.count, 3)
        self.assertEqual(gc.isenabled(), gc_before)

    def test_wall_timing_failure_does_not_erase_control_path_pass(self) -> None:
        result = runner.run_nominal_phase(
            plant=FakePlant(),
            solver=FakeSolver(),
            spec=runner.PhaseSpec(duration_s=0.006, sequence_index=0),
        )
        slow = replace(
            result,
            control_deadline_miss_count=result.tick_count,
            control_compute_ms=np.full(result.tick_count, 2.5),
        )

        self.assertFalse(slow.control_path_pass)
        timing = runner.control_hard_timing(slow, paced=True)
        self.assertFalse(timing["pass"])
        self.assertEqual(timing["deadline_miss_count"], result.tick_count)
        self.assertFalse(timing["p99_within_limit"])
        self.assertFalse(timing["max_within_deadline"])
        broken_sequence = replace(result, last_sequence=result.tick_count)
        self.assertFalse(broken_sequence.control_path_pass)
        self.assertFalse(runner.wall_timing(result, paced=False)["pass"])

    def test_contained_deadline_miss_keeps_sample_but_not_timing_pass(self) -> None:
        result = runner.run_nominal_phase(
            plant=FakePlant(),
            solver=FakeSolver(),
            spec=runner.PhaseSpec(duration_s=0.006, sequence_index=0),
        )
        control_ms = np.asarray((0.5, 2.0, 0.5), dtype=float)
        qdot = result.qdot.copy()
        qdot[1, :] = 0.0
        accepted = np.asarray((1, 0, 1), dtype=np.uint8)
        stop_request = np.asarray((0, 1, 0), dtype=np.uint8)
        hashes = np.asarray(
            [
                runner.simulation_command_sha256(
                    qdot[index],
                    cmd_valid=bool(accepted[index]),
                    stop_request=bool(stop_request[index]),
                )
                for index in range(3)
            ],
            dtype="<U64",
        )
        contained = replace(
            result,
            accepted_tick_count=2,
            stop_count=1,
            control_deadline_miss_count=1,
            max_qdot_abs_rad_s=float(np.max(np.abs(qdot))),
            exact_zero_rejection_count=1,
            deadline_rejection_count=1,
            deadline_zero_rejection_count=1,
            nonzero_rejection_count=0,
            control_compute_ms=control_ms,
            qdot=qdot,
            accepted=accepted,
            deadline_rejected=np.asarray((0, 1, 0), dtype=np.uint8),
            command_stop_request=stop_request,
            command_bytes_sha256=hashes,
            actions=np.asarray(("execute", "stop", "execute"), dtype="<U96"),
            reasons=np.asarray(
                ("ok", runner.CONTROL_DEADLINE_REASON, "ok"),
                dtype="<U160",
            ),
        )

        self.assertTrue(contained.control_path_pass)
        timing = runner.control_hard_timing(contained, paced=True)
        self.assertFalse(timing["pass"])
        self.assertEqual(timing["deadline_miss_count"], 1)
        self.assertEqual(timing["samples"], 3)

    def test_slow_physics_is_diagnostic_and_does_not_pollute_control_hard(self) -> None:
        result = runner.run_nominal_phase(
            plant=FakePlant(),
            solver=FakeSolver(),
            spec=runner.PhaseSpec(duration_s=0.006, sequence_index=0),
        )
        slow_physics = replace(
            result,
            control_deadline_miss_count=0,
            cycle_compute_deadline_miss_count=result.tick_count,
            absolute_deadline_miss_count=result.tick_count,
            control_compute_ms=np.full(result.tick_count, 0.5),
            oracle_snapshot_ms=np.full(result.tick_count, 0.1),
            command_apply_and_physics_ms=np.full(result.tick_count, 2.5),
            cycle_wall_ms=np.full(result.tick_count, 3.1),
            release_lateness_ms=np.full(result.tick_count, 1.75),
            absolute_finish_lateness_ms=np.full(result.tick_count, 2.85),
        )

        control = runner.control_hard_timing(slow_physics, paced=True)
        cycle = runner.simulator_cycle_timing(slow_physics)

        self.assertTrue(control["pass"])
        self.assertEqual(control["deadline_miss_count"], 0)
        self.assertFalse(cycle["meets_500hz_diagnostic"])
        self.assertEqual(
            cycle["cycle_compute_deadline_miss_count"],
            result.tick_count,
        )

    def test_unprefaulted_or_slow_control_fails_hard_lane(self) -> None:
        result = runner.run_nominal_phase(
            plant=FakePlant(),
            solver=FakeSolver(),
            spec=runner.PhaseSpec(duration_s=0.006, sequence_index=0),
        )
        slow = replace(
            result,
            control_deadline_miss_count=result.tick_count,
            control_compute_ms=np.full(result.tick_count, 2.1),
        )
        unprefaulted = replace(result, trace_buffers_prefaulted=False)

        self.assertFalse(runner.control_hard_timing(slow, paced=True)["pass"])
        self.assertFalse(
            runner.control_hard_timing(unprefaulted, paced=True)["pass"]
        )

    def test_absolute_schedule_starts_after_one_off_gc_collection(self) -> None:
        source = inspect.getsource(runner.run_nominal_phase)

        self.assertLess(
            source.index("gc.collect()"),
            source.index("wall_start = time.perf_counter()"),
        )

    def test_runtime_timing_environment_binds_scheduler_affinity_and_capabilities(self) -> None:
        with (
            mock.patch.object(runner.os, "sched_getaffinity", return_value={2, 4}, create=True),
            mock.patch.object(runner.os, "sched_getscheduler", return_value=0, create=True),
            mock.patch.object(
                runner.os,
                "sched_getparam",
                return_value=SimpleNamespace(sched_priority=7),
                create=True,
            ),
            mock.patch.dict(
                runner.os.environ,
                {
                    "OPENBLAS_NUM_THREADS": "1",
                    "OMP_NUM_THREADS": "2",
                    "MKL_NUM_THREADS": "3",
                    "NUMEXPR_NUM_THREADS": "4",
                },
                clear=False,
            ),
        ):
            environment = runner.runtime_timing_environment(
                plant=FakePlant(),
                solver=FakeSolver(),
                pace_wall_clock=True,
                release_spin_window_s=0.002,
            )

        self.assertEqual(environment["process_affinity"]["cpu_ids"], [2, 4])
        self.assertEqual(environment["process_scheduler"]["priority"], 7)
        self.assertEqual(environment["thread_environment"]["OMP_NUM_THREADS"], "2")
        self.assertEqual(environment["versions"]["cupy"], "fake-cupy")
        self.assertEqual(environment["versions"]["mujoco"], "fake-mujoco")
        self.assertTrue(environment["capabilities"]["busy_poll_completion"])
        self.assertTrue(environment["paced_wall_clock"])
        self.assertEqual(environment["release_spin_window_s"], 0.002)
        changed = json.loads(json.dumps(environment))
        changed["process_affinity"]["cpu_ids"] = [2]
        self.assertNotEqual(
            runner.canonical_sha256(environment),
            runner.canonical_sha256(changed),
        )
        changed_scheduler = json.loads(json.dumps(environment))
        changed_scheduler["process_scheduler"]["policy_name"] = "SCHED_FIFO"
        changed_scheduler["process_scheduler"]["priority"] = 20
        self.assertNotEqual(
            runner.canonical_sha256(environment),
            runner.canonical_sha256(changed_scheduler),
        )

    def test_fault_matrix_is_complete_and_every_rejection_is_exact_zero(self) -> None:
        rows = runner.run_fault_matrix(plant=FakePlant(), solver=FakeSolver())

        self.assertEqual({row["id"] for row in rows}, runner.P0_REQUIRED_FAULTS)
        self.assertTrue(all(row["passed"] is True for row in rows), rows)
        self.assertTrue(all(row["exact_zero_command"] is True for row in rows))

    def test_phase_parser_only_accepts_canonical_prefix(self) -> None:
        self.assertEqual(
            [spec.duration_s for spec in runner.parse_phases((2.0, 10.0))],
            [2.0, 10.0],
        )
        with self.assertRaisesRegex(ValueError, "canonical prefix"):
            runner.parse_phases((10.0,))
        with self.assertRaisesRegex(ValueError, "canonical prefix"):
            runner.parse_phases((2.0, 60.0))

    def test_no_contact_scene_must_be_hash_bound_and_cover_60_seconds(self) -> None:
        plant = FakePlant()
        plant.manifest["no_contact_scene"] = {
            "id": "test_no_contact_scene",
            "mode": "surface_translation_with_native_collision",
            "output_key": "no_contact_velocity",
            "translation_offset_m": [0.25, 0.0, 0.0],
            "initial_clearance_m": 0.020,
            "required_initial_clearance_m": 0.010,
            "commanded_travel_budget_m": 0.009,
            "minimum_remaining_clearance_m": 0.011,
            "native_contact_enabled": True,
            "claim": "test_only",
        }

        lane = runner.require_hash_bound_no_contact_lane(plant)  # type: ignore[arg-type]

        self.assertEqual(lane["minimum_remaining_clearance_m"], 0.011)
        missing = FakePlant()
        with self.assertRaisesRegex(ValueError, "lacks a hash-bound"):
            runner.require_hash_bound_no_contact_lane(missing)  # type: ignore[arg-type]
        plant.manifest["no_contact_scene"]["minimum_remaining_clearance_m"] = 0.004  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "internally inconsistent"):
            runner.require_hash_bound_no_contact_lane(plant)  # type: ignore[arg-type]

    def test_runner_does_not_copy_policy_or_safety_implementation(self) -> None:
        source = inspect.getsource(runner)

        self.assertIn("Step5dSimulatorAdapter", source)
        self.assertIn("StrictRnnControlPolicy", source)
        self.assertIn("SafetyEnvelope", source)
        self.assertNotIn("def compute_dls_shadow", source)
        self.assertNotIn("def step5d_v30_contract_pipeline", source)
        self.assertNotIn("class SafetyEnvelope", source)
        self.assertNotIn("class StrictRnnControlPolicy", source)

    def test_phase_rejects_non_integer_500hz_duration(self) -> None:
        with self.assertRaisesRegex(ValueError, "integer number"):
            runner.PhaseSpec(duration_s=0.003, sequence_index=0).tick_count


if __name__ == "__main__":
    unittest.main()
