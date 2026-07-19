#!/usr/bin/env python3
"""Deterministic unit and negative tests for the single simulator adapter."""

from __future__ import annotations

import dataclasses
import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_control_contract import (  # noqa: E402
    STRICT_RNN_SOLVER_OK_STATUS,
    ZERO6,
    ControlCandidate,
    DeferredV30Diagnostics,
    SafetyEnvelope,
)
from step5d_simulator_adapter import (  # noqa: E402
    P0_V8_HISTORICAL_DIAGNOSTIC_PHASES_S,
    FrameLineage,
    IntegerRateSchedule,
    SimulatorState,
    Step5dIngressGuard,
    Step5dSimulatorAdapter,
    assert_p0_v8_profile,
    simulation_claim_boundary,
)


IDENTITY6 = tuple(
    tuple(float(row == column) for column in range(6)) for row in range(6)
)


def lineage(**overrides: object) -> FrameLineage:
    values: dict[str, object] = {
        "command_frame": "base",
        "pose_frame": "base",
        "twist_frame": "base",
        "wrench_frame": "base",
        "jacobian_frame": "base",
        "normal_frame": "base",
        "transform_chain": ("world->base", "base->tool0", "tool0->active_tcp"),
        "sha256": "1" * 64,
    }
    values.update(overrides)
    return FrameLineage(**values)  # type: ignore[arg-type]


def state(**overrides: object) -> SimulatorState:
    values: dict[str, object] = {
        "engine": "unit",
        "engine_version": "1.0",
        "sequence": 0,
        "sim_time_s": 0.0,
        "wall_time_s": 100.0,
        "observation_age_s": 0.0,
        "q": ZERO6,
        "qd": ZERO6,
        "tcp_pose": ZERO6,
        "tcp_twist": ZERO6,
        "wrench": ZERO6,
        "command_jacobian": IDENTITY6,
        "desired_twist": (0.0, 0.0, 0.0004, 0.0, 0.0, 0.0),
        "reaction_normal": (0.0, 0.0, -1.0),
        "approach_normal": (0.0, 0.0, 1.0),
        "frame_lineage": lineage(),
        "calibration_hash": "calib_7367377276742883610",
        "model_hash": "2" * 64,
    }
    values.update(overrides)
    return SimulatorState(**values)  # type: ignore[arg-type]


class FixedPolicy:
    def __init__(self, qdot: tuple[float, ...] = (0.0, 0.0, 0.0004, 0.0, 0.0, 0.0)) -> None:
        self.qdot = qdot

    def compute(self, observation: object) -> ControlCandidate:
        jacobian = getattr(observation, "jacobian")
        predicted = tuple(
            sum(float(jacobian[row][column]) * self.qdot[column] for column in range(6))
            for row in range(6)
        )
        desired = getattr(observation, "desired_twist")
        residual = math.sqrt(
            sum((predicted[index] - float(desired[index])) ** 2 for index in range(6))
        )
        return ControlCandidate(
            qdot=self.qdot,  # type: ignore[arg-type]
            predicted_twist=predicted,  # type: ignore[arg-type]
            residual_norm=residual,
            active_bounds_count=0,
            frame_id=getattr(observation, "command_frame"),
            solver_status=str(STRICT_RNN_SOLVER_OK_STATUS),
            diagnostics={"backend": "unit", "active_bounds_mask": (False,) * 6},
        )


def adapter(policy: object | None = None) -> Step5dSimulatorAdapter:
    return Step5dSimulatorAdapter(
        policy=policy or FixedPolicy(),  # type: ignore[arg-type]
        safety_envelope=SafetyEnvelope(),
        deferred_diagnostics=DeferredV30Diagnostics(capacity=16),
    )


