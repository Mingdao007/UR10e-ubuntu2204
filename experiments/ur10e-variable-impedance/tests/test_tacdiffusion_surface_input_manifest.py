import json
import math
from pathlib import Path
import unittest

from ur10e_vic.tacdiffusion.surface_input_manifest import (
    EXPECTED_CORNER_ORDER,
    MODEL_HEIGHT_MISMATCH_TOLERANCE_M,
    assess_surface_input,
    assess_surface_model,
    load_surface_input_manifest,
    load_surface_model_binding,
    rotvec_to_quaternion_xyzw,
    to_planar_surface_calibration,
)
from ur10e_vic.tacdiffusion.surface import SurfacePose


ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "config" / "tacdiffusion_surface_input_20260726.json"


class SurfaceInputManifestTests(unittest.TestCase):
    def test_manifest_binds_order_units_and_preprocessing(self):
        surface_input = load_surface_input_manifest(MANIFEST)
        self.assertEqual(surface_input.corner_order, EXPECTED_CORNER_ORDER)
        self.assertEqual(surface_input.frame_id, "base")
        self.assertEqual(surface_input.manifest["coordinate_convention"]["position_units"], "m")
        self.assertEqual(surface_input.manifest["coordinate_convention"]["orientation_units"], "rad")
        self.assertEqual(surface_input.manifest["coordinate_convention"]["orientation_parameterization"], "ur_rotvec")
        self.assertTrue(all(abs(sum(value * value for value in corner.orientation_xyzw) - 1.0) < 1e-12 for corner in surface_input.corners))

    def test_v11_model_hash_axes_units_and_unique_corner_mapping(self):
        binding = load_surface_model_binding(MANIFEST)
        self.assertEqual(binding.sha256, "c68d176a88260f7845f102c1dc6e9a60549d7c270738d1757050c0d79036c1cc")
        self.assertEqual(binding.triangle_count, 91200)
        self.assertEqual(binding.native_units, "mm")
        self.assertEqual(binding.scale_to_m, 0.001)
        self.assertEqual(binding.height_axis_in_stl, "y")
        self.assertEqual(binding.in_plane_axes_in_stl, ("x", "z"))
        self.assertTrue(binding.global_top_minimum_is_unique)
        self.assertEqual(binding.model_corner_points_stl_mm[0], (-111.46788787841797, 90.33544921875, 150.0))
        self.assertEqual(binding.model_corner_points_stl_mm[1][0], -36.46788787841797)
        self.assertEqual(binding.model_corner_points_stl_mm[2][2], 0.0)
        self.assertAlmostEqual(binding.predicted_delta_z_m[1], 0.010867866516113281, places=15)

    def test_v11_model_registration_is_reproducible_and_curved_ready_only(self):
        surface_input = load_surface_input_manifest(MANIFEST)
        binding = load_surface_model_binding(MANIFEST)
        assessment = assess_surface_model(surface_input, binding)
        self.assertTrue(assessment.model_match)
        self.assertAlmostEqual(assessment.height_delta_max_abs_m, 0.0007110685522228799, places=15)
        self.assertAlmostEqual(assessment.registration.rms_residual_m, 0.0021106404995174313, places=15)
        self.assertAlmostEqual(assessment.registration.max_residual_m, 0.0029824481900019013, places=15)
        self.assertEqual(len(assessment.registration.per_point_residual_m), 4)

    def test_model_height_mismatch_fails_closed(self):
        surface_input = load_surface_input_manifest(MANIFEST)
        binding = load_surface_model_binding(MANIFEST)
        changed = list(surface_input.corners)
        original = changed[1]
        changed[1] = SurfacePose(
            position_base_m=(original.position_base_m[0], original.position_base_m[1], original.position_base_m[2] + 0.002),
            orientation_xyzw=original.orientation_xyzw,
            frame_id=original.frame_id,
        )
        mismatched = surface_input.__class__(
            schema=surface_input.schema,
            frame_id=surface_input.frame_id,
            corner_order=surface_input.corner_order,
            corners=tuple(changed),
            manifest=surface_input.manifest,
        )
        with self.assertRaisesRegex(ValueError, "height deltas do not match"):
            assess_surface_model(mismatched, binding, mismatch_tolerance_m=MODEL_HEIGHT_MISMATCH_TOLERANCE_M)

    def test_captured_four_corners_fail_closed_for_planar_backend(self):
        surface_input = load_surface_input_manifest(MANIFEST)
        assessment = assess_surface_input(surface_input)
        self.assertAlmostEqual(assessment.width_m, 0.07283088946808082, places=12)
        self.assertAlmostEqual(assessment.height_m, 0.14648596369261943, places=12)
        self.assertAlmostEqual(assessment.planarity_residual_m, 0.013094908855259307, places=12)
        self.assertAlmostEqual(assessment.rectangle_closure_error_m, 0.013201937083195938, places=12)
        self.assertAlmostEqual(assessment.z_range_m, 0.013904046465960024, places=12)
        self.assertFalse(assessment.planar_contract_eligible)
        with self.assertRaisesRegex(ValueError, "not eligible for planar"):
            to_planar_surface_calibration(surface_input)

    def test_rotvec_conversion_is_unit_and_handles_pi_x_orientation(self):
        quaternion = rotvec_to_quaternion_xyzw((math.pi, 0.0, 0.0))
        self.assertAlmostEqual(sum(value * value for value in quaternion), 1.0, places=12)
        self.assertAlmostEqual(abs(quaternion[0]), 1.0, places=12)
        self.assertAlmostEqual(quaternion[3], 0.0, places=12)

    def test_manifest_is_native_json_and_records_fail_closed_status(self):
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], "ur10e_tacdiffusion_surface_input/v1")
        self.assertFalse(payload["status"]["planar_surface_ready"])
        self.assertTrue(payload["status"]["unique_v11_corner_correspondence_validated"])
        self.assertTrue(payload["status"]["v11_height_delta_model_match_validated"])
        self.assertTrue(payload["status"]["cad_to_base_rigid_fit_validated_offline"])
        self.assertTrue(payload["status"]["model_aligned_curved_surface_ready"])
        self.assertTrue(payload["status"]["cad_to_base_transform_validated"])
        self.assertFalse(payload["status"]["curved_trajectory_backend_ready"])
        self.assertFalse(payload["status"]["live_or_contact_ready"])
        self.assertEqual(len(payload["stl_candidates"]), 2)
        self.assertTrue(all(candidate["native_units"] == "mm" for candidate in payload["stl_candidates"]))


if __name__ == "__main__":
    unittest.main()
