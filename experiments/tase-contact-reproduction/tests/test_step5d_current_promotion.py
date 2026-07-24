#!/usr/bin/env python3
"""Offline tests for Step5d current package promotion."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import promote_step5d_current as promote  # noqa: E402


TARGET_DIR = "/programs/andyl/kunwei/step5"
V20 = "step5d_strict_rnn_liveprep_v20"
V21 = "step5d_strict_rnn_liveprep_v21"
V22 = "step5d_strict_rnn_liveprep_v22"
V23 = "step5d_strict_rnn_liveprep_v23"
V24 = "step5d_strict_rnn_liveprep_v24"
V25 = "step5d_strict_rnn_ablation_v25"
V26 = "step5d_strict_rnn_ablation_v26"
V27 = "step5d_strict_rnn_ablation_v27"
V28 = "step5d_strict_rnn_ablation_v28"
V29 = "step5d_strict_rnn_ablation_v29"
V30 = "step5d_strict_rnn_ablation_v30"


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


def _write_v24_fixture(root: Path) -> tuple[Path, Path]:
    config = root / "config"
    config.mkdir(parents=True)
    v23_dir = root / "programs" / "step5"
    v24_dir = root / "candidate"
    v23_sha = _write_triplet(v23_dir, V23, "old-current-v23")
    v24_sha = _write_triplet(v24_dir, V24, "new-current-v24")
    manifest_path = _write_readback(root, V24, v24_dir, v24_sha)
    v23_run = root / "runs" / "bridge_step5d_strict_rnn_liveprep_v23_fixture"
    v23_run.mkdir(parents=True)
    (v23_run / "summary.json").write_text(
        json.dumps({"stop_reason": "step5d_contact_safety:tcp_cage_braking_margin_exhausted"}),
        encoding="utf-8",
    )
    stale_bridge_profile = {
        "step4e_version": V23,
        "cage_primary_policy": (
            "low-load/no-contact inside positive cage margin freezes path_time, resets outer-loop state "
            "during low-load active_reacquire_solver, caps active-reacquire predicted TCP speed at 0.035 m/s, "
            "and remains active_reacquire_solver until hard guards trip"
        ),
        "sensor_hard_guards": {
            "raw_normal_n": 100.0,
            "force_norm_n": 100.0,
            "torque_norm_nm": 4.0,
        },
        "step5d_reacquire_predicted_tcp_speed_cap_m_s": 0.035,
    }
    current = {
        "version": 2,
        "current_step": "Step5d",
        "current_stage_id": V23,
        "program": V23,
        "stage_table_path": "config/step5_stage_table.json",
        "controller_target": f"{TARGET_DIR}/{V23}.urp",
        "controller_script": f"{TARGET_DIR}/{V23}.script",
        "local_triplet": f"programs/step5/{V23}",
        "status": f"{V23}_controller_readback_verified_pending_live_bridge_run_not_reproduction_claim",
        "sha256": v23_sha,
        "bridge_profile": stale_bridge_profile,
        "evidence": {},
        "bridge_trigger": {"required_before_live": [f"TP program opened on controller read-back v23 package"]},
        "retained_steps": [{"step": "Step5", "role": "v23 current before test"}],
        "notes": [],
    }
    table = {
        "stages": [
            {
                "id": V23,
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
                    "raw_normal_guard_n": 100.0,
                    "force_norm_guard_n": 100.0,
                },
                "cadence": {},
                "contact_policy": {"controller_readback_status": "verified"},
                "local_delivery_evidence": {
                    "program_basename": V23,
                    "local_program_dir": "programs/step5",
                    "local_triplet": f"programs/step5/{V23}.{{script,txt,urp}}",
                    "controller_readback_verified": True,
                    "sha256": v23_sha,
                },
            }
        ]
    }
    (config / "current_stage.json").write_text(json.dumps(current), encoding="utf-8")
    (config / "step5_stage_table.json").write_text(json.dumps(table), encoding="utf-8")
    return v24_dir, manifest_path


def _write_v26_fixture(root: Path) -> tuple[Path, Path]:
    config = root / "config"
    config.mkdir(parents=True)
    v25_dir = root / "programs" / "step5"
    v26_dir = root / "candidate"
    v25_sha = _write_triplet(v25_dir, V25, "old-current-v25")
    v26_sha = _write_triplet(v26_dir, V26, "new-current-v26")
    manifest_path = _write_readback(root, V26, v26_dir, v26_sha)
    v25_run = root / "runs" / "bridge_step5d_strict_rnn_ablation_v25_fixture"
    v25_run.mkdir(parents=True)
    (v25_run / "summary.json").write_text(json.dumps({"stop_reason": "signal_sigint"}), encoding="utf-8")
    current = {
        "version": 2,
        "current_step": "Step5d",
        "current_stage_id": V25,
        "program": V25,
        "stage_table_path": "config/step5_stage_table.json",
        "controller_target": f"{TARGET_DIR}/{V25}.urp",
        "controller_script": f"{TARGET_DIR}/{V25}.script",
        "local_triplet": f"programs/step5/{V25}",
        "status": f"{V25}_controller_readback_verified_pending_live_bridge_run_not_reproduction_claim",
        "sha256": v25_sha,
        "bridge_profile": {"step4e_version": V25, "stage25_control_mode": "speedl_cartesian_oracle"},
        "evidence": {},
        "bridge_trigger": {"required_before_live": [f"TP program opened on controller read-back v25 package"]},
        "retained_steps": [{"step": "Step5", "role": "v25 current before test"}],
        "notes": [],
    }
    table = {
        "stages": [
            {
                "id": V25,
                "stage": "Step5d",
                "owner": "bridge+TP",
                "active": True,
                "blocked": False,
                "complete": False,
                "completion_target": True,
                "block_reason": "current v25 fixture",
                "guard": {
                    "line_entry_normal_load_min_n": 10.5,
                    "line_entry_normal_load_max_n": 12.8,
                    "line_entry_raw_sanity_min_n": 9.5,
                    "line_entry_raw_sanity_max_n": 13.5,
                    "attitude_cap_rad_s": 0.150,
                },
                "cadence": {},
                "contact_policy": {"controller_readback_status": "verified"},
                "local_delivery_evidence": {
                    "program_basename": V25,
                    "local_program_dir": "programs/step5",
                    "local_triplet": f"programs/step5/{V25}.{{script,txt,urp}}",
                    "controller_readback_verified": True,
                    "sha256": v25_sha,
                },
                "operator_lifecycle": {
                    "expected_program": f"{TARGET_DIR}/{V25}.urp",
                },
                "runtime_interface_ref": {
                    "stage25_default_control_mode": "speedl_cartesian_oracle",
                },
            },
            {
                "id": V26,
                "stage": "Step5d",
                "owner": "bridge+TP",
                "active": False,
                "blocked": False,
                "complete": False,
                "completion_target": True,
                "block_reason": "candidate v26 fixture",
                "guard": {
                    "line_entry_normal_load_min_n": 7.0,
                    "line_entry_normal_load_max_n": 18.0,
                    "line_entry_raw_sanity_min_n": 5.0,
                    "line_entry_raw_sanity_max_n": 20.0,
                    "line_entry_recovery_normal_load_max_n": 24.0,
                    "attitude_cap_rad_s": 0.015,
                    "cartesian_layout_code": 523.0,
                    "joint_layout_code": 524.0,
                },
                "cadence": {},
                "contact_policy": {"controller_readback_status": "candidate"},
                "local_delivery_evidence": {},
                "operator_lifecycle": {
                    "expected_program": f"{TARGET_DIR}/{V26}.urp",
                },
                "runtime_interface_ref": {
                    "stage25_default_control_mode": "speedl_cartesian_oracle",
                },
            },
        ]
    }
    (config / "current_stage.json").write_text(json.dumps(current), encoding="utf-8")
    (config / "step5_stage_table.json").write_text(json.dumps(table), encoding="utf-8")
    return v26_dir, manifest_path


class Step5dCurrentPromotionTest(unittest.TestCase):
    def test_v30_promotion_rejects_current_incomplete_evidence_before_local_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "candidate_v30"
            v30_sha = _write_triplet(candidate, V30, "v30")
            manifest_path = _write_readback(root, V30, candidate, v30_sha)
            config = root / "config"
            config.mkdir(parents=True)
            current = {
                "program": V29,
                "current_stage_id": V29,
                "stage_table_path": "config/step5_stage_table.json",
                "p0_v8_candidate": {"p0_v8_passed": False},
            }
            table = {
                "stages": [
                    {"id": V29, "active": True},
                    {
                        "id": V30,
                        "active": False,
                        "runtime_profile": {
                            "backend": "cupy",
                            "inner_iterations": 512,
                            "epsilon": 0.01,
                            "sigr_exponent_r": 0.8,
                            "qdot_cap_rad_s": 0.05,
                            "control_mode": "speedj_rnn_live",
                            "joint_layout_code": 524.0,
                        },
                        "runtime_scheduler": {
                            "policy": "SCHED_FIFO",
                            "priority": 20,
                        },
                        "contact_policy": {
                            "dls_shadow_only": True,
                            "dls_fallback_allowed": False,
                        },
                        "guard": {"dls_runtime_fallback_allowed": False},
                        "p0_v8_gate": {"passed": False},
                        "package_delivery": {"sha256": v30_sha},
                    },
                ]
            }
            (config / "current_stage.json").write_text(json.dumps(current), encoding="utf-8")
            (config / "step5_stage_table.json").write_text(json.dumps(table), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "direct frozen-duration P0 v8 pass"):
                promote.promote(root, V30, TARGET_DIR, candidate, manifest_path)

            self.assertFalse((root / "programs" / "step5" / f"{V30}.script").exists())
            self.assertEqual(
                json.loads((config / "current_stage.json").read_text(encoding="utf-8"))["program"],
                V29,
            )

    def test_v30_promotion_does_not_require_live_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            v26_dir, manifest_path = _write_v26_fixture(root)
            promote.promote(root, V26, TARGET_DIR, v26_dir, manifest_path)
            candidate = root / "candidate_v30"
            v30_sha = _write_triplet(candidate, V30, "v30")
            v30_manifest = _write_readback(root, V30, candidate, v30_sha)
            table_path = root / "config/step5_stage_table.json"
            table = json.loads(table_path.read_text(encoding="utf-8"))
            template = next(row for row in table["stages"] if row["id"] == V26)
            candidate_row = json.loads(json.dumps(template))
            candidate_row.update(
                {
                    "id": V30,
                    "active": False,
                    "blocked": True,
                    "runtime_profile": {
                        "backend": "cupy",
                        "inner_iterations": 512,
                        "epsilon": 0.01,
                        "sigr_exponent_r": 0.8,
                        "qdot_cap_rad_s": 0.05,
                        "control_mode": "speedj_rnn_live",
                        "joint_layout_code": 524.0,
                    },
                    "runtime_scheduler": {
                        "policy": "SCHED_FIFO",
                        "priority": 20,
                    },
                    "contact_policy": {
                        "dls_shadow_only": True,
                        "dls_fallback_allowed": False,
                    },
                    "guard": {"dls_runtime_fallback_allowed": False},
                    "p0_v8_gate": {"passed": True},
                    "review_v3": {"required_stack": "1+1", "evidence_frozen": True},
                    "promotion_gate": {"current_promotion_allowed": True},
                }
            )
            table["stages"].append(candidate_row)
            table_path.write_text(json.dumps(table), encoding="utf-8")
            current_path = root / "config/current_stage.json"
            current = json.loads(current_path.read_text(encoding="utf-8"))
            current["bridge_trigger"]["live_motion_authorized"] = False
            current_path.write_text(json.dumps(current), encoding="utf-8")
            frozen = {
                "ok": True,
                "program": V30,
                "review_v3": {"composite_fingerprint": "2" * 64},
                "live_motion_authorized": False,
            }

            with mock.patch.object(promote, "verify_v30_evidence_freeze", return_value=frozen):
                result = promote.promote(root, V30, TARGET_DIR, candidate, v30_manifest)

            self.assertTrue(result["ok"])
            self.assertEqual(result["promotion_evidence"], frozen)
            current = json.loads(current_path.read_text(encoding="utf-8"))
            self.assertEqual(current["program"], V30)
            self.assertFalse(current["bridge_trigger"]["live_motion_authorized"])
            self.assertEqual(current["v30_promotion_evidence"], frozen)
            rows = {
                row["id"]: row
                for row in json.loads(table_path.read_text(encoding="utf-8"))["stages"]
            }
            self.assertTrue(rows[V30]["package_delivery"]["controller_readback_verified"])
            self.assertFalse(rows[V30]["promotion_gate"]["bridge_start_allowed"])
            self.assertTrue(rows[V30]["contact_policy"]["dls_shadow_only"])
            self.assertFalse(rows[V30]["contact_policy"]["dls_fallback_allowed"])
            self.assertNotIn("stage25_control_modes", rows[V30]["contact_policy"])

    def test_v29_freeze_for_v30_preserves_historical_evidence_payloads(self) -> None:
        evidence = {"sha256": {".script": "a" * 64}, "portable": "frozen"}
        package = {"sha256": {".script": "a" * 64}, "controller_readback_status": "verified"}
        table = {
            "stages": [
                {
                    "id": V29,
                    "active": True,
                    "blocked": False,
                    "complete": False,
                    "completion_target": True,
                    "local_analysis_evidence": json.loads(json.dumps(evidence)),
                    "package_delivery": json.loads(json.dumps(package)),
                    "current_binding": {"is_current": True},
                    "lifecycle": {"current_candidate": True},
                }
            ]
        }

        promote.freeze_v29_fallback_for_v30(table)

        row = table["stages"][0]
        self.assertEqual(row["local_analysis_evidence"], evidence)
        self.assertEqual(row["package_delivery"], package)
        self.assertFalse(row["active"])
        self.assertTrue(row["blocked"])
        self.assertFalse(row["current_binding"]["is_current"])

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

    def test_promote_v24_overwrites_v23_bridge_profile_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            v24_dir, manifest_path = _write_v24_fixture(root)

            result = promote.promote(root, V24, TARGET_DIR, v24_dir, manifest_path)

            self.assertTrue(result["ok"])
            current = json.loads((root / "config" / "current_stage.json").read_text(encoding="utf-8"))
            self.assertEqual(current["program"], V24)
            bridge_profile = current["bridge_profile"]
            self.assertEqual(bridge_profile["step4e_version"], V24)
            self.assertEqual(
                bridge_profile["sensor_hard_guards"],
                {"raw_normal_n": 25.0, "force_norm_n": 25.0, "torque_norm_nm": 4.0},
            )
            self.assertIn("writes zero qdot", bridge_profile["cage_primary_policy"])
            self.assertNotIn("remains active_reacquire_solver", bridge_profile["cage_primary_policy"])
            self.assertNotIn("step5d_reacquire_predicted_tcp_speed_cap_m_s", bridge_profile)
            self.assertIn("stage25_low_load_policy", bridge_profile)
            self.assertIn("stage25_post_rnn_tracking_guard", bridge_profile)

    def test_promote_v26_uses_v26_candidate_row_not_previous_v25_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            v26_dir, manifest_path = _write_v26_fixture(root)

            result = promote.promote(root, V26, TARGET_DIR, v26_dir, manifest_path)

            self.assertTrue(result["ok"])
            self.assertEqual(result["previous_program"], V25)
            table = json.loads((root / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
            rows = {row["id"]: row for row in table["stages"]}
            self.assertFalse(rows[V25]["active"])
            self.assertTrue(rows[V25]["complete"])
            self.assertTrue(rows[V26]["active"])
            self.assertEqual(rows[V26]["guard"]["line_entry_normal_load_min_n"], 7.0)
            self.assertEqual(rows[V26]["guard"]["line_entry_normal_load_max_n"], 18.0)
            self.assertEqual(rows[V26]["guard"]["line_entry_raw_sanity_min_n"], 5.0)
            self.assertEqual(rows[V26]["guard"]["line_entry_raw_sanity_max_n"], 20.0)
            self.assertEqual(rows[V26]["guard"]["line_entry_recovery_normal_load_max_n"], 24.0)
            self.assertEqual(rows[V26]["guard"]["attitude_cap_rad_s"], 0.015)
            self.assertEqual(rows[V26]["operator_lifecycle"]["expected_program"], f"{TARGET_DIR}/{V26}.urp")
            self.assertEqual(rows[V26]["runtime_interface_ref"]["stage25_default_control_mode"], "speedl_cartesian_oracle")
            self.assertEqual(rows[V26]["contact_policy"]["tp_role"], "multimode_executor_and_guard_only")
            self.assertEqual(rows[V26]["contact_policy"]["default_stage25_control_mode"], "speedl_cartesian_oracle")
            current = json.loads((root / "config" / "current_stage.json").read_text(encoding="utf-8"))
            self.assertEqual(current["program"], V26)
            self.assertEqual(current["bridge_profile"]["stage25_control_mode"], "speedl_cartesian_oracle")
            self.assertIn("7-18N", current["bridge_profile"]["stage25_3_preload_gate"])

    def test_promote_v27_records_doubled_tp_play_wait_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            v26_dir, manifest_path = _write_v26_fixture(root)
            promote.promote(root, V26, TARGET_DIR, v26_dir, manifest_path)
            table_path = root / "config" / "step5_stage_table.json"
            table = json.loads(table_path.read_text(encoding="utf-8"))
            table["bridge_startup_policy"] = {
                "observed_timing": {
                    "current_step5d_tp_play_wait_max_s": 10,
                }
            }
            table_path.write_text(json.dumps(table), encoding="utf-8")
            v27_dir = root / "candidate_v27"
            v27_sha = _write_triplet(v27_dir, V27, "v27")
            v27_manifest = _write_readback(root, V27, v27_dir, v27_sha)

            result = promote.promote(root, V27, TARGET_DIR, v27_dir, v27_manifest)

            self.assertTrue(result["ok"])
            self.assertEqual(result["previous_program"], V26)
            table = json.loads(table_path.read_text(encoding="utf-8"))
            self.assertEqual(
                table["bridge_startup_policy"]["observed_timing"]["current_step5d_tp_play_wait_max_s"],
                20,
            )
            rows = {row["id"]: row for row in table["stages"]}
            self.assertEqual(rows[V27]["policy_refs"]["bridge_startup_policy"], "bridge_startup_policy")
            self.assertNotIn("applies_to_stage_ids", table["bridge_startup_policy"])
            self.assertEqual(rows[V27]["operator_lifecycle"]["wait_for_play_s"], 20)
            self.assertEqual(rows[V27]["operator_lifecycle"]["autowatch_wait_for_play_s"], 20)
            self.assertEqual(rows[V27]["guard"]["raw_normal_guard_n"], 50.0)
            self.assertEqual(rows[V27]["guard"]["force_norm_guard_n"], 60.0)
            self.assertEqual(rows[V27]["guard"]["torque_norm_guard_nm"], 3.0)
            self.assertEqual(rows[V27]["guard"]["stage25_cadence_max_gap_s"], 0.020)
            self.assertEqual(
                rows[V26]["local_delivery_evidence"]["controller_target"],
                f"{TARGET_DIR}/step5d/{V26}.urp",
            )
            self.assertEqual(rows[V26]["operator_lifecycle"]["expected_program"], f"{TARGET_DIR}/step5d/{V26}.urp")
            current = json.loads((root / "config" / "current_stage.json").read_text(encoding="utf-8"))
            self.assertEqual(current["bridge_profile"]["sensor_hard_guards"]["raw_normal_n"], 50.0)
            self.assertEqual(current["bridge_profile"]["sensor_hard_guards"]["force_norm_n"], 60.0)
            self.assertEqual(current["bridge_profile"]["sensor_hard_guards"]["torque_norm_nm"], 3.0)
            self.assertIn("stage25_cadence_consumption_instrumentation", current["bridge_profile"])
            self.assertEqual(
                current["evidence"]["v26_local_triplet"],
                f"programs/step5/step5d/{V26}",
            )
            self.assertEqual(current["evidence"]["v26_controller_target"], f"{TARGET_DIR}/step5d/{V26}.urp")
            self.assertEqual(current["evidence"]["v27_local_triplet"], f"programs/step5/{V27}")

    def test_promote_v29_fails_before_mutating_current_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            v26_dir, manifest_path = _write_v26_fixture(root)
            promote.promote(root, V26, TARGET_DIR, v26_dir, manifest_path)
            table_path = root / "config" / "step5_stage_table.json"
            table = json.loads(table_path.read_text(encoding="utf-8"))
            table["bridge_startup_policy"] = {
                "observed_timing": {
                    "current_step5d_tp_play_wait_max_s": 20,
                }
            }
            table_path.write_text(json.dumps(table), encoding="utf-8")

            v28_dir = root / "candidate_v28"
            v28_sha = _write_triplet(v28_dir, V28, "v28")
            v28_manifest = _write_readback(root, V28, v28_dir, v28_sha)
            promote.promote(root, V28, TARGET_DIR, v28_dir, v28_manifest)

            v29_dir = root / "candidate_v29"
            v29_sha = _write_triplet(v29_dir, V29, "v29")
            v29_manifest = _write_readback(root, V29, v29_dir, v29_sha)
            table_before = table_path.read_bytes()
            current_path = root / "config" / "current_stage.json"
            current_before = current_path.read_bytes()

            with self.assertRaisesRegex(
                RuntimeError,
                "ARCHIVED_PROFILE.*step5d_strict_rnn_autotune_v3_r012",
            ):
                promote.promote(root, V29, TARGET_DIR, v29_dir, v29_manifest)

            self.assertEqual(table_path.read_bytes(), table_before)
            self.assertEqual(current_path.read_bytes(), current_before)


if __name__ == "__main__":
    unittest.main()
