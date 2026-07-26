from __future__ import annotations

import math
import unittest

from ur10e_vic.backends import Step5bTwistInput, VelocityAdmittanceSurrogate
from ur10e_vic.constraints import ImpedanceBounds, ProposalSupervisor, derive_damping
from ur10e_vic.policies import (
    DBILPrediction,
    DBILShadowPolicy,
    DirectionalStiffnessAdaptor,
    DirectionalVICPolicy,
    FixedImpedancePolicy,
    ScriptedPhase,
    ScriptedPhasePolicy,
)
from ur10e_vic.simulation import VICSimulatorAdapter

from helpers import observation


BASELINE_K = (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
SCAFFOLD_HASH = "e" * 64


class _NominalPredictor:
    def predict(self, item):
        return DBILPrediction(item.nominal_zft, 1.0, "d" * 64)


class SimulationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bounds = ImpedanceBounds()

    def _policy(self, name: str):
        if name == "fixed":
            return FixedImpedancePolicy(self.bounds)
        if name == "scripted":
            return ScriptedPhasePolicy(
                self.bounds,
                (ScriptedPhase(0.0, 1.0), ScriptedPhase(0.005, 0.5)),
            )
        if name == "deterministic":
            return DirectionalVICPolicy(DirectionalStiffnessAdaptor(self.bounds))
        if name == "dbil":
            return DBILShadowPolicy(
                DirectionalStiffnessAdaptor(self.bounds), _NominalPredictor()
            )
        raise AssertionError(name)

    def _supervisor(self) -> ProposalSupervisor:
        return ProposalSupervisor(BASELINE_K, self.bounds, 0.005)

    @staticmethod
    def _backend() -> VelocityAdmittanceSurrogate:
        return VelocityAdmittanceSurrogate(
            step5b_scaffold_sha256=SCAFFOLD_HASH,
            step5b_activation_ready=True,
        )

    @staticmethod
    def _step5b(item) -> Step5bTwistInput:
        return Step5bTwistInput(
            sequence=item.sequence,
            generated_at_s=item.timestamp_s,
            twist_base=(0.001, 0.0002, 0.0, 0.01, 0.0, -0.01),
            scaffold_sha256=SCAFFOLD_HASH,
        )

    def test_full_adapter_shadow_on_off_is_bit_for_bit_command_invariant(self) -> None:
        commands = (
            (0.001, -0.0, 0.003, -0.004, 0.005, -0.006),
            (0.0011, -0.002, 0.0031, -0.004, 0.005, -0.006),
            (0.0012, -0.002, 0.0032, -0.004, 0.005, -0.006),
        )
        for policy_name in ("fixed", "scripted", "deterministic", "dbil"):
            with self.subTest(policy=policy_name):
                off = VICSimulatorAdapter(
                    self._policy(policy_name),
                    self._supervisor(),
                    self._backend(),
                    execution_mode="baseline",
                )
                on = VICSimulatorAdapter(
                    self._policy(policy_name),
                    self._supervisor(),
                    self._backend(),
                    execution_mode="shadow",
                )
                for index, baseline in enumerate(commands):
                    item = observation(timestamp_s=index * 0.005)
                    baseline_valid = index != 1
                    off_result = off.tick(
                        item, baseline, baseline_valid=baseline_valid
                    )
                    on_result = on.tick(
                        item, baseline, baseline_valid=baseline_valid
                    )
                    self.assertEqual(
                        off_result.actual_command_bit_pattern,
                        on_result.actual_command_bit_pattern,
                    )
                    self.assertEqual(on_result.route, "baseline_shadow_bypass")
                    self.assertFalse(on_result.dbil_active_enabled)
                    self.assertFalse(on_result.live_motion_authorized)

    def test_only_three_deterministic_policies_have_simulation_active_capability(self) -> None:
        for policy_name in ("fixed", "scripted", "deterministic"):
            with self.subTest(policy=policy_name):
                adapter = VICSimulatorAdapter(
                    self._policy(policy_name),
                    self._supervisor(),
                    self._backend(),
                    execution_mode="simulation_active",
                    simulation_active_capability=True,
                )
                previous = BASELINE_K
                for tick in range(2):
                    item = observation(timestamp_s=tick * 0.005)
                    result = adapter.tick(
                        item,
                        (0.0,) * 6,
                        dt_s=0.005,
                        step5b_input=self._step5b(item),
                    )
                    self.assertTrue(result.actual_command_valid, result.route)
                    self.assertEqual(result.route, "active_backend")
                    self.assertIsNotNone(result.proposal)
                    stiffness = result.proposal.stiffness
                    damping = result.proposal.damping
                    self.assertEqual(stiffness[3:], BASELINE_K[3:])
                    self.assertTrue(
                        all(math.isfinite(value) for value in stiffness + damping)
                    )
                    self.assertTrue(all(value >= 0.0 for value in stiffness + damping))
                    self.assertTrue(
                        all(
                            self.bounds.minimum[index]
                            <= stiffness[index]
                            <= self.bounds.maximum[index]
                            for index in range(6)
                        )
                    )
                    self.assertEqual(damping, derive_damping(stiffness, self.bounds))
                    self.assertTrue(
                        all(stiffness[index] <= previous[index] for index in range(6))
                    )
                    self.assertTrue(
                        all(
                            previous[index] - stiffness[index]
                            <= self.bounds.max_slew_per_s[index] * 0.005 + 1e-12
                            for index in range(6)
                        )
                    )
                    previous = stiffness

        with self.assertRaisesRegex(ValueError, "explicit capability"):
            VICSimulatorAdapter(
                self._policy("fixed"),
                self._supervisor(),
                self._backend(),
                execution_mode="simulation_active",
            )
        with self.assertRaisesRegex(ValueError, "permanently shadow-only"):
            VICSimulatorAdapter(
                self._policy("dbil"),
                self._supervisor(),
                self._backend(),
                execution_mode="simulation_active",
                simulation_active_capability=True,
            )

    def test_stale_active_proposal_ramps_translation_to_safe_low_then_stops(self) -> None:
        adapter = VICSimulatorAdapter(
            self._policy("fixed"),
            self._supervisor(),
            self._backend(),
            execution_mode="simulation_active",
            simulation_active_capability=True,
        )
        first_item = observation(timestamp_s=0.0)
        first = adapter.tick(
            first_item,
            (0.0,) * 6,
            step5b_input=self._step5b(first_item),
        )
        self.assertTrue(first.actual_command_valid)

        for timestamp_s in (0.005, 0.010):
            item = observation(timestamp_s=timestamp_s)
            held = adapter.tick(
                item,
                (0.0,) * 6,
                step5b_input=self._step5b(item),
                policy_sample_available=False,
            )
            self.assertEqual(held.supervision.mode, "hold")
            self.assertTrue(held.actual_command_valid)

        stale_item = observation(timestamp_s=0.010001)
        stale = adapter.tick(
            stale_item,
            (0.0,) * 6,
            policy_sample_available=False,
        )
        self.assertEqual(stale.supervision.mode, "damping_only")
        self.assertTrue(stale.supervision.request_stop)
        self.assertIsNone(stale.actual_command)

        final = stale
        for tick in range(1, 282):
            item = observation(timestamp_s=0.010001 + tick * 0.005)
            final = adapter.tick(
                item,
                (0.0,) * 6,
                policy_sample_available=False,
            )
        self.assertEqual(final.supervision.mode, "stop")
        self.assertEqual(final.proposal.stiffness[:3], self.bounds.safe_low[:3])
        self.assertEqual(final.proposal.stiffness[3:], BASELINE_K[3:])
        self.assertEqual(
            final.proposal.damping,
            derive_damping(final.proposal.stiffness, self.bounds),
        )
        self.assertFalse(final.actual_command_valid)
        self.assertIsNone(final.actual_command)


if __name__ == "__main__":
    unittest.main()
