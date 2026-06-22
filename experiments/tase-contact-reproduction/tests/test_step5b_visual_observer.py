#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import build_step5b_visual_observer as visual_observer  # noqa: E402
import run_step5b_gz_transport_probe as probe  # noqa: E402


class Step5bVisualObserverTest(unittest.TestCase):
    def test_visual_observer_writes_required_images_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step5b_visual_observer_test_") as tmp:
            run_dir = Path(tmp) / "step5b_gz_transport_integration_fixture"
            run_dir.mkdir()
            probe.build_probe_world(run_dir / "probe_worlds", no_contact=False)
            trace_dir = run_dir / "step5b_total_wrench"
            trace_dir.mkdir(parents=True)
            (trace_dir / "step5b_total_contact_wrench_trace.json").write_text(
                json.dumps(
                    {
                        "schema": "ur10e_canonical_wrench_trace_v1",
                        "rows": [
                            {
                                "t_s": 0.125,
                                "force_n": [0.0, 0.0, -5.0],
                                "normal_load_n": 5.0,
                                "reaction_normal": [0.0, 0.0, -1.0],
                                "approach_normal": [0.0, 0.0, 1.0],
                            }
                        ],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            review_path = visual_observer.build_visual_observer(run_dir)
            review = json.loads(review_path.read_text(encoding="utf-8"))
            metrics = json.loads(Path(review["image_metrics_path"]).read_text(encoding="utf-8"))
            files_exist = all(Path(row["path"]).is_file() for row in review["artifacts"].values())

        self.assertFalse(review["viewer_level_pass"])
        self.assertFalse(review["formal_viewer_evidence_present"])
        self.assertFalse(review["formal_required_items"]["ur10e_arm_visible"])
        self.assertFalse(review["formal_required_items"]["eoat_chain_attached_to_arm_visible"])
        self.assertTrue(review["m4_visual_path_debuggable"])
        self.assertIn("schematic_overlay_not_formal_gazebo_rviz_viewer_evidence", review["failure_reasons"])
        self.assertIn("current_run_rviz_viewer_level_candidate_missing", review["failure_reasons"])
        self.assertTrue(metrics["all_nonblank"])
        self.assertTrue(files_exist)

    def test_visual_observer_records_static_rviz_candidate_without_formal_acceptance(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step5b_visual_observer_rviz_test_") as tmp:
            run_dir = Path(tmp) / "step5b_gz_transport_integration_fixture"
            run_dir.mkdir()
            probe.build_probe_world(run_dir / "probe_worlds", no_contact=False)
            trace_dir = run_dir / "step5b_total_wrench"
            trace_dir.mkdir(parents=True)
            (trace_dir / "step5b_total_contact_wrench_trace.json").write_text(
                json.dumps({"schema": "ur10e_canonical_wrench_trace_v1", "rows": []}, indent=2) + "\n",
                encoding="utf-8",
            )
            rviz_dir = run_dir / "step5b_rviz_debug"
            rviz_dir.mkdir()
            screenshot = rviz_dir / "rviz_debug_render.png"
            screenshot.write_bytes(b"fake-rviz-png")
            render_report = {
                "schema": "ur10e_step5b_rviz_debug_render_report_v1",
                "ok": True,
                "current_run_rviz_viewer_candidate_present": True,
                "formal_step5b_viewer_acceptance_allowed": False,
                "screenshot_path": str(screenshot),
                "screenshot_sha256": "0" * 64,
            }
            (rviz_dir / "rviz_render_report.json").write_text(json.dumps(render_report, indent=2) + "\n", encoding="utf-8")
            (rviz_dir / "step5b_rviz_debug_manifest.json").write_text(
                json.dumps({"all_required_items_evidenced": True}, indent=2) + "\n",
                encoding="utf-8",
            )

            review_path = visual_observer.build_visual_observer(run_dir)
            review = json.loads(review_path.read_text(encoding="utf-8"))
            rviz_status = json.loads(Path(review["artifacts"]["rviz_candidate_status"]["path"]).read_text(encoding="utf-8"))

        self.assertFalse(review["viewer_level_pass"])
        self.assertFalse(review["formal_viewer_evidence_present"])
        self.assertTrue(review["formal_required_items"]["rviz_viewer_candidate_present"])
        self.assertNotIn("current_run_rviz_viewer_level_candidate_missing", review["failure_reasons"])
        self.assertIn("static_rviz_candidate_not_formal_observer_acceptance", review["failure_reasons"])
        self.assertIn("static_rviz_candidate_not_formal_observer_acceptance", review["m5_go_blockers"])
        self.assertTrue(rviz_status["current_run_rviz_viewer_candidate_present"])
        self.assertFalse(rviz_status["formal_step5b_viewer_acceptance_allowed"])


if __name__ == "__main__":
    unittest.main()
