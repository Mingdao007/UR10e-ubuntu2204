#!/usr/bin/env python3
"""Bitwise equivalence checks for the shared P0 control-tick workspace."""

from __future__ import annotations

import dataclasses
import math
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5d_control_contract as contract  # noqa: E402


IDENTITY6 = tuple(
    tuple(float(row == column) for column in range(6)) for row in range(6)
)


def observation(**overrides: object) -> contract.Step5dObservation:
    values: dict[str, object] = {
        "sequence": 17,
        "timestamp_s": 0.034,
        "q": contract.ZERO6,
        "qd": contract.ZERO6,
        "tcp_pose": contract.ZERO6,
        "tcp_twist": contract.ZERO6,
        "wrench": contract.ZERO6,
        "jacobian": IDENTITY6,
        "desired_twist": (0.0, 0.0, 0.0004, 0.0, 0.0, 0.0),
        "reaction_normal": (0.0, 0.0, -1.0),
        "approach_normal": (0.0, 0.0, 1.0),
        "command_frame": "base",
        "normal_frame": "base",
        "omega_minus": (-0.05,) * 6,
        "omega_plus": (0.05,) * 6,
        "dt_s": 0.002,
    }
    values.update(overrides)
    return contract.Step5dObservation(**values)  # type: ignore[arg-type]


def candidate(
    qdot: tuple[float, ...] = (0.0, 0.0, 0.0004, 0.0, 0.0, 0.0),
    **overrides: object,
) -> contract.ControlCandidate:
    values: dict[str, object] = {
        "qdot": qdot,
        "predicted_twist": qdot,
        "residual_norm": 0.0,
        "active_bounds_count": 0,
        "frame_id": "base",
        "solver_status": str(contract.STRICT_RNN_SOLVER_OK_STATUS),
        "diagnostics": {
            "backend": "equivalence_fixture",
            "active_bounds_mask": (False,) * 6,
        },
    }
    values.update(overrides)
    return contract.ControlCandidate(**values)  # type: ignore[arg-type]