class Step5dSimulatorAdapterTest(unittest.TestCase):
    def test_integer_schedule_is_exact_2k_500_200(self) -> None:
        schedule = IntegerRateSchedule()

        self.assertEqual(schedule.control_stride, 4)
        self.assertEqual(schedule.dbil_stride, 10)
        self.assertEqual(
            [tick for tick in range(40) if schedule.is_control_tick(tick)],
            list(range(0, 40, 4)),
        )
        self.assertEqual(
            [tick for tick in range(40) if schedule.is_dbil_tick(tick)],
            list(range(0, 40, 10)),
        )
        with self.assertRaisesRegex(ValueError, "integer"):
            IntegerRateSchedule(physics_hz=2_000, control_hz=333, dbil_hz=200)

    def test_frozen_profile_and_canary_order(self) -> None:
        assert_p0_v8_profile(
            {
                "backend": "cupy",
                "inner_iterations": 512,
                "epsilon": 0.010,
                "sigr_exponent_r": 0.8,
                "qdot_cap_rad_s": 0.05,
                "effective_ko": 0.01,
                "dls_runtime_fallback_allowed": False,
            }
        )
        self.assertEqual(P0_V8_HISTORICAL_DIAGNOSTIC_PHASES_S, (2.0, 10.0, 60.0))
        with self.assertRaisesRegex(ValueError, "profile drift"):
            assert_p0_v8_profile(
                {
                    "backend": "cupy",
                    "inner_iterations": 512,
                    "epsilon": 0.022,
                    "sigr_exponent_r": 0.8,
                    "qdot_cap_rad_s": 0.05,
                    "effective_ko": 0.01,
                    "dls_runtime_fallback_allowed": False,
                }
            )

    def test_nominal_tick_uses_production_pipeline_and_dls_is_shadow_only(self) -> None:
        result = adapter().step(state())

        self.assertEqual(result.ingress_reason, "ok")
        self.assertTrue(result.control.decision.accepted)
        self.assertEqual(result.simulation_command.qdot[2], 0.0004)
        self.assertEqual(result.command_jacobian_source, "calibrated_pinocchio")
        self.assertFalse(result.engine_oracle_command_source)
        self.assertIsNotNone(result.control.dls_shadow)
        assert result.control.dls_shadow is not None
        self.assertFalse(result.control.dls_shadow.runtime_fallback_allowed)
        self.assertEqual(
            result.simulation_command.qdot,
            result.control.register_command.qdot,
        )

    def test_policy_exception_and_missing_output_stop_exact_zero(self) -> None:
        class RaisingPolicy:
            def compute(self, observation: object) -> ControlCandidate:
                raise RuntimeError("synthetic")

        class MissingPolicy:
            def compute(self, observation: object) -> None:
                return None

        for policy, expected in (
            (RaisingPolicy(), "strict_rnn_policy_failure:RuntimeError"),
            (MissingPolicy(), "strict_rnn_policy_failure:TypeError"),
        ):
            with self.subTest(policy=type(policy).__name__):
                result = adapter(policy).step(state())
                self.assertEqual(result.control.decision.reason, expected)
                self.assertEqual(result.control.decision.action, "stop")
                self.assertEqual(result.simulation_command.qdot, ZERO6)
                self.assertTrue(result.simulation_command.stop_request)

    def test_normal_mismatch_safe_holds_exact_zero(self) -> None:
        result = adapter().step(
            state(approach_normal=(0.0, 0.0, -1.0))
        )

        self.assertEqual(result.control.decision.reason, "normal_contract_mismatch")
        self.assertEqual(result.control.decision.action, "safe_hold")
        self.assertEqual(result.simulation_command.qdot, ZERO6)
        self.assertFalse(result.simulation_command.stop_request)

    def test_ingress_fault_matrix_latches_stop_and_zeroes_sink(self) -> None:
        faults = {
            "stale": {"observation_age_s": 0.100001},
            "wrong_frame": {"frame_lineage": lineage(wrench_frame="tool")},
            "bad_hash": {"model_hash": "bad"},
            "nonfinite": {"q": (math.nan, 0.0, 0.0, 0.0, 0.0, 0.0)},
            "force": {"wrench": (5.0001, 0.0, 0.0, 0.0, 0.0, 0.0)},
            "torque": {"wrench": (0.0, 0.0, 0.0, 3.0001, 0.0, 0.0)},
            "contact": {"native_contact_count": 1},
            "collision": {"cage_collision_count": 1},
            "outside_cage": {"tcp_inside_cage": False},
        }
        for label, values in faults.items():
            with self.subTest(label=label):
                test_adapter = adapter()
                result = test_adapter.step(state(**values))
                self.assertTrue(result.control.decision.reason.startswith("simulator_ingress_failure:"))
                self.assertEqual(result.simulation_command.qdot, ZERO6)
                self.assertTrue(result.simulation_command.stop_request)
                followup = test_adapter.step(
                    state(sequence=1, sim_time_s=0.002)
                )
                self.assertTrue(followup.ingress_reason.startswith("latched:"))
                self.assertEqual(followup.simulation_command.qdot, ZERO6)

    def test_duplicate_gap_reorder_and_jitter_stop_exact_zero(self) -> None:
        scenarios = (
            (state(sequence=0, sim_time_s=0.002), "sequence_duplicate"),
            (state(sequence=2, sim_time_s=0.002), "sequence_gap"),
            (state(sequence=-1, sim_time_s=0.002), "sequence_reordered"),
            (state(sequence=1, sim_time_s=0.003), "control_tick_jitter"),
        )
        for second, expected in scenarios:
            with self.subTest(expected=expected):
                test_adapter = adapter()
                self.assertTrue(test_adapter.step(state()).control.decision.accepted)
                result = test_adapter.step(second)
                self.assertEqual(result.ingress_reason, expected)
                self.assertEqual(result.simulation_command.qdot, ZERO6)

    def test_missing_observation_after_nominal_tick_stops_exact_zero(self) -> None:
        test_adapter = adapter()
        test_adapter.step(state())

        result = test_adapter.step(None)

        self.assertEqual(result.ingress_reason, "missing_simulator_observation")
        self.assertEqual(result.simulation_command.qdot, ZERO6)
        self.assertTrue(result.simulation_command.stop_request)

    def test_qdot_rail_is_caught_after_direction_preserving_slew(self) -> None:
        test_adapter = adapter(FixedPolicy((0.051, 0.0, 0.0, 0.0, 0.0, 0.0)))
        test_adapter.previous_qdot = (0.051, 0.0, 0.0, 0.0, 0.0, 0.0)

        result = test_adapter.step(state(desired_twist=(0.051, 0.0, 0.0004, 0.0, 0.0, 0.0)))

        self.assertEqual(result.control.decision.reason, "qdot_bound_exceeded")
        self.assertEqual(result.simulation_command.qdot, ZERO6)
        self.assertTrue(result.simulation_command.stop_request)

    def test_source_sink_receives_only_final_register_qdot(self) -> None:
        class Source:
            def read_state(self) -> SimulatorState:
                return state()

        class Sink:
            command = None

            def write_command(self, command: object) -> None:
                self.command = command

        sink = Sink()
        result = adapter().run_one_tick(Source(), sink)

        self.assertEqual(sink.command, result.simulation_command)
        self.assertEqual(result.simulation_command.qdot, result.control.register_command.qdot)

    def test_simulation_claims_never_promote_live_state(self) -> None:
        boundary = simulation_claim_boundary()

        self.assertEqual(boundary["workflow_state"], "liveprep_blocked")
        self.assertEqual(boundary["current_program"], "step5d_strict_rnn_ablation_v29")
        for field in (
            "v30_active",
            "live_motion_authorized",
            "package_accepted",
            "live_accepted",
            "reproduction_complete",
        ):
            self.assertFalse(boundary[field])


if __name__ == "__main__":
    unittest.main()
