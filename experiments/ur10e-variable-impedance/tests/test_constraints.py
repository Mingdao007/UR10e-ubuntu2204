from __future__ import annotations

import math
import unittest

from ur10e_vic.constraints import (
    ImpedanceBounds,
    ProposalSupervisor,
    derive_damping,
    limit_stiffness,
)
from ur10e_vic.contracts import ImpedanceProposal, PoseSample


class ConstraintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bounds = ImpedanceBounds()
        self.zft = PoseSample((0, 0, 0), (1, 0, 0, 0))

    def test_run_stiffness_cannot_increase_and_is_slew_limited(self) -> None:
        previous = (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
        increased = limit_stiffness((900,) * 6, previous, 0.005, self.bounds)
        self.assertEqual(increased, previous)
        decreased = limit_stiffness(
            (40.0, 40.0, 40.0, 1.0, 1.0, 1.0),
            previous,
            0.005,
            self.bounds,
        )
        self.assertEqual(decreased[:3], (598.0, 598.0, 598.0))
        self.assertEqual(decreased[3:], (29.9, 29.9, 29.9))

    def test_damping_tracks_square_root_of_stiffness(self) -> None:
        low = derive_damping((100,) * 6, self.bounds)
        high = derive_damping((400,) * 6, self.bounds)
        for low_value, high_value in zip(low, high):
            self.assertAlmostEqual(high_value / low_value, 2.0)

    def test_stale_proposal_holds_two_periods_then_requests_stop(self) -> None:
        initial = (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
        supervisor = ProposalSupervisor(initial, self.bounds, 0.005)
        proposal = ImpedanceProposal(
            generated_at_s=0.0,
            s_zft=self.zft,
            stiffness=(500.0, 500.0, 500.0, 30.0, 30.0, 30.0),
            damping=derive_damping((500, 500, 500, 30, 30, 30), self.bounds),
            confidence=1.0,
            age_s=0.0,
            source="fixed",
            model_hash="",
        )
        fresh = supervisor.step(
            proposal, now_s=0.0, dt_s=0.005, fallback_zft=self.zft
        )
        self.assertEqual(fresh.mode, "tracking")
        held = supervisor.step(
            None, now_s=0.010, dt_s=0.005, fallback_zft=self.zft
        )
        self.assertEqual(held.mode, "hold")
        stale = supervisor.step(
            None, now_s=0.010001, dt_s=0.005, fallback_zft=self.zft
        )
        self.assertEqual(stale.mode, "damping_only")
        self.assertTrue(stale.request_stop)
        self.assertFalse(stale.proposal.valid)
        self.assertTrue(math.isinf(stale.proposal.age_s) is False)

    def test_missing_first_proposal_fails_closed(self) -> None:
        supervisor = ProposalSupervisor(
            (600.0, 600.0, 600.0, 30.0, 30.0, 30.0),
            self.bounds,
            0.005,
        )
        decision = supervisor.step(
            None, now_s=0.0, dt_s=0.005, fallback_zft=self.zft
        )
        self.assertTrue(decision.request_stop)
        self.assertFalse(decision.proposal.valid)
        self.assertEqual(decision.mode, "damping_only")

    def test_phase1_supervisor_rejects_rotational_stiffness_change(self) -> None:
        initial = (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
        supervisor = ProposalSupervisor(initial, self.bounds, 0.005)
        changed = (500.0, 500.0, 500.0, 29.0, 30.0, 30.0)
        proposal = ImpedanceProposal(
            generated_at_s=0.0,
            s_zft=self.zft,
            stiffness=changed,
            damping=derive_damping(changed, self.bounds),
            confidence=1.0,
            age_s=0.0,
            source="scripted_phase:0.000000",
            model_hash="",
        )
        decision = supervisor.step(
            proposal, now_s=0.0, dt_s=0.005, fallback_zft=self.zft
        )
        self.assertTrue(decision.request_stop)
        self.assertEqual(decision.mode, "damping_only")
        self.assertEqual(decision.proposal.stiffness[3:], initial[3:])


if __name__ == "__main__":
    unittest.main()
