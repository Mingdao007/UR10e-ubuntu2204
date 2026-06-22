#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
MODULE_PATH = TOOLS / "build_step5b_rviz_debug_evidence_pack.py"
RENDERER_PATH = TOOLS / "render_p3_rviz_debug_scene.py"
sys.path.insert(0, str(TOOLS))


def import_module(path: Path, name: str):
    if not path.is_file():
        raise AssertionError(f"missing module: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Step5bRvizDebugEvidencePackTest(unittest.TestCase):
    def test_write_pack_creates_step5b_manifest_and_config(self) -> None:
        pack = import_module(MODULE_PATH, "build_step5b_rviz_debug_evidence_pack")
        with tempfile.TemporaryDirectory(prefix="step5b_rviz_pack_") as tmp:
            root = Path(tmp)
            run_dir = self._write_run_fixture(root / "run")
            manifest_path = pack.write_pack(
                root / "rviz",
                run_dir,
                generated_at="2026-06-22T11:45:00+08:00",
            )
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            config_text = (manifest_path.parent / "ur10e_step5b_debug.rviz").read_text(encoding="utf-8")

        self.assertEqual(payload["schema"], "ur10e_step5b_rviz_debug_evidence_pack_v1")
        self.assertEqual(payload["claim_tier"], "visual_only")
        self.assertEqual(payload["render_report_schema"], "ur10e_step5b_rviz_debug_render_report_v1")
        self.assertEqual(payload["publisher_node_name"], "ur10e_step5b_rviz_static_scene_publisher")
        self.assertTrue(payload["all_required_items_evidenced"])
        self.assertFalse(payload["rendered_screenshot_evidence"]["present"])
        self.assertFalse(payload["current_run_rviz_viewer_candidate_present"])
        self.assertFalse(payload["formal_step5b_viewer_acceptance_allowed"])
        self.assertFalse(payload["full_rviz_render_acceptance_allowed"])
        self.assertFalse(payload["allow_full_rviz_render_acceptance_from_static_render"])
        self.assertIn("no Gazebo launch", payload["safety_boundary"])
        self.assertIn("formal Step5b observer acceptance", payload["forbidden_claim"])
        self.assertIn("/ur10e/step5b/rviz/wrench_vector", config_text)
        self.assertIn("step5b_total_contact_wrench_trace.json", payload["source_paths"]["total_wrench_trace"])
        self.assertTrue(payload["source_hashes"]["probe_manifest_sha256"])
        self.assertEqual(payload["static_scene"]["trace_summary"]["row_count"], 1)

    def test_missing_required_items_fails_closed(self) -> None:
        pack = import_module(MODULE_PATH, "build_step5b_rviz_debug_evidence_pack")
        payload = {"evidenced_items": {item: {"evidenced": True} for item in pack.REQUIRED_ITEMS}}
        payload["evidenced_items"]["TF tree"]["evidenced"] = False
        self.assertEqual(pack.missing_required_items(payload), ["TF tree"])

    def test_renderer_update_marks_step5b_current_run_candidate_only(self) -> None:
        pack = import_module(MODULE_PATH, "build_step5b_rviz_debug_evidence_pack")
        renderer = import_module(RENDERER_PATH, "render_p3_rviz_debug_scene")
        with tempfile.TemporaryDirectory(prefix="step5b_rviz_render_update_") as tmp:
            root = Path(tmp)
            run_dir = self._write_run_fixture(root / "run")
            manifest_path = pack.write_pack(root / "rviz", run_dir)
            screenshot = manifest_path.parent / "rviz_debug_render.png"
            screenshot.write_bytes(b"fake-png-for-manifest-update-test")
            report = manifest_path.parent / "rviz_render_report.json"

            renderer.update_manifest_with_render(
                manifest_path,
                generated_at="2026-06-22T11:46:00+08:00",
                screenshot_path=screenshot,
                render_report_path=report,
            )
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            render_report = renderer.build_render_report(
                generated_at="2026-06-22T11:46:00+08:00",
                manifest_path=manifest_path,
                rviz_config_path=manifest_path.parent / "ur10e_step5b_debug.rviz",
                screenshot_path=screenshot,
                publisher_summary_path=manifest_path.parent / "summary.json",
                rviz_stdout_path=manifest_path.parent / "rviz_stdout.txt",
                rviz_stderr_path=manifest_path.parent / "rviz_stderr.txt",
                ffmpeg_stderr_path=manifest_path.parent / "ffmpeg_stderr.txt",
                rviz_returncode=0,
                publisher_returncode=0,
                ffmpeg_returncode=0,
            )

        self.assertTrue(payload["rendered_screenshot_evidence"]["present"])
        self.assertTrue(payload["current_run_rviz_viewer_candidate_present"])
        self.assertFalse(payload["formal_step5b_viewer_acceptance_allowed"])
        self.assertFalse(payload["full_rviz_render_acceptance_allowed"])
        self.assertEqual(render_report["schema"], "ur10e_step5b_rviz_debug_render_report_v1")
        self.assertTrue(render_report["current_run_rviz_viewer_candidate_present"])
        self.assertFalse(render_report["formal_step5b_viewer_acceptance_allowed"])

    def _write_run_fixture(self, run_dir: Path) -> Path:
        probe_dir = run_dir / "probe_worlds"
        probe_dir.mkdir(parents=True)
        world = probe_dir / "step5b_forced_contact_probe_world.sdf"
        world.write_text(
            """<sdf version="1.9">
  <world name="step5b_fixture">
    <model name="step5b_nonformal_forced_eoat_probe">
      <pose>-0.486 -0.224 0.088 0 0 0</pose>
    </model>
    <model name="step5b_reference_path_visual">
      <pose>0 0 0 0 0 0</pose>
      <link name="p0"><pose>-0.50 -0.24 0.05 0 0 0</pose></link>
      <link name="p1"><pose>-0.48 -0.22 0.04 0 0 0</pose></link>
    </model>
  </world>
</sdf>
""",
            encoding="utf-8",
        )
        manifest = {
            "probe_world": str(world),
            "contact_target_pose_world": {
                "x_m": -0.486,
                "y_m": -0.224,
                "z_m": 0.008,
                "surface_top_z_m": 0.008,
            },
            "surface": {
                "min_x_m": -0.523,
                "max_x_m": -0.437,
                "min_y_m": -0.244,
                "max_y_m": -0.069,
                "size_z_m": 0.008,
                "top_z_m": 0.008,
            },
        }
        (probe_dir / "step5b_forced_contact_probe_world.manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        trace_dir = run_dir / "step5b_total_wrench"
        trace_dir.mkdir(parents=True)
        (trace_dir / "step5b_total_contact_wrench_trace.json").write_text(
            json.dumps(
                {
                    "rows": [
                        {
                            "normal_load_n": 245.0,
                            "reaction_normal": [0.0, 0.0, 1.0],
                            "approach_normal": [0.0, 0.0, -1.0],
                        }
                    ]
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        visual_dir = run_dir / "step5b_visual_observer"
        visual_dir.mkdir()
        (visual_dir / "observer_review.json").write_text(
            json.dumps({"viewer_level_pass": False}, indent=2) + "\n",
            encoding="utf-8",
        )
        return run_dir


if __name__ == "__main__":
    unittest.main()
