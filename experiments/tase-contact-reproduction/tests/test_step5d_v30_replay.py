#!/usr/bin/env python3
"""Offline replay and evidence tests for the v29 -> v30 handoff."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import replay_step5d_v30 as replay  # noqa: E402


class Step5dV30ReplayTest(unittest.TestCase):
    def test_replay_recomputes_direction_from_explicit_normals(self) -> None:
        rows = [
            {
                "_step5d_outer_xdot_limited_approach_normal_m_s": "0.001",
                "_step5d_jqdot_raw_approach_normal_m_s": "0.0009",
                "_step5d_constraint_residual_norm": "0.0001",
                "_step5d_active_bounds_count": "0",
                "_step4e_control_normal_b_x": "0",
                "_step4e_control_normal_b_y": "0",
                "_step4e_control_normal_b_z": "-1",
                "_step5d_force_sign_convention": "step5_step6_positive_normal_load",
                "_step5d_rnn_raw_qd0_rad_s": "0.0009",
                **{f"_step5d_rnn_raw_qd{i}_rad_s": "0" for i in range(1, 6)},
            },
            {
                "_step5d_outer_xdot_limited_approach_normal_m_s": "0.001",
                "_step5d_jqdot_raw_approach_normal_m_s": "-0.0009",
                "_step5d_constraint_residual_norm": "0.0001",
                "_step5d_active_bounds_count": "0",
                "_step4e_control_normal_b_x": "0",
                "_step4e_control_normal_b_y": "0",
                "_step4e_control_normal_b_z": "-1",
                "_step5d_force_sign_convention": "step5_step6_positive_normal_load",
                "_step5d_rnn_raw_qd0_rad_s": "-0.0009",
                **{f"_step5d_rnn_raw_qd{i}_rad_s": "0" for i in range(1, 6)},
            },
        ]

        summary = replay.replay_logged_projection_rows(rows)

        self.assertEqual(summary["rows"], 2)
        self.assertEqual(summary["accepted_rows"], 1)
        self.assertEqual(summary["safe_hold_rows"], 1)
        self.assertEqual(summary["reason_counts"], {"approach_normal_unload_mismatch": 1, "ok": 1})
        self.assertFalse(summary["acceptance_pass"])

    def test_evidence_manifest_hashes_only_allowlisted_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "summary.json").write_text('{"ok": true}\n', encoding="utf-8")
            (run_dir / "stage_frequency_summary.json").write_text('{"ok": true}\n', encoding="utf-8")
            (run_dir / "step5d_bridge_analysis.json").write_text('{"ok": true}\n', encoding="utf-8")
            (run_dir / "bridge_run_manifest.json").write_text('{"profile": "v29"}\n', encoding="utf-8")
            (run_dir / "metadata.json").write_text('{"profile": "v29"}\n', encoding="utf-8")
            (run_dir / "bridge_rtde_500hz.csv").write_text("a,b\n1,2\n", encoding="utf-8")
            (run_dir / "secret.txt").write_text("must not enter manifest", encoding="utf-8")

            source_hashes = {name: hashlib.sha256((run_dir / name).read_bytes()).hexdigest() for name in replay.EVIDENCE_FILES}

            manifest = replay.build_evidence_manifest(
                run_dir,
                source_run="/remote/read-only/run",
                source_hashes=source_hashes,
            )

        self.assertEqual(manifest["schema_version"], "step5d_imported_evidence_v1")
        self.assertEqual(set(manifest["files"]), set(replay.EVIDENCE_FILES))
        expected = hashlib.sha256(b"a,b\n1,2\n").hexdigest()
        self.assertEqual(manifest["files"]["bridge_rtde_500hz.csv"]["local_sha256"], expected)
        self.assertTrue(manifest["files"]["bridge_rtde_500hz.csv"]["source_local_sha256_match"])
        self.assertTrue(manifest["copy_complete"])
        self.assertEqual(manifest["source_mode"], "read_only_copy")

    def test_csv_loader_requires_logged_projection_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "bridge_rtde_500hz.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["write_index"])
                writer.writeheader()
                writer.writerow({"write_index": "1"})

            with self.assertRaisesRegex(RuntimeError, "missing required replay columns"):
                replay.load_logged_projection_rows(csv_path)

    def test_revisit_ledger_preserves_claim_boundaries(self) -> None:
        ledger = json.loads((ROOT / "config" / "step5d_v30_revisit_ledger.json").read_text(encoding="utf-8"))

        self.assertEqual(ledger["v27"]["claim"], "successful_10s_fix_validation_not_reproduction")
        self.assertEqual(ledger["v28"]["status"], "superseded_incomplete")
        self.assertEqual(ledger["v29"]["classification"], "stage25_cadence_or_consumption_failure")
        self.assertEqual(ledger["v30"]["status"], "offline_candidate")
        self.assertFalse(ledger["v30"]["live_motion_authorized"])

    def test_legacy_unload_label_is_migrated_only_when_canonical_projection_passes(self) -> None:
        row = {
            "_step5d_outer_xdot_limited_approach_normal_m_s": "0.00008",
            "_step5d_jqdot_raw_approach_normal_m_s": "0.00007999",
            "_step5d_constraint_residual_norm": "1e-8",
            "_step5d_active_bounds_count": "0",
            "_step4e_control_normal_b_x": "0",
            "_step4e_control_normal_b_y": "0",
            "_step4e_control_normal_b_z": "-1",
            "_step5d_force_sign_convention": "step5_step6_positive_normal_load",
            "_step5d_rnn_reject_reason": "approach_normal_unload_mismatch",
            **{f"_step5d_rnn_raw_qd{i}_rad_s": "0.01" for i in range(6)},
        }

        summary = replay.replay_logged_projection_rows([row])

        self.assertEqual(summary["accepted_rows"], 1)
        self.assertEqual(summary["normal_mismatch_rows"], 0)
        self.assertEqual(summary["legacy_reason_migrated_rows"], 1)
        self.assertEqual(summary["legacy_label_migrations"], {"approach_normal_unload_mismatch->ok": 1})
        self.assertTrue(summary["acceptance_pass"])


if __name__ == "__main__":
    unittest.main()
