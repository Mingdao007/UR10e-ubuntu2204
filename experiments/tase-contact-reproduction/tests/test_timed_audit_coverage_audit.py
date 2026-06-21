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
MODULE_PATH = TOOLS / "build_timed_audit_coverage_audit.py"
sys.path.insert(0, str(TOOLS))


def import_audit_module():
    if not MODULE_PATH.is_file():
        raise AssertionError(f"missing generator: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("build_timed_audit_coverage_audit", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load spec for {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_subagent_triplet(root: Path, hour: int) -> None:
    for lens in ["visual-observer", "geometry-frame", "report-claim"]:
        (root / f"ur10e-gazebo-hour{hour}-{lens}-subagent-prompt-20260621-010000.md").write_text(
            "prompt\n",
            encoding="utf-8",
        )
        (root / f"ur10e-gazebo-hour{hour}-{lens}-subagent-result-20260621-010000.md").write_text(
            "result\n",
            encoding="utf-8",
        )


def write_json_opus_record(root: Path, *, name: str, finished_at: str) -> None:
    prompt = root / f"{name}-prompt.md"
    response = root / f"{name}-response.md"
    stderr = root / f"{name}-stderr.txt"
    meta = root / f"{name}-meta.json"
    prompt.write_text("prompt\n", encoding="utf-8")
    response.write_text("response\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    meta.write_text(
        json.dumps(
            {
                "schema": "ur10e_claude_opus_advisory_meta_v1",
                "model": "opus",
                "status": "ok",
                "exit_code": 0,
                "prompt_path": str(prompt),
                "response_path": str(response),
                "stderr_path": str(stderr),
                "started_at": finished_at,
                "finished_at": finished_at,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def write_text_opus_record(root: Path, *, name: str, finished_at: str) -> None:
    prompt = root / f"{name}-prompt.md"
    response = root / f"{name}-response.md"
    stderr = root / f"{name}-stderr.txt"
    meta = root / f"{name}-meta.txt"
    prompt.write_text("prompt\n", encoding="utf-8")
    response.write_text("response\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    meta.write_text(
        "\n".join(
            [
                f"prompt={prompt}",
                f"response={response}",
                f"stderr={stderr}",
                f"started_at={finished_at}",
                f"ended_at={finished_at}",
                "exit_code=0",
                "",
            ]
        ),
        encoding="utf-8",
    )


class TimedAuditCoverageAuditTest(unittest.TestCase):
    def test_accepts_complete_expected_schedule_and_meta_formats(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="timed_audit_complete_fixture_") as tmp:
            root = Path(tmp)
            write_subagent_triplet(root, 1)
            write_subagent_triplet(root, 2)
            write_json_opus_record(
                root,
                name="ur10e-gazebo-opus-baseline-20260621-000500-HKT",
                finished_at="2026-06-21T00:05:00+08:00",
            )
            write_text_opus_record(
                root,
                name="ur10e-gazebo-opus-hour2-20260621-020500-HKT",
                finished_at="2026-06-21T02:05:00+08:00",
            )
            summary = audit.timed_audit_coverage_summary(
                root,
                goal_start_at="2026-06-21T00:00:00+08:00",
                generated_at="2026-06-21T02:30:00+08:00",
            )

        self.assertTrue(summary["full_acceptance_timed_audit_ready"])
        self.assertEqual(summary["expected_subagent_triplet_hours"], [1, 2])
        self.assertEqual(summary["missing_expected_subagent_triplet_hours"], [])
        self.assertEqual(summary["expected_opus_checkpoint_hours"], [0, 2])
        self.assertEqual(summary["missing_opus_checkpoint_hours"], [])
        self.assertEqual(summary["complete_opus_record_count"], 2)

    def test_goal_schedule_blocks_missing_expected_hours(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="timed_audit_gap_fixture_") as tmp:
            root = Path(tmp)
            write_subagent_triplet(root, 4)
            write_subagent_triplet(root, 7)
            base = root / "ur10e-gazebo-hour7-opus-advisory-response-20260621-075742"
            base.with_suffix(".stdout.txt").write_text("advice\n", encoding="utf-8")
            base.with_suffix(".stderr.txt").write_text("", encoding="utf-8")
            base.with_suffix(".exitcode.txt").write_text("0\n", encoding="utf-8")
            summary = audit.timed_audit_coverage_summary(
                root,
                goal_start_at="2026-06-21T00:56:00+08:00",
                generated_at="2026-06-21T09:12:00+08:00",
            )

        self.assertFalse(summary["full_acceptance_timed_audit_ready"])
        self.assertEqual(summary["expected_subagent_triplet_hours"], [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(summary["missing_expected_subagent_triplet_hours"], [1, 2, 3, 5, 6, 8])
        self.assertEqual(summary["missing_opus_checkpoint_hours"], [0, 2, 4, 8])
        self.assertIn("hourly subagent triplet sequence has gaps", summary["unresolved_p0_p1_findings"])
        self.assertIn("Opus advisory checkpoint coverage missing, stale, or nonzero", summary["unresolved_p0_p1_findings"])

    def test_write_audit_creates_machine_readable_artifact(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="timed_audit_write_fixture_") as tmp:
            output_dir = Path(tmp) / "out"
            path = audit.write_audit(
                output_dir,
                generated_at="2026-06-21T01:10:00+08:00",
                goal_start_at="2026-06-21T00:00:00+08:00",
                handoff_root=Path(tmp) / "handoffs",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema"], "ur10e_timed_audit_coverage_audit_v1")
        self.assertFalse(payload["timed_audit_coverage"]["full_acceptance_timed_audit_ready"])
        self.assertFalse(payload["live_authorization"]["robot_motion_authorized"])


if __name__ == "__main__":
    unittest.main()
