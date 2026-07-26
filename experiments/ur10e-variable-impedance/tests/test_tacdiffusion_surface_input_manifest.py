import json
import math
from pathlib import Path
import unittest

from ur10e_vic.tacdiffusion.surface_input_manifest import (
    EXPECTED_CORNER_ORDER,
    assess_surface_input,
    load_surface_input_manifest,
    rotvec_to_quaternion_xyzw,
    to_planar_surface_calibration,
)


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
        self.assertFalse(payload["status"]["cad_to_base_transform_validated"])
        self.assertFalse(payload["status"]["live_or_contact_ready"])
        self.assertEqual(len(payload["stl_candidates"]), 2)
        self.assertTrue(all(candidate["native_units"] == "mm" for candidate in payload["stl_candidates"]))


if __name__ == "__main__":
    unittest.main()
