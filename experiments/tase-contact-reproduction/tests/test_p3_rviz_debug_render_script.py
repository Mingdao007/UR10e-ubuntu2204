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
MODULE_PATH = TOOLS / "render_p3_rviz_debug_scene.py"
sys.path.insert(0, str(TOOLS))


def import_render_module():
    if not MODULE_PATH.is_file():
        raise AssertionError(f"missing renderer: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("render_p3_rviz_debug_scene", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load spec for {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class P3RvizDebugRenderScriptTest(unittest.TestCase):
    def test_build_render_report_is_visual_only_and_no_live(self) -> None:
        renderer = import_render_module()
        with tempfile.TemporaryDirectory(prefix="rviz_render_report_") as tmp:
            root = Path(tmp)
            screenshot = root / "rviz_debug_render.png"
            screenshot.write_bytes(b"not-a-real-png-for-report-test")
            manifest = root / "rviz_debug_manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            config = root / "ur10e_p3_debug.rviz"
            config.write_text("Panels: []\n", encoding="utf-8")
            summary = root / "summary.json"
            summary.write_text("{}", encoding="utf-8")
            stdout = root / "rviz_stdout.txt"
            stdout.write_text("", encoding="utf-8")
            stderr = root / "rviz_stderr.txt"
            stderr.write_text("", encoding="utf-8")
            ffmpeg = root / "ffmpeg_stderr.txt"
            ffmpeg.write_text("", encoding="utf-8")
            report = renderer.build_render_report(
                generated_at="2026-06-21T04:30:00+08:00",
                manifest_path=manifest,
                rviz_config_path=config,
                screenshot_path=screenshot,
                publisher_summary_path=summary,
                rviz_stdout_path=stdout,
                rviz_stderr_path=stderr,
                ffmpeg_stderr_path=ffmpeg,
                rviz_returncode=0,
                publisher_returncode=0,
                ffmpeg_returncode=0,
            )

        self.assertEqual(report["schema"], "ur10e_p3_rviz_debug_render_report_v1")
        self.assertEqual(report["claim_tier"], "visual_only")
        self.assertTrue(report["ok"])
        self.assertIn("no robot motion", report["safety_boundary"])
        self.assertEqual(
            report["forbidden_claim"],
            "physical Gazebo collision/contact physics; simulated_ft; real bench/live contact",
        )
        self.assertEqual(report["returncodes"]["ffmpeg"], 0)
        self.assertTrue(report["screenshot_sha256"])

    def test_update_manifest_with_render_records_visual_only_screenshot(self) -> None:
        renderer = import_render_module()
        with tempfile.TemporaryDirectory(prefix="rviz_render_manifest_") as tmp:
            root = Path(tmp)
            screenshot = root / "rviz_debug_render.png"
            screenshot.write_bytes(b"not-a-real-png-for-manifest-test")
            report = root / "rviz_render_report.json"
            manifest = root / "rviz_debug_manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "all_required_items_evidenced": True,
                        "rendered_screenshot_evidence": {"present": False},
                    }
                ),
                encoding="utf-8",
            )
            renderer.update_manifest_with_render(
                manifest,
                generated_at="2026-06-21T04:30:00+08:00",
                screenshot_path=screenshot,
                render_report_path=report,
            )
            payload = json.loads(manifest.read_text(encoding="utf-8"))

        self.assertTrue(payload["rendered_screenshot_evidence"]["present"])
        self.assertEqual(payload["rendered_screenshot_evidence"]["claim_tier"], "visual_only")
        self.assertTrue(payload["full_rviz_render_acceptance_allowed"])


if __name__ == "__main__":
    unittest.main()