class Step5dP0HotPathEquivalenceTest(unittest.TestCase):
    def assert_pipeline_equivalent(
        self,
        obs: contract.Step5dObservation,
        raw: contract.ControlCandidate,
        previous_qdot: contract.Vector6 | None,
    ) -> None:
        reference_deferred = contract.DeferredV30Diagnostics(capacity=1)
        with mock.patch.object(
            contract,
            "_prepare_control_tick_workspace",
            return_value=None,
        ):
            reference = contract.step5d_v30_contract_pipeline(
                obs,
                raw,
                previous_qdot=previous_qdot,
                safety_envelope=contract.SafetyEnvelope(),
                deferred_diagnostics=reference_deferred,
            )

        optimized_deferred = contract.DeferredV30Diagnostics(capacity=1)
        optimized = contract.step5d_v30_contract_pipeline(
            obs,
            raw,
            previous_qdot=previous_qdot,
            safety_envelope=contract.SafetyEnvelope(),
            deferred_diagnostics=optimized_deferred,
        )

        self.assertEqual(optimized, reference)
        np.testing.assert_array_equal(
            optimized_deferred.numeric[:1],
            reference_deferred.numeric[:1],
            strict=True,
        )
        self.assertEqual(optimized_deferred.reasons, reference_deferred.reasons)
        self.assertEqual(optimized_deferred.actions, reference_deferred.actions)

    def test_seeded_nominal_commands_and_diagnostics_are_bitwise_equal(self) -> None:
        rng = np.random.default_rng(20260711)
        for _ in range(256):
            qdot_array = rng.uniform(-0.0003, 0.0003, size=6)
            qdot_array[2] = rng.uniform(0.0001, 0.0005)
            qdot = tuple(float(value) for value in qdot_array)
            obs = observation(desired_twist=qdot)
            raw = candidate(qdot=qdot, predicted_twist=qdot)
            self.assert_pipeline_equivalent(obs, raw, qdot)  # type: ignore[arg-type]

    def test_fault_decisions_and_exact_zero_commands_are_equal(self) -> None:
        tool_to_base = (
            (0.0, 0.0, -1.0),
            (0.0, 1.0, 0.0),
            (1.0, 0.0, 0.0),
        )
        cases = (
            (
                observation(
                    reaction_normal=(-1.0, 0.0, 0.0),
                    approach_normal=(1.0, 0.0, 0.0),
                    normal_frame="tool",
                    normal_to_command_rotation=tool_to_base,
                ),
                candidate(),
                contract.ZERO6,
            ),
            (observation(), candidate(solver_status="41.0"), contract.ZERO6),
            (observation(), candidate(frame_id="tool"), contract.ZERO6),
            (
                observation(approach_normal=(0.0, 0.0, -1.0)),
                candidate(),
                contract.ZERO6,
            ),
            (
                observation(desired_twist=(0.002, 0.0, 0.0004, 0.0, 0.0, 0.0)),
                candidate(),
                contract.ZERO6,
            ),
            (
                observation(desired_twist=(0.0, 0.0, 0.051, 0.0, 0.0, 0.0)),
                candidate(qdot=(0.0, 0.0, 0.051, 0.0, 0.0, 0.0)),
                (0.0, 0.0, 0.051, 0.0, 0.0, 0.0),
            ),
            (
                observation(),
                candidate(active_bounds_count=1),
                contract.ZERO6,
            ),
            (
                observation(desired_twist=(0.0, 0.0, -0.0004, 0.0, 0.0, 0.0)),
                candidate(qdot=(0.0, 0.0, -0.0004, 0.0, 0.0, 0.0)),
                (0.0, 0.0, -0.0004, 0.0, 0.0, 0.0),
            ),
            (
                observation(),
                candidate(qdot=(0.0, 0.0, -0.0004, 0.0, 0.0, 0.0)),
                (0.0, 0.0, -0.0004, 0.0, 0.0, 0.0),
            ),
            (
                observation(),
                candidate(qdot=(0.0, 0.0, 0.0011, 0.0, 0.0, 0.0)),
                (0.0, 0.0, 0.0011, 0.0, 0.0, 0.0),
            ),
            (
                observation(normal_to_command_rotation=((1.0, 0.0, 0.0),) * 3),
                candidate(),
                contract.ZERO6,
            ),
            (
                observation(omega_minus=(0.05,) * 6, omega_plus=(-0.05,) * 6),
                candidate(),
                contract.ZERO6,
            ),
            (
                observation(dt_s=0.0),
                candidate(),
                contract.ZERO6,
            ),
            (
                observation(sequence=-1),
                candidate(),
                contract.ZERO6,
            ),
            (
                observation(q=(math.nan, 0.0, 0.0, 0.0, 0.0, 0.0)),
                candidate(),
                contract.ZERO6,
            ),
            (
                observation(),
                candidate(qdot=(math.nan, 0.0, 0.0, 0.0, 0.0, 0.0)),
                contract.ZERO6,
            ),
        )
        for obs, raw, previous in cases:
            with self.subTest(raw=raw, observation=obs):
                self.assert_pipeline_equivalent(obs, raw, previous)  # type: ignore[arg-type]

    def test_seeded_rejection_properties_match_legacy_path(self) -> None:
        rng = np.random.default_rng(5656)
        for index in range(256):
            qdot_array = rng.uniform(-0.0003, 0.0003, size=6)
            qdot_array[2] = rng.uniform(0.0001, 0.0005)
            qdot = tuple(float(value) for value in qdot_array)
            obs = observation(desired_twist=qdot)
            raw = candidate(qdot=qdot, predicted_twist=qdot)
            previous: contract.Vector6 = qdot  # type: ignore[assignment]
            mutation = index % 8
            if mutation == 0:
                raw = dataclasses.replace(raw, solver_status="invalid")
            elif mutation == 1:
                raw = dataclasses.replace(raw, frame_id="tool")
            elif mutation == 2:
                obs = dataclasses.replace(obs, approach_normal=(0.0, 0.0, -1.0))
            elif mutation == 3:
                desired = list(qdot)
                desired[0] += 0.002
                obs = dataclasses.replace(obs, desired_twist=tuple(desired))  # type: ignore[arg-type]
            elif mutation == 4:
                raw = dataclasses.replace(raw, active_bounds_count=1)
            elif mutation == 5:
                rail = (0.0, 0.0, 0.051, 0.0, 0.0, 0.0)
                obs = dataclasses.replace(obs, desired_twist=rail)
                raw = dataclasses.replace(raw, qdot=rail, predicted_twist=rail)
                previous = rail
            elif mutation == 6:
                raw = dataclasses.replace(
                    raw,
                    qdot=(math.nan, 0.0, 0.0, 0.0, 0.0, 0.0),
                )
            else:
                obs = dataclasses.replace(
                    obs,
                    wrench=(math.inf, 0.0, 0.0, 0.0, 0.0, 0.0),
                )
            self.assert_pipeline_equivalent(obs, raw, previous)


if __name__ == "__main__":
    unittest.main()
