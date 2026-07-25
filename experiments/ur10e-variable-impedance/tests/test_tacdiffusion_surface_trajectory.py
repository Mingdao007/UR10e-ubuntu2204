import math
import unittest

from ur10e_vic.tacdiffusion.surface import SurfacePose, calibrate_surface
from ur10e_vic.tacdiffusion.trajectory import (
    TRAJECTORY_FAMILIES,
    episode_plan,
    generate_bounded_trajectory,
)


def square_corners(width=0.12, height=0.08):
    q = (0.0, 0.0, 0.0, 1.0)
    return (
        SurfacePose((-width / 2, -height / 2, 0.2), q),
        SurfacePose((width / 2, -height / 2, 0.2), q),
        SurfacePose((width / 2, height / 2, 0.2), q),
        SurfacePose((-width / 2, height / 2, 0.2), q),
    )


class SurfaceCalibrationTests(unittest.TestCase):
    def test_calibration_exposes_safe_frame_and_explicit_normal_roles(self):
        surface = calibrate_surface(square_corners(), safe_inset_m=0.01)
        self.assertEqual(surface.schema_version, "ur10e_surface_calibration/v1")
        self.assertAlmostEqual(surface.width_m, 0.12)
        self.assertAlmostEqual(surface.height_m, 0.08)
        self.assertEqual(surface.reaction_normal_base, (0.0, 0.0, 1.0))
        self.assertEqual(surface.approach_normal_base, (0.0, 0.0, -1.0))
        for actual, expected in zip(surface.normalized_to_base(1.0, -1.0), (0.05, -0.03, 0.2)):
            self.assertAlmostEqual(actual, expected)
        with self.assertRaisesRegex(ValueError, "safe inset"):
            surface.normalized_to_base(1.01, 0.0)

    def test_order_nonplanar_and_inconsistent_orientation_fail_closed(self):
        corners = square_corners()
        with self.assertRaisesRegex(ValueError, "rectang"):
            calibrate_surface((corners[0], corners[1], corners[3], corners[2]))
        nonplanar = list(corners)
        nonplanar[2] = SurfacePose((0.06, 0.04, 0.201), corners[2].orientation_xyzw)
        with self.assertRaisesRegex(ValueError, "coplanar"):
            calibrate_surface(nonplanar, planarity_tolerance_m=1e-5)
        tilted = list(corners)
        tilted[2] = SurfacePose(tilted[2].position_base_m, (0.0, math.sin(0.2), 0.0, math.cos(0.2)))
        with self.assertRaisesRegex(ValueError, "orientations"):
            calibrate_surface(tilted)


class BoundedTrajectoryTests(unittest.TestCase):
    def test_all_locked_families_are_seeded_bounded_and_replayable(self):
        surface = calibrate_surface(square_corners(), safe_inset_m=0.01)
        for family in TRAJECTORY_FAMILIES:
            first = generate_bounded_trajectory(surface, family=family, seed=7, duration_s=8.0, rate_hz=100)
            second = generate_bounded_trajectory(surface, family=family, seed=7, duration_s=8.0, rate_hz=100)
            self.assertEqual(first, second)
            self.assertEqual(len(first.samples), 801)
            self.assertTrue(all(-1.0 <= sample.u_normalized <= 1.0 for sample in first.samples))
            self.assertTrue(all(-1.0 <= sample.v_normalized <= 1.0 for sample in first.samples))
            positions = first.base_positions(surface)
            self.assertTrue(all(math.isfinite(value) for point in positions for value in point))
            self.assertLessEqual(first.max_speed_m_s, 0.04 + 1e-9)
            self.assertLessEqual(first.max_acceleration_m_s2, 0.8 + 1e-9)
            self.assertLessEqual(first.max_curvature_m_inv, 5000.0 + 1e-9)
            self.assertTrue(all(sample.curvature_m_inv >= 0.0 for sample in first.samples))

    def test_episode_plan_covers_families_and_changes_safe_profile(self):
        plan = episode_plan(50, seed=123)
        self.assertEqual(len(plan), 50)
        self.assertEqual({family for _, family, _ in plan}, set(TRAJECTORY_FAMILIES))
        self.assertGreater(len({profile.speed_scale for _, _, profile in plan}), 1)
        self.assertTrue(all(profile.normal_force_target_n >= profile.preload_n for _, _, profile in plan))

    def test_too_fast_path_is_rejected_instead_of_clipping_dynamics(self):
        surface = calibrate_surface(square_corners(), safe_inset_m=0.01)
        with self.assertRaisesRegex(ValueError, "speed bound"):
            generate_bounded_trajectory(surface, family="circle", seed=1, duration_s=0.05, rate_hz=100)

    def test_physical_metrics_and_curvature_limit_are_configurable(self):
        surface = calibrate_surface(square_corners(), safe_inset_m=0.01)
        trajectory = generate_bounded_trajectory(surface, family="circle", seed=1, duration_s=8.0, rate_hz=100)
        sample = trajectory.samples[len(trajectory.samples) // 4]
        self.assertGreater(sample.speed_m_s, 0.0)
        self.assertGreater(sample.acceleration_m_s2, 0.0)
        self.assertGreater(sample.curvature_m_inv, 0.0)
        with self.assertRaisesRegex(ValueError, "curvature bound"):
            generate_bounded_trajectory(surface, family="circle", seed=1, duration_s=8.0, rate_hz=100, max_curvature_m_inv=1.0)

    def test_surface_dimension_and_inset_boundary_are_fail_closed(self):
        q = (0.0, 0.0, 0.0, 1.0)
        tiny = square_corners(width=0.01, height=0.01)
        with self.assertRaisesRegex(ValueError, "insufficient"):
            calibrate_surface(tiny, safe_inset_m=0.005)
        surface = calibrate_surface(square_corners(), safe_inset_m=0.01)
        boundary = surface.normalized_to_base(1.0, 1.0)
        self.assertTrue(all(math.isfinite(value) for value in boundary))
        with self.assertRaisesRegex(ValueError, "safe inset"):
            surface.local_to_base(surface.safe_u_bounds_m[1] + 1e-5, 0.0)


if __name__ == "__main__":
    unittest.main()
