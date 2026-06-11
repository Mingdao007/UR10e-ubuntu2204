#!/usr/bin/env python3
"""Offline checks for Step4e/Step4f/Step4g bridge path references."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import kunwei_rtde_bridge as bridge  # noqa: E402


def local_coordinates(xy: tuple[float, float], anchor: tuple[float, float]) -> tuple[float, float]:
    dx = xy[0] - anchor[0]
    dy = xy[1] - anchor[1]
    along = dx * bridge.STEP4E_LINE_UNIT_XY[0] + dy * bridge.STEP4E_LINE_UNIT_XY[1]
    lateral = dx * bridge.STEP4E_LINE_PERP_XY[0] + dy * bridge.STEP4E_LINE_PERP_XY[1]
    return along, lateral


class Step4FGPathReferenceTest(unittest.TestCase):
    def test_line_reference_matches_existing_projection(self) -> None:
        pose_xy = (
            bridge.STEP4E_START_XY[0] + 0.050 * bridge.STEP4E_LINE_UNIT_XY[0] + 0.003 * bridge.STEP4E_LINE_PERP_XY[0],
            bridge.STEP4E_START_XY[1] + 0.050 * bridge.STEP4E_LINE_UNIT_XY[1] + 0.003 * bridge.STEP4E_LINE_PERP_XY[1],
        )
        ref = bridge.step4e_path_reference("line", pose_xy, 12.0)
        self.assertAlmostEqual(ref["progress"], 0.050, places=9)
        self.assertAlmostEqual(ref["path_error_xy"][0], -0.003 * bridge.STEP4E_LINE_PERP_XY[0], places=9)
        self.assertAlmostEqual(ref["path_error_xy"][1], -0.003 * bridge.STEP4E_LINE_PERP_XY[1], places=9)

    def test_cycloid_formula_and_envelope(self) -> None:
        end_ref = bridge.step4e_path_reference("cycloid", bridge.STEP4E_START_XY, 60.0)
        along, lateral = local_coordinates(end_ref["desired_xy"], bridge.STEP4E_START_XY)
        self.assertAlmostEqual(along, 0.015 * (6.0 - math.sin(6.0)), places=9)
        self.assertAlmostEqual(lateral, 0.015 * (1.0 - math.cos(6.0)), places=9)
        self.assertAlmostEqual(end_ref["progress"], 60.0, places=9)

        samples = [
            bridge.step4e_path_reference("cycloid", bridge.STEP4E_START_XY, idx * 0.1)
            for idx in range(601)
        ]
        local = [local_coordinates(sample["desired_xy"], bridge.STEP4E_START_XY) for sample in samples]
        speeds = [
            math.hypot(sample["desired_velocity_xy"][0], sample["desired_velocity_xy"][1])
            for sample in samples
        ]
        self.assertAlmostEqual(max(along for along, _ in local), 0.094191232, places=6)
        self.assertAlmostEqual(max(lateral for _, lateral in local), 0.030, places=5)
        self.assertAlmostEqual(max(speeds), 0.003, places=5)

    def test_eight_formula_and_envelope(self) -> None:
        start_ref = bridge.step4e_path_reference("eight", bridge.STEP4E_LINE_MID_XY, 0.0)
        along, lateral = local_coordinates(start_ref["desired_xy"], bridge.STEP4E_LINE_MID_XY)
        self.assertAlmostEqual(along, 0.0, places=9)
        self.assertAlmostEqual(lateral, 0.0, places=9)
        self.assertAlmostEqual(math.hypot(*start_ref["desired_velocity_xy"]), math.hypot(0.004, 0.002), places=9)

        samples = [
            bridge.step4e_path_reference("eight", bridge.STEP4E_LINE_MID_XY, idx * 0.1)
            for idx in range(601)
        ]
        local = [local_coordinates(sample["desired_xy"], bridge.STEP4E_LINE_MID_XY) for sample in samples]
        speeds = [
            math.hypot(sample["desired_velocity_xy"][0], sample["desired_velocity_xy"][1])
            for sample in samples
        ]
        self.assertAlmostEqual(max(along for along, _ in local), 0.040, places=5)
        self.assertAlmostEqual(min(along for along, _ in local), -0.040, places=5)
        self.assertAlmostEqual(max(lateral for _, lateral in local), 0.010, places=5)
        self.assertAlmostEqual(min(lateral for _, lateral in local), -0.010, places=5)
        self.assertAlmostEqual(max(speeds), math.hypot(0.004, 0.002), places=5)


if __name__ == "__main__":
    unittest.main()
