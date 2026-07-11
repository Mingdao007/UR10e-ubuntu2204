from __future__ import annotations

import unittest

from ur10e_vic.constraints import ImpedanceBounds
from ur10e_vic.math3d import pose_error
from ur10e_vic.policies import (
    DBILPrediction,
    DBILShadowPolicy,
    DirectionalStiffnessAdaptor,
    DirectionalVICPolicy,
    FixedImpedancePolicy,
    ScriptedPhase,
    ScriptedPhasePolicy,
)
from ur10e_vic.contracts import PoseSample

from helpers import observation


class _Predictor:
    def predict(self, item):
        return DBILPrediction(item.nominal_zft, 0.8, "b" * 64)


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bounds = ImpedanceBounds()

    def test_fixed_and_scripted_policies_are_non_increasing(self) -> None:
        fixed = FixedImpedancePolicy(self.bounds).propose(observation())
        self.assertTrue(fixed.shadow_only)
        scripted = ScriptedPhasePolicy(
            self.bounds,
            (ScriptedPhase(0.0, 1.0), ScriptedPhase(1.0, 0.5)),
        )
        before = scripted.propose(observation(timestamp_s=0.0))
        after = scripted.propose(observation(timestamp_s=1.0))
        self.assertTrue(
            all(new <= old for old, new in zip(before.stiffness, after.stiffness))
        )

    def test_nominal_and_dbil_use_identical_directional_adaptor(self) -> None:
        item = observation()
        deterministic = DirectionalVICPolicy(
            DirectionalStiffnessAdaptor(self.bounds)
        ).propose(item)
        dbil = DBILShadowPolicy(
            DirectionalStiffnessAdaptor(self.bounds), _Predictor()
        ).propose(item)
        self.assertEqual(deterministic.stiffness, dbil.stiffness)
        self.assertEqual(dbil.source, "dbil_shadow")
        self.assertTrue(dbil.shadow_only)

    def test_quaternion_sign_does_not_change_pose_error(self) -> None:
        target_a = PoseSample((0, 0, 0), (0.0, 1.0, 0.0, 0.0))
        target_b = PoseSample((0, 0, 0), (0.0, -1.0, 0.0, 0.0))
        current = PoseSample((0, 0, 0), (1.0, 0.0, 0.0, 0.0))
        self.assertEqual(pose_error(target_a, current), pose_error(target_b, current))

    def test_dbil_prediction_failure_is_invalid_shadow_not_fallback(self) -> None:
        class Broken:
            def predict(self, item):
                raise RuntimeError("model unavailable")

        proposal = DBILShadowPolicy(
            DirectionalStiffnessAdaptor(self.bounds), Broken()
        ).propose(observation())
        self.assertFalse(proposal.valid)
        self.assertTrue(proposal.shadow_only)
        self.assertEqual(proposal.source, "dbil_shadow_error")


if __name__ == "__main__":
    unittest.main()
