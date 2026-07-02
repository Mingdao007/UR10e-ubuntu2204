#!/usr/bin/env python3
"""Offline tests for Step5d current package promotion."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import promote_step5d_current as promote  # noqa: E402


TARGET_DIR = "/programs/andyl/kunwei/step5"
V20 = "step5d_strict_rnn_liveprep_v20"
V21 = "step5d_strict_rnn_liveprep_v21"
V22 = "step5d_strict_rnn_liveprep_v22"
V23 = "step5d_strict_rnn_liveprep_v23"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_triplet(local_dir: Path, program: str, prefix: str) -> dict[str, str]:
    local_dir.mkdir(parents=True, exist_ok=True)
    shas: dict[str, str] = {}
    for ext in promote.EXTENSIONS:
        data = f"{prefix}:{program}:{ext}\n".encode("utf-8")
        (local_dir / f"{program}{ext}").write_bytes(data)
        shas[ext] = _sha(data)
    return shas


def _write_readback(root: Path, program: str, local_dir: Path, shas: dict[str, str]) -> Path:
    readback_dir = root / "runs" / f"controller_readback_{program}_fixture"
    readback_dir.mkdir(parents=True, exist_ok=True)
    for ext in promote.EXTENSIONS:
        source = local_dir / f"{program}{ext}"
        (readback_dir / source.name).write_bytes(source.read_bytes())
    manifest = {
        "status": "controller read-back verified",
        "target_dir": TARGET_DIR,
        "delivery_mode": "full_upload_readback",
        "validation": {
            "stamp": f"2026-07-02T2200HKT_{program.upper()}",
            "program": program,
            "target_dir": TARGET_DIR,
            "installation_relative_path": "../../../default",
            "script_node_path": f"{TARGET_DIR}/{program}.script",
            "script_sha256": shas[".script"],
            "txt_sha256": shas[".txt"],
            "urp_sha256": shas[".urp"],
        },
        "sha256": {
            "local": shas,
            "controller": shas,
            "readback": shas,
        },
    }
    manifest_path = readback_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def _write_fixture(root: Path) -> tuple[Path, Path]:
    config = root / "config"
    config.mkdir(parents=True)
    v20_dir = root / "programs" / "step5" / "step5d"
    v21_dir = root / "programs" / "step5"
    v22_dir = root / "candidate"
    v20_sha = _write_triplet(v20_dir, V20, "retained-v20")
    v21_sha = _write_triplet(v21_dir, V21, "old-current")
    v22_sha = _write_triplet(v22_dir, V22, "new-current")
    manifest_path = _write_readback(root, V22, v22_dir, v22_sha)
    v21_run = root / "runs" / "bridge_step4e_line_outerloop_step5d_strict_rnn_liveprep_v21_fixture"
    v21_run.mkdir(parents=True)
    (v21_run / "summary.json").write_text(json.dumps({"stop_reason": 13}), encoding="utf-8")
    current = {
        "version": 2,
        "current_step": "Step5d",
        "current_stage_id": V21,
        "program": V21,
        "stage_table_path": "config/step5_stage_table.json",
        "controller_target": f"{TARGET_DIR}/{V21}.urp",
        "controller_script": f"{TARGET_DIR}/{V21}.script",
        "local_triplet": f"programs/step5/{V21}",
        "status": f"{V21}_controller_readback_verified_pending_live_bridge_run_not_reproduction_claim",
        "sha256": v21_sha,
        "bridge_profile": {
            "step4e_version": V21,
        },
        "evidence": {},
        "bridge_trigger": {
            "required_before_live": [f"TP program opened on controller read-back v21 package"],
        },
        "retained_steps": [
            {"step": "Step5", "role": "v21 current before test"},
        ],
        "notes": [],
    }
    table = {
        "stages": [
            {
                "id": V20,
                "stage": "Step5d",
                "owner": "bridge+TP",
                "active": False,
                "blocked": False,
                "complete": True,
                "completion_target": False,
                "block_reason": "retained fixture",
                "guard": {
                    "line_entry_normal_load_min_n": 8.0,
                    "line_entry_normal_load_max_n": 13.0,
                },
                "cadence": {},
                "contact_policy": {
                    "controller_readback_status": "verified",
                },
                "local_delivery_evidence": {
                    "program_basename": V20,
                    "local_program_dir": "programs/step5/step5d",
                    "local_triplet": f"programs/step5/step5d/{V20}.{{script,txt,urp}}",
                    "controller_readback_verified": True,
                    "sha256": v20_sha,
                    "archived_to_step5d_dir": True,
                },
            },
            {
                "id": V21,
                "stage": "Step5d",
                "owner": "bridge+TP",
                "active": True,
                "blocked": False,
                "complete": False,
                "completion_target": True,
                "block_reason": "current fixture",
                "guard": {
                    "line_entry_normal_load_min_n": 7.5,
                    "line_entry_normal_load_max_n": 14.0,
                },
                "cadence": {},
                "contact_policy": {
                    "controller_readback_status": "verified",
                },
                "local_delivery_evidence": {
                    "program_basename": V21,
                    "local_program_dir": "programs/step5",
                    "local_triplet": f"programs/step5/{V21}.{{script,txt,urp}}",
                    "controller_readback_verified": True,
                    "sha256": v21_sha,
                },
            }
        ]
    }
    (config / "current_stage.json").write_text(json.dumps(current), encoding="utf-8")
    (config / "step5_stage_table.json").write_text(json.dumps(table), encoding="utf-8")
    return v22_dir, manifest_path


def _write_v23_fixture(root: Path) -> tuple[Path, Path]:
    config = root / "config"
    config.mkdir(parents=True)
    v22_dir = root / "programs" / "step5"
    v23_dir = root / "candidate"
    v22_sha = _write_triplet(v22_dir, V22, "old-current-v22")
    v23_sha = _write_triplet(v23_dir, V23, "new-current-v23")
    manifest_path = _write_readback(root, V23, v23_dir, v23_sha)
    v22_run = root / "runs" / "bridge_step4e_line_outerloop_step5d_strict_rnn_liveprep_v22_fixture"
    v22_run.mkdir(parents=True)
    (v22_run / "summary.json").write_text(json.dumps({"stop_reason": "normal_force_guard"}), encoding="utf-8")
    current = {
        "version": 2,
        "current_step": "Step5d",
        "current_stage_id": V22,
        "program": V22,
        "stage_table_path": "config/step5_stage_table.json",
        "controller_target": f"{TARGET_DIR}/{V22}.urp",
        "controller_script": f"{TARGET_DIR}/{V22}.script",
        "local_triplet": f"programs/step5/{V22}",
        "status": f"{V22}_controller_readback_verified_pending_live_bridge_run_not_reproduction_claim",
        "sha256": v22_sha,
        "bridge_profile": {"step4e_version": V22},
        "evidence": {},
        "bridge_trigger": {"required_before_live": [f"TP program opened on controller read-back v22 package"]},
        "retained_steps": [{"step": "Step5", "role": "v22 current before test"}],
        "notes": [],
    }
    table = {
        "stages": [
            {
                "id": V22,
                "stage": "Step5d",
                "owner": "bridge+TP",
                "active": True,
                "blocked": False,
                "complete": False,
                "completion_target": True,
                "block_reason": "current fixture",
                "guard": {
                    "line_entry_normal_load_min_n": 7.5,
                    "line_entry_normal_load_max_n": 14.0,
                },
                "cadence": {},
                "contact_policy": {"controller_readback_status": "verified"},
                "local_delivery_evidence": {
                    "program_basename": V22,
                    "local_program_dir": "programs/step5",
                    "local_triplet": f"programs/step5/{V22}.{{script,txt,urp}}",
                    "controller_readback_verified": True,
                    "sha256": v22_sha,
                },
            }
        ]
    }
    (config / "current_stage.json").write_text(json.dumps(current), encoding="utf-8")
    (config / "step5_stage_table.json").write_text(json.dumps(table), encoding="utf-8")
    return v23_dir, manifest_path


class Step5dCurrentPromotionTest(unittest.TestCase):
    def test_promote_v22_archives_v21_and_updates_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            v22_dir, manifest_path = _write_fixture(root)

            result = promote.promote(root, V22, TARGET_DIR, v22_dir, manifest_path)

            self.assertTrue(result["ok"])
            self.assertEqual(result["previous_program"], V21)
            self.assertFalse((root / "programs" / "step5" / f"{V21}.urp").exists())
            self.assertTrue((root / "programs" / "step5" / "step5d" / f"{V21}.urp").is_file())
            self.assertTrue((root / "programs" / "step5" / f"{V22}.urp").is_file())
            current = json.loads((root / "config" / "current_stage.json").read_text(encoding="utf-8"))
            self.assertEqual(current["program"], V22)
            self.assertEqual(current["bridge_profile"]["step4e_version"], V22)
            self.assertIn("v22", current["bridge_trigger"]["required_before_live"][1])
            self.assertIn("stage25_95_qdot_clear_barrier", current["bridge_profile"])
            self.assertTrue(current["evidence"]["v21_retained_after_live_failure"])
            self.assertEqual(current["evidence"]["v21_live_attempts"]["latest_stop_reason"], 13)
            self.assertEqual(current["evidence"]["v22_qdot_clear_barrier"]["stage"], 25.95)
            table = json.loads((root / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
            rows = {row["id"]: row for row in table["stages"]}
            self.assertFalse(rows[V21]["active"])
            self.assertTrue(rows[V21]["complete"])
            self.assertTrue(rows[V21]["local_delivery_evidence"]["archived_to_step5d_dir"])
            self.assertIn("Stage25.3->25.0 register-layout hazard", rows[V21]["live_run_evidence"]["root_cause_summary"])
            self.assertTrue(rows[V22]["active"])
            self.assertFalse(rows[V22]["complete"])
            self.assertEqual(rows[V22]["guard"]["line_entry_normal_load_min_n"], 7.5)
            self.assertEqual(rows[V22]["guard"]["line_entry_raw_sanity_min_n"], 7.0)
            self.assertEqual(rows[V22]["guard"]["stage25_95_qdot_clear_required_s"], 0.006)

    def test_promote_v23_archives_v22_and_records_normal_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            v23_dir, manifest_path = _write_v23_fixture(root)

            result = promote.promote(root, V23, TARGET_DIR, v23_dir, manifest_path)

            self.assertTrue(result["ok"])
            self.assertEqual(result["previous_program"], V22)
            self.assertFalse((root / "programs" / "step5" / f"{V22}.urp").exists())
            self.assertTrue((root / "programs" / "step5" / "step5d" / f"{V22}.urp").is_file())
            self.assertTrue((root / "programs" / "step5" / f"{V23}.urp").is_file())
            current = json.loads((root / "config" / "current_stage.json").read_text(encoding="utf-8"))
            self.assertEqual(current["program"], V23)
            self.assertEqual(current["bridge_profile"]["step4e_version"], V23)
            self.assertIn("stage25_post_rnn_normal_guard", current["bridge_profile"])
            self.assertTrue(current["evidence"]["v22_retained_after_live_failure"])
            self.assertEqual(current["evidence"]["v22_live_attempts"]["latest_stop_reason"], "normal_force_guard")
            self.assertEqual(current["evidence"]["v23_qdot_clear_barrier"]["qdot_zero_tol_rad_s"], 0.0005)
            self.assertEqual(current["evidence"]["v23_post_rnn_normal_guard"]["hard_stop_load_n"], 25.0)
            table = json.loads((root / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
            rows = {row["id"]: row for row in table["stages"]}
            self.assertFalse(rows[V22]["active"])
            self.assertTrue(rows[V22]["complete"])
            self.assertTrue(rows[V22]["local_delivery_evidence"]["archived_to_step5d_dir"])
            self.assertIn("normal_force_guard", rows[V22]["live_run_evidence"]["root_cause_summary"])
            self.assertTrue(rows[V23]["active"])
            self.assertFalse(rows[V23]["complete"])
            self.assertEqual(rows[V23]["guard"]["stage25_95_qdot_clear_zero_tol_rad_s"], 0.0005)
            self.assertEqual(rows[V23]["guard"]["stage25_post_rnn_normal_guard_hard_stop_load_n"], 25.0)


if __name__ == "__main__":
    unittest.main()
