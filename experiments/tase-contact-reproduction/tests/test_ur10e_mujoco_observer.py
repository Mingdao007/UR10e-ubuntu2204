#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from render_ur10e_mujoco_observer import CAMERAS, validate_review  # noqa: E402


class Ur10eMujocoObserverTest(unittest.TestCase):
    def test_provisional_four_view_review_is_bound_and_non_promoting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            views = []
            for camera in CAMERAS:
                path = root / f"{camera}.png"
                path.write_bytes(camera.encode())
                views.append(
                    {
                        "camera": camera,
                        "path": path.name,
                        "size_bytes": path.stat().st_size,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
            payload = {
                "schema": "ur10e_mujoco_observer_review_v1",
                "views": views,
                "overall_pass": False,
                "measured_tcp_surface_xy_error_m": 0.0,
                "blockers": ["current_kunwei_stack_full_cad_missing"],
                "claim_boundary": {
                    "visual_acceptance_pass": False,
                    "contact_sim_pass": False,
                    "live_accepted": False,
                    "reproduction_complete": False,
                },
            }

            self.assertEqual(validate_review(payload, root), [])
            payload["overall_pass"] = True
            self.assertIn(
                "provisional_scene_must_not_pass_visual_acceptance",
                validate_review(payload, root),
            )
            payload["overall_pass"] = False
            payload["measured_tcp_surface_xy_error_m"] = 1.0
            self.assertIn(
                "tcp_surface_model_xy_alignment_invalid",
                validate_review(payload, root),
            )


if __name__ == "__main__":
    unittest.main()
