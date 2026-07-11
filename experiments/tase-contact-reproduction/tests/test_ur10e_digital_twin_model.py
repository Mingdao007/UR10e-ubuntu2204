#!/usr/bin/env python3
"""Dependency-light tests for calibrated MuJoCo model generation."""

from __future__ import annotations

import json
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_ur10e_digital_twin_model import (  # noqa: E402
    JOINT_NAMES,
    box_diagonal_inertia,
    fixed_transform,
    patch_mjcf,
    validate_inputs,
)


def canonical_mjcf() -> str:
    bodies = ""
    cursor = "<body name='wrist_3_link'/>"
    return f"""
<mujoco model="calibrated">
  <compiler angle="radian"/>
  <asset/>
  <worldbody>
    <body name="shoulder_link">
      <joint name="shoulder_pan_joint"/>
      <body name="upper_arm_link">
        <joint name="shoulder_lift_joint"/>
        <body name="forearm_link">
          <joint name="elbow_joint"/>
          <body name="wrist_1_link">
            <joint name="wrist_1_joint"/>
            <body name="wrist_2_link">
              <joint name="wrist_2_joint"/>
              <body name="wrist_3_link">
                <joint name="wrist_3_joint"/>
              </body>
            </body>
          </body>
        </body>
      </body>
    </body>
  </worldbody>
</mujoco>
"""


def fixed_urdf() -> str:
    return """
<robot name="ur10e">
  <link name="wrist_3_link"/>
  <link name="flange"/>
  <link name="tool0"/>
  <joint name="wrist_3-flange" type="fixed">
    <parent link="wrist_3_link"/><child link="flange"/>
    <origin xyz="0 0 0" rpy="0 -1.5707963267948966 -1.5707963267948966"/>
  </joint>
  <joint name="flange-tool0" type="fixed">
    <parent link="flange"/><child link="tool0"/>
    <origin xyz="0 0 0" rpy="1.5707963267948966 0 1.5707963267948966"/>
  </joint>
</robot>
"""


def config() -> dict[str, object]:
    value = json.loads(
        (ROOT / "config" / "digital_twin_model_inputs_v1.json").read_text(
            encoding="utf-8"
        )
    )
    value["initial_q"] = [0.0] * 6
    return value


