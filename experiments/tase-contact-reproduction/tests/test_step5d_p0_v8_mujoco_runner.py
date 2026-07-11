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
from step5d_simulator_adapter import FrameLineage, SimulatorState  # noqa: E402
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
            compute_deadline_miss_count=result.tick_count,
            absolute_deadline_miss_count=result.tick_count,
            compute_ms=np.full(result.tick_count, 2.5),
            absolute_finish_lateness_ms=np.full(result.tick_count, 0.5),
        )

        self.assertTrue(slow.control_path_pass)
        timing = runner.wall_timing(slow, paced=True)
        self.assertFalse(timing["pass"])
        self.assertEqual(timing["deadline_miss_count"], result.tick_count)
        self.assertEqual(
            timing["compute_deadline_miss_count"], result.tick_count
        )
        self.assertEqual(
            timing["absolute_deadline_miss_count"], result.tick_count
        )
        self.assertFalse(timing["p99_within_limit"])
        self.assertFalse(timing["max_within_deadline"])
        broken_sequence = replace(result, last_sequence=result.tick_count)
        self.assertFalse(broken_sequence.control_path_pass)
        self.assertFalse(runner.wall_timing(result, paced=False)["pass"])

    def test_absolute_release_deadline_blocks_even_when_compute_is_fast(self) -> None:
        result = runner.run_nominal_phase(
            plant=FakePlant(),
            solver=FakeSolver(),
            spec=runner.PhaseSpec(duration_s=0.006, sequence_index=0),
        )
        schedule_late = replace(
            result,
            compute_deadline_miss_count=0,
            absolute_deadline_miss_count=result.tick_count,
            compute_ms=np.full(result.tick_count, 0.5),
            release_lateness_ms=np.full(result.tick_count, 1.75),
            absolute_finish_lateness_ms=np.full(result.tick_count, 0.25),
        )

        timing = runner.wall_timing(schedule_late, paced=True)

        self.assertTrue(timing["p99_within_limit"])
        self.assertTrue(timing["max_within_deadline"])
        self.assertEqual(timing["compute_deadline_miss_count"], 0)
        self.assertEqual(
            timing["absolute_deadline_miss_count"], result.tick_count
        )
        self.assertFalse(timing["absolute_finish_within_deadline"])
        self.assertFalse(timing["pass"])

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
            )

        self.assertEqual(environment["process_affinity"]["cpu_ids"], [2, 4])
        self.assertEqual(environment["process_scheduler"]["priority"], 7)
        self.assertEqual(environment["thread_environment"]["OMP_NUM_THREADS"], "2")
        self.assertEqual(environment["versions"]["cupy"], "fake-cupy")
        self.assertEqual(environment["versions"]["mujoco"], "fake-mujoco")
        self.assertTrue(environment["capabilities"]["busy_poll_completion"])
        self.assertTrue(environment["paced_wall_clock"])
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
