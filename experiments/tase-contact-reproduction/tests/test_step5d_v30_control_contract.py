#!/usr/bin/env python3
"""Offline contract tests for the Step5d v30 control pipeline."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_control_contract import (  # noqa: E402
    JOINT_LAYOUT_CODE,
    STRICT_RNN_SOLVER_OK_STATUS,
    ControlCandidate,
    ControlPolicy,
    DeferredV30Diagnostics,
    SafetyDecision,
    SafetyEnvelope,
    Step5dObservation,
    StrictRnnControlPolicy,
    apply_direction_preserving_slew,
    compute_dls_shadow,
    decision_to_register_command,
)


def observation(**overrides: object) -> Step5dObservation:
    values: dict[str, object] = {
        "sequence": 7,
        "timestamp_s": 1.25,
        "q": (0.0,) * 6,
        "qd": (0.0,) * 6,
        "tcp_pose": (0.0,) * 6,
        "tcp_twist": (0.0,) * 6,
        "wrench": (0.0,) * 6,
        "jacobian": tuple(tuple(float(i == j) for j in range(6)) for i in range(6)),
        "desired_twist": (0.0, 0.0, 0.001, 0.0, 0.0, 0.0),
        "reaction_normal": (0.0, 0.0, -1.0),
        "approach_normal": (0.0, 0.0, 1.0),
        "command_frame": "base",
        "normal_frame": "base",
        "normal_to_command_rotation": None,
    }
    values.update(overrides)
    return Step5dObservation(**values)  # type: ignore[arg-type]


def candidate(**overrides: object) -> ControlCandidate:
    values: dict[str, object] = {
        "qdot": (0.0, 0.0, 0.001, 0.0, 0.0, 0.0),
        "predicted_twist": (0.0, 0.0, 0.001, 0.0, 0.0, 0.0),
        "residual_norm": 0.0,
        "active_bounds_count": 0,
        "frame_id": "base",
        "solver_status": str(STRICT_RNN_SOLVER_OK_STATUS),
        "diagnostics": {"backend": "cupy", "inner_iterations": 1024},
    }
    values.update(overrides)
    return ControlCandidate(**values)  # type: ignore[arg-type]


class Step5dV30ControlContractTest(unittest.TestCase):
    def test_strict_rnn_policy_implements_public_control_policy(self) -> None:
        class FakeSolver:
            config = SimpleNamespace(epsilon=0.01, sigr_exponent_r=0.8)

            def solve(self, **kwargs: object) -> SimpleNamespace:
                target = kwargs["target_state"]  # type: ignore[index]
                self.target = target
                return SimpleNamespace(
                    qdot=(0.0, 0.0, 0.001, 0.0, 0.0, 0.0),
                    residual_norm=0.0,
                    solver_status=40.0,
                    diagnostics={"active_bounds_mask": (False,) * 6},
                )

        obs = observation(
            omega_minus=(-0.05,) * 6,
            omega_plus=(0.05,) * 6,
        )
        policy = StrictRnnControlPolicy(FakeSolver())

        result = policy.compute(obs)

        self.assertIsInstance(policy, ControlPolicy)
        self.assertEqual(result.frame_id, "base")
        self.assertEqual(result.predicted_twist[2], 0.001)
        self.assertEqual(result.active_bounds_count, 0)

    def test_normal_contract_accepts_reaction_negative_approach(self) -> None:
        decision = SafetyEnvelope().evaluate(observation(), candidate())

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.action, "execute")
        self.assertEqual(decision.reason, "ok")
        self.assertGreater(decision.metrics["desired_approach_m_s"], 0.0)
        self.assertGreater(decision.metrics["predicted_approach_m_s"], 0.0)

    def test_invalid_strict_rnn_solver_status_stops_before_register_command(self) -> None:
        for invalid_status in ("strict_rnn_invalid", "91.0", "nan"):
            with self.subTest(invalid_status=invalid_status):
                decision = SafetyEnvelope().evaluate(
                    observation(), candidate(solver_status=invalid_status)
                )
                command = decision_to_register_command(observation(), decision)

                self.assertFalse(decision.accepted)
                self.assertEqual(decision.action, "stop")
                self.assertEqual(decision.reason, "strict_rnn_solver_status_invalid")
                self.assertTrue(command.stop_request)
                self.assertFalse(command.cmd_valid)
                self.assertEqual(command.qdot, (0.0,) * 6)

    def test_normal_contract_fails_closed_when_signs_are_not_opposites(self) -> None:
        obs = observation(approach_normal=(0.0, 0.0, -1.0))

        decision = SafetyEnvelope().evaluate(obs, candidate())

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.action, "safe_hold")
        self.assertEqual(decision.reason, "normal_contract_mismatch")

    def test_frame_mismatch_without_rotation_fails_closed(self) -> None:
        obs = observation(normal_frame="tool")

        decision = SafetyEnvelope().evaluate(obs, candidate())

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "missing_normal_frame_transform")

    def test_frame_transform_is_applied_before_direction_guard(self) -> None:
        # tool +X maps to base +Z, so tool reaction -X maps to base -Z.
        rotation = (
            (0.0, 0.0, -1.0),
            (0.0, 1.0, 0.0),
            (1.0, 0.0, 0.0),
        )
        obs = observation(
            reaction_normal=(-1.0, 0.0, 0.0),
            approach_normal=(1.0, 0.0, 0.0),
            normal_frame="tool",
            normal_to_command_rotation=rotation,
        )

        decision = SafetyEnvelope().evaluate(obs, candidate())

        self.assertTrue(decision.accepted)
        self.assertEqual(decision.metrics["frame_transform_applied"], 1.0)

    def test_reversed_unload_is_safe_held_not_executed(self) -> None:
        reversed_candidate = candidate(
            qdot=(0.0, 0.0, -0.001, 0.0, 0.0, 0.0),
            predicted_twist=(0.0, 0.0, -0.001, 0.0, 0.0, 0.0),
        )

        decision = SafetyEnvelope().evaluate(observation(), reversed_candidate)
        command = decision_to_register_command(observation(), decision)

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "approach_normal_unload_mismatch")
        self.assertEqual(command.qdot, (0.0,) * 6)
        self.assertTrue(command.cmd_valid)
        self.assertEqual(command.layout_code, JOINT_LAYOUT_CODE)
        self.assertFalse(command.stop_request)

    def test_nonfinite_candidate_requests_stop_and_zero_command(self) -> None:
        bad = candidate(qdot=(math.nan, 0.0, 0.0, 0.0, 0.0, 0.0))

        decision = SafetyEnvelope().evaluate(observation(), bad)
        command = decision_to_register_command(observation(), decision)

        self.assertEqual(decision.action, "stop")
        self.assertEqual(decision.reason, "nonfinite_or_bad_shape")
        self.assertTrue(command.stop_request)
        np.testing.assert_allclose(command.qdot, np.zeros(6))

    def test_qdot_bound_and_residual_are_fail_closed(self) -> None:
        envelope = SafetyEnvelope(qdot_cap_rad_s=0.05, max_residual_norm=1e-3)

        over_bound = envelope.evaluate(
            observation(),
            candidate(
                qdot=(0.051, 0.0, 0.0, 0.0, 0.0, 0.0),
                predicted_twist=(0.051, 0.0, 0.0, 0.0, 0.0, 0.0),
            ),
        )
        over_residual = envelope.evaluate(observation(), candidate(residual_norm=1.1e-3))

        self.assertEqual(over_bound.reason, "qdot_bound_exceeded")
        self.assertEqual(over_bound.action, "stop")
        self.assertEqual(over_residual.reason, "constraint_residual_norm_exceeded")
        self.assertEqual(over_residual.action, "safe_hold")

    def test_direction_preserving_slew_does_not_create_component_clip_unload(self) -> None:
        jacobian = np.eye(6)
        jacobian[2, 4] = -2.0
        obs = observation(
            jacobian=tuple(tuple(float(value) for value in row) for row in jacobian),
            desired_twist=(0.0, 0.0, 0.0001, 0.0, 0.0, 0.0),
        )
        raw_qdot = np.array([0.0, 0.0, 0.0011, 0.0, 0.0005, 0.0])
        raw = candidate(
            qdot=tuple(float(value) for value in raw_qdot),
            predicted_twist=tuple(float(value) for value in jacobian @ raw_qdot),
        )

        component_clipped = np.clip(raw_qdot, -0.0004, 0.0004)
        slewed = apply_direction_preserving_slew(obs, raw)
        decision = SafetyEnvelope().evaluate(obs, slewed)

        self.assertLess(float((jacobian @ component_clipped)[2]), 0.0)
        self.assertGreater(slewed.predicted_twist[2], 0.0)
        self.assertTrue(slewed.diagnostics["slew_active"])
        self.assertAlmostEqual(
            slewed.residual_norm,
            float(
                np.linalg.norm(
                    np.asarray(slewed.predicted_twist)
                    - np.asarray(obs.desired_twist)
                )
            ),
        )
        self.assertNotEqual(slewed.residual_norm, raw.residual_norm)
        self.assertTrue(decision.accepted)

    def test_register_command_sanitizes_nonfinite_diagnostic_scalars(self) -> None:
        obs = observation(
            sequence=-7,
            path_time_s=math.nan,
            force_error_n=math.inf,
            orientation_error_rad=-math.inf,
        )
        decision = SafetyDecision(
            accepted=False,
            action="stop",
            reason="synthetic_structural_failure",
            qdot=(math.nan,) * 6,
            metrics={},
        )

        command = decision_to_register_command(obs, decision)
        registers = command.as_register_values()

        self.assertEqual(command.heartbeat, 0.0)
        self.assertEqual(command.qdot, (0.0,) * 6)
        self.assertEqual(command.path_time_s, 0.0)
        self.assertEqual(command.force_error_n, 0.0)
        self.assertEqual(command.orientation_error_rad, 0.0)
        self.assertTrue(command.stop_request)
        self.assertTrue(all(math.isfinite(value) for value in registers.values()))

    def test_deferred_diagnostics_is_bounded_lossless_and_fail_closed(self) -> None:
        obs = observation()
        proposal = candidate()
        decision = SafetyEnvelope().evaluate(obs, proposal)
        command = decision_to_register_command(obs, decision)
        deferred = DeferredV30Diagnostics(capacity=2)

        self.assertTrue(deferred.record(obs, proposal, decision, command))
        self.assertTrue(deferred.record(obs, proposal, decision, command))
        self.assertFalse(deferred.record(obs, proposal, decision, command))

        self.assertEqual(deferred.count, 2)
        self.assertTrue(deferred.overflowed)
        self.assertEqual(deferred.reasons[:2], ["ok", "ok"])
        fields = deferred._field_index
        self.assertEqual(deferred.numeric[0, fields["register_43"]], 1.0)
        self.assertEqual(deferred.numeric[0, fields["register_47"]], JOINT_LAYOUT_CODE)

    def test_dls_shadow_is_command_inert_and_forbidden_as_fallback(self) -> None:
        obs = observation(
            omega_minus=(-0.05,) * 6,
            omega_plus=(0.05,) * 6,
        )
        proposal = candidate()
        decision = SafetyEnvelope().evaluate(obs, proposal)
        command_before = decision_to_register_command(obs, decision)

        shadow = compute_dls_shadow(obs, proposal)
        deferred = DeferredV30Diagnostics(capacity=1)
        self.assertTrue(deferred.record(obs, proposal, decision, command_before, shadow))
        command_after = decision_to_register_command(obs, decision)

        self.assertEqual(command_after, command_before)
        self.assertFalse(shadow.runtime_fallback_allowed)
        self.assertEqual(shadow.normal_sign_difference, False)
        self.assertEqual(
            deferred.numeric[0, deferred._field_index["dls_shadow_normal_sign_difference"]],
            0.0,
        )

    def test_dls_shadow_uses_same_normal_frame_transform_as_safety(self) -> None:
        rotation = (
            (0.0, 0.0, -1.0),
            (0.0, 1.0, 0.0),
            (1.0, 0.0, 0.0),
        )
        obs = observation(
            reaction_normal=(-1.0, 0.0, 0.0),
            approach_normal=(1.0, 0.0, 0.0),
            normal_frame="tool",
            normal_to_command_rotation=rotation,
            omega_minus=(-0.05,) * 6,
            omega_plus=(0.05,) * 6,
        )

        decision = SafetyEnvelope().evaluate(obs, candidate())
        shadow = compute_dls_shadow(obs, candidate())

        self.assertTrue(decision.accepted)
        self.assertAlmostEqual(shadow.desired_approach_m_s, 0.001)
        self.assertAlmostEqual(shadow.dls_approach_m_s, 0.001, places=8)


if __name__ == "__main__":
    unittest.main()