class Ur10eDigitalTwinModelTest(unittest.TestCase):
    def test_input_manifest_hashes_and_claim_boundary_are_valid(self) -> None:
        value = config()

        validate_inputs(value)

        boundary = value["claim_boundary"]
        self.assertFalse(boundary["calibrated_physics_claim_allowed"])
        self.assertFalse(boundary["p0_sim_physics_pass_allowed"])
        self.assertEqual(value["active_variant"], "current_kunwei_stack_122p1_geometry_provisional")

    def test_tool0_fixed_transform_is_rigid_and_not_guessed_tcp(self) -> None:
        transform = fixed_transform(fixed_urdf(), "wrist_3_link", "tool0")

        np.testing.assert_allclose(transform[:3, :3] @ transform[:3, :3].T, np.eye(3), atol=1e-12)
        self.assertAlmostEqual(float(np.linalg.det(transform[:3, :3])), 1.0)
        tcp = config()["variants"]["current_kunwei_stack_122p1_geometry_provisional"]["active_tcp_offset_tool0_m"]
        self.assertAlmostEqual(tcp[2], 0.12209917288991741)
        self.assertNotAlmostEqual(tcp[2], 0.085)

    def test_provisional_inertia_is_finite_positive_and_explicitly_estimated(self) -> None:
        inertia = box_diagonal_inertia(0.3662, (0.10, 0.10, 0.1221))

        self.assertTrue(all(value > 0.0 for value in inertia))
        variant = config()["variants"]["current_kunwei_stack_122p1_geometry_provisional"]
        self.assertIn("estimate", variant["inertia_model"])
        self.assertEqual(variant["claim_ceiling"], "geometry_provisional")

    def test_velocity_model_contains_native_contact_ft_and_four_cameras(self) -> None:
        text = patch_mjcf(
            canonical_mjcf(),
            config=config(),
            urdf_text=fixed_urdf(),
            actuator_mode="velocity",
        )
        root = ET.fromstring(text)

        self.assertEqual(root.find("option").attrib["timestep"], "0.0005")
        self.assertEqual(
            [node.attrib["name"] for node in root.findall("./actuator/velocity")],
            [f"velocity_{name}" for name in JOINT_NAMES],
        )
        self.assertEqual(root.findall("./actuator/motor"), [])
        self.assertIsNotNone(root.find(".//site[@name='active_tcp_site']"))
        self.assertIsNotNone(root.find(".//site[@name='kunwei_ft_sensor_site']"))
        self.assertIsNotNone(root.find("./sensor/force[@name='kunwei_force_raw']"))
        self.assertIsNotNone(root.find("./sensor/torque[@name='kunwei_torque_raw']"))
        self.assertIsNotNone(root.find("./sensor/force[@name='production_tcp_force_raw']"))
        self.assertIsNotNone(root.find("./sensor/torque[@name='production_tcp_torque_raw']"))
        touch_site = root.find(".//site[@name='contact_pad_touch_site']")
        self.assertEqual(touch_site.attrib["type"], "box")
        self.assertEqual(touch_site.attrib["size"], "0.026 0.026 0.005")
        self.assertIsNotNone(root.find("./contact/pair[@name='eoat_surface_pair']"))
        surface_visual = root.find(".//geom[@name='step5_surface_visual']")
        surface_collision = root.find(".//geom[@name='step5_surface_collision']")
        self.assertEqual(surface_visual.attrib["type"], "mesh")
        self.assertEqual(surface_visual.attrib["contype"], "0")
        self.assertEqual(surface_collision.attrib["type"], "box")
        self.assertEqual(surface_collision.attrib["size"], "0.09 0.05 0.0040224195")
        self.assertEqual(surface_collision.attrib["solref"], "-1000000 -100")
        cameras = {node.attrib["name"] for node in root.findall(".//camera")}
        self.assertEqual(cameras, {"observer_wide", "observer_oblique", "observer_close", "observer_contact"})
        self.assertNotIn("tcp_site_unverified_85mm", text)
        self.assertNotIn("ur10e_nominal.xml", text)

    def test_torque_surrogate_is_mutually_exclusive_and_claim_limited(self) -> None:
        text = patch_mjcf(
            canonical_mjcf(),
            config=config(),
            urdf_text=fixed_urdf(),
            actuator_mode="torque_surrogate",
        )
        root = ET.fromstring(text)

        self.assertEqual(root.findall("./actuator/velocity"), [])
        self.assertEqual(
            [node.attrib["name"] for node in root.findall("./actuator/motor")],
            [f"torque_{name}" for name in JOINT_NAMES],
        )
        self.assertFalse(config()["claim_boundary"]["live_accepted"])

    def test_p0_no_contact_scene_retracts_surface_without_disabling_collision(self) -> None:
        contact = ET.fromstring(
            patch_mjcf(
                canonical_mjcf(),
                config=config(),
                urdf_text=fixed_urdf(),
                actuator_mode="velocity",
                scene_id="contact",
            )
        )
        no_contact = ET.fromstring(
            patch_mjcf(
                canonical_mjcf(),
                config=config(),
                urdf_text=fixed_urdf(),
                actuator_mode="velocity",
                scene_id="p0_no_contact",
            )
        )

        contact_position = np.fromstring(
            contact.find(".//body[@name='step5_surface']").attrib["pos"], sep=" "
        )
        no_contact_position = np.fromstring(
            no_contact.find(".//body[@name='step5_surface']").attrib["pos"], sep=" "
        )
        expected_offset = np.asarray(
            config()["surface"]["p0_no_contact_scene"]["translation_offset_m"]
        )
        np.testing.assert_allclose(no_contact_position - contact_position, expected_offset)
        collision = no_contact.find(".//geom[@name='step5_surface_collision']")
        self.assertEqual(collision.attrib["contype"], "1")
        self.assertEqual(collision.attrib["conaffinity"], "1")
        self.assertIsNotNone(no_contact.find("./contact/pair[@name='eoat_surface_pair']"))

    def test_integer_schedule_is_frozen(self) -> None:
        rates = config()["rates_hz"]
        self.assertEqual(rates, {"physics": 2000, "control": 500, "dbil": 200})
        self.assertEqual(rates["physics"] // rates["control"], 4)
        self.assertEqual(rates["physics"] // rates["dbil"], 10)


if __name__ == "__main__":
    unittest.main()
