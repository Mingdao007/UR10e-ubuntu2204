#!/usr/bin/env python3
"""Unit tests for MuJoCo state and wrench adaptation."""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import ur10e_mujoco_adapter as adapter  # noqa: E402


class Ur10eMujocoAdapterTest(unittest.TestCase):
    def test_raw_parent_child_force_is_negated_to_external_reaction(self) -> None:
        force, torque = adapter.external_wrench_at_tcp(
            (0.0, 0.0, -10.0),
            (0.0, 0.0, 0.0),
            rotation_tcp_from_sensor=np.eye(3),
            sensor_to_tcp_sensor_m=(0.0, 0.0, 0.0),
        )

        np.testing.assert_allclose(force, (0.0, 0.0, 10.0))
        np.testing.assert_allclose(torque, np.zeros(3))
        reaction = force / np.linalg.norm(force)
        approach = -reaction
        np.testing.assert_allclose(reaction, (0.0, 0.0, 1.0))
        np.testing.assert_allclose(approach, (0.0, 0.0, -1.0))

    def test_off_axis_wrench_is_shifted_from_sensor_to_tcp(self) -> None:
        force, torque = adapter.external_wrench_at_tcp(
            (0.0, -10.0, 0.0),
            (0.0, 0.0, 0.0),
            rotation_tcp_from_sensor=np.eye(3),
            sensor_to_tcp_sensor_m=(0.1, 0.0, 0.0),
        )

        np.testing.assert_allclose(force, (0.0, 10.0, 0.0))
        np.testing.assert_allclose(torque, (0.0, 0.0, -1.0))

    def test_sensor_rotation_is_applied_after_sign_and_moment_shift(self) -> None:
        rotation = np.array(((0.0, -1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)))
        force, torque = adapter.external_wrench_at_tcp(
            (-2.0, 0.0, 0.0),
            (0.0, 0.0, -3.0),
            rotation_tcp_from_sensor=rotation,
            sensor_to_tcp_sensor_m=(0.0, 0.0, 0.0),
        )

        np.testing.assert_allclose(force, (0.0, 2.0, 0.0))
        np.testing.assert_allclose(torque, (0.0, 0.0, 3.0))

    def test_tcp_wrench_rotates_to_canonical_base_frame(self) -> None:
        rotation = np.diag((1.0, -1.0, -1.0))
        wrench = adapter.wrench_tcp_to_base(
            (1.0, 2.0, 3.0),
            (4.0, 5.0, 6.0),
            rotation,
        )

        self.assertEqual(wrench, (1.0, -2.0, -3.0, 4.0, -5.0, -6.0))

    def test_engine_plugin_never_calls_solver_or_safety_envelope(self) -> None:
        source = inspect.getsource(adapter)

        self.assertNotIn("StrictTaseRnnSolver", source)
        self.assertNotIn("StrictRnnControlPolicy", source)
        self.assertNotIn("SafetyEnvelope", source)
        self.assertNotIn("compute_dls_shadow", source)
        self.assertIn("step5d_tcp_jacobian_base", source)
        self.assertIn("step5d_omega_bounds", source)


if __name__ == "__main__":
    unittest.main()
