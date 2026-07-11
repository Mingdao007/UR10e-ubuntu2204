import hashlib
import json
import sys
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import validate_cross_step_parameter_table as validator  # noqa: E402


def add_no_contact_p0_capture_fixture(root: Path) -> None:
    table = validator.load_json(root / "config" / "step5_stage_table.json")
    current = validator.load_json(root / "config" / "current_stage.json")
    row = next(row for row in table["stages"] if row.get("id") == "step5d_strict_rnn_no_contact_p0_v7")
    delivery = row["package_delivery"]
    current["bridge_trigger"]["no_contact_p0_capture"] = {
        "profile": "step5d_strict_rnn_no_contact_p0_v7",
        "controller_target": delivery["controller_target"],
        "controller_readback_manifest": delivery["controller_readback_manifest"],
        "sha256": delivery["sha256"],
    }
    (root / "config" / "current_stage.json").write_text(json.dumps(current), encoding="utf-8")


class CrossStepParameterTableTest(unittest.TestCase):
    def test_cross_step_parameter_table_contract_passes(self) -> None:
        self.assertEqual([], validator.validate(ROOT))

    def test_step5_flow_current_summary_must_match_current_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            current = validator.load_json(ROOT / "config" / "current_stage.json")
            flow = (ROOT / "STEP5_FLOW.md").read_text(encoding="utf-8")
            (tmp_root / "STEP5_FLOW.md").write_text(
                flow.replace(current["current_stage_id"], "step5d_strict_rnn_ablation_v99", 1),
                encoding="utf-8",
            )

            failures = validator.validate(tmp_root)

        self.assertTrue(
            any("STEP5_FLOW current summary does not match current_stage.json" in failure for failure in failures),
            failures,
        )

    def test_current_v29_readback_flags_and_claim_states_are_consistent(self) -> None:
        current = validator.load_json(ROOT / "config" / "current_stage.json")
        table = validator.load_json(ROOT / "config" / "step5_stage_table.json")
        row = next(row for row in table["stages"] if row.get("id") == current["current_stage_id"])

        self.assertTrue(row["acceptance"]["controller_readback_verified"])
        self.assertTrue(current["v29_contact_candidate"]["controller_readback_verified"])
        self.assertEqual(current["liveprep_status"]["state"], "blocked")
        self.assertTrue(row["blocked"])
        self.assertEqual(row["blocked"], current["liveprep_status"]["state"] == "blocked")
        self.assertEqual(current["live_run_status"]["state"], "not_started")
        self.assertEqual(current["reproduction_status"]["state"], "incomplete")

    def test_canonical_protocol_current_program_and_mode_are_not_stale(self) -> None:
        current = validator.load_json(ROOT / "config" / "current_stage.json")
        table = validator.load_json(ROOT / "config" / "step5_stage_table.json")
        protocol = validator.load_json(ROOT / "config" / "tase_protocol_table.json")
        row = next(item for item in table["stages"] if item.get("id") == current["current_stage_id"])
        profile = protocol["experiment_profiles"]["Step5.step5d_rnn"]

        self.assertEqual(profile["current_program"], current["program"])
        self.assertEqual(profile["stage25_default_control_mode"], row["guard"]["stage25_default_control_mode"])

    def test_validator_detects_stale_canonical_protocol_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            shutil.copy2(ROOT / "STEP5_FLOW.md", tmp_root / "STEP5_FLOW.md")
            protocol_path = tmp_root / "config" / "tase_protocol_table.json"
            protocol = validator.load_json(protocol_path)
            protocol["experiment_profiles"]["Step5.step5d_rnn"]["current_program"] = "step5d_strict_rnn_ablation_v27"
            protocol_path.write_text(json.dumps(protocol), encoding="utf-8")

            failures = validator.validate(tmp_root)

        self.assertIn("canonical Step5d current_program does not match current_stage.json", failures)

    def test_v30_rejects_partial_controller_delivery_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            shutil.copytree(ROOT / "programs", tmp_root / "programs")
            shutil.copy2(ROOT / "STEP5_FLOW.md", tmp_root / "STEP5_FLOW.md")
            table_path = tmp_root / "config" / "step5_stage_table.json"
            table = validator.load_json(table_path)
            v30 = next(item for item in table["stages"] if item.get("id") == "step5d_strict_rnn_ablation_v30")
            v30["package_delivery"]["controller_target"] = "/programs/forbidden.urp"
            table_path.write_text(json.dumps(table), encoding="utf-8")

            failures = validator.validate(tmp_root)

        self.assertIn(
            "v30 inactive package controller delivery must be entirely offline or a "
            "complete manifest-bound upload+readback",
            failures,
        )

    def test_manifest_bound_delivery_accepts_offline_or_complete_readback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            program = "step5d_strict_rnn_ablation_v30"
            local_base = root / "programs" / program
            local_base.parent.mkdir(parents=True)
            hashes = {}
            for ext in validator.PACKAGE_EXTENSIONS:
                path = local_base.with_suffix(ext)
                path.write_bytes(f"{program}{ext}".encode())
                hashes[ext] = validator.file_sha256(path)
            delivery = {
                "program_basename": program,
                "local_triplet": str(local_base.relative_to(root)),
                "sha256": hashes,
                "controller_target": None,
                "controller_uploaded": False,
                "controller_readback_verified": False,
                "controller_readback_manifest": None,
            }
            state, failures = validator._validate_manifest_bound_delivery(
                root, label="v30 inactive package", delivery=delivery
            )
            self.assertEqual("offline", state)
            self.assertEqual([], failures)

            target_dir = "/programs/andyl/kunwei/step5"
            readback_dir = root / "runs" / "controller_readback_v30_test"
            readback_dir.mkdir(parents=True)
            for ext in validator.PACKAGE_EXTENSIONS:
                shutil.copy2(local_base.with_suffix(ext), readback_dir / f"{program}{ext}")
            target = f"{target_dir}/{program}.urp"
            manifest = {
                "status": "controller read-back verified",
                "target_dir": target_dir,
                "validation": {"program": program, "target_dir": target_dir},
                "target_resolution": {"controller_target": target},
                "sha256": {
                    "local": hashes,
                    "controller": hashes,
                    "readback": hashes,
                },
            }
            manifest_path = readback_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            delivery.update(
                {
                    "controller_target": target,
                    "controller_uploaded": True,
                    "controller_readback_verified": True,
                    "controller_readback_manifest": str(manifest_path.relative_to(root)),
                }
            )
            state, failures = validator._validate_manifest_bound_delivery(
                root, label="v30 inactive package", delivery=delivery
            )
            self.assertEqual("delivered", state)
            self.assertEqual([], failures)

    def test_v30_current_promotion_is_blocked_until_p0_review_timing_and_readback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT, tmp_root, dirs_exist_ok=True)
            current_path = tmp_root / "config" / "current_stage.json"
            current = validator.load_json(current_path)
            current["current_stage_id"] = validator.V30_PROGRAM
            current["program"] = validator.V30_PROGRAM
            current_path.write_text(json.dumps(current), encoding="utf-8")

            failures = validator.validate(tmp_root)

        self.assertIn(
            "v30 current promotion requires P0 v8, manifest-bound readback, ready "
            "timing/safe-hold, and accepted Review v2 2+1",
            failures,
        )

    def test_inactive_v30_accepts_manifest_bound_upload_and_readback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT, tmp_root, dirs_exist_ok=True)
            table_path = tmp_root / "config" / "step5_stage_table.json"
            table = validator.load_json(table_path)
            v30 = next(
                row for row in table["stages"] if row.get("id") == validator.V30_PROGRAM
            )
            delivery = v30["package_delivery"]
            hashes = delivery["sha256"]
            target_dir = "/programs/andyl/kunwei/step5"
            target = f"{target_dir}/{validator.V30_PROGRAM}.urp"
            readback_dir = tmp_root / "runs" / "controller_readback_v30_test"
            readback_dir.mkdir(parents=True)
            local_base = tmp_root / delivery["local_triplet"]
            for ext in validator.PACKAGE_EXTENSIONS:
                shutil.copy2(
                    local_base.with_suffix(ext),
                    readback_dir / f"{validator.V30_PROGRAM}{ext}",
                )
            manifest_path = readback_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "status": "controller read-back verified",
                        "target_dir": target_dir,
                        "validation": {
                            "program": validator.V30_PROGRAM,
                            "target_dir": target_dir,
                        },
                        "target_resolution": {"controller_target": target},
                        "sha256": {
                            "local": hashes,
                            "controller": hashes,
                            "readback": hashes,
                        },
                    }
                ),
                encoding="utf-8",
            )
            delivery.update(
                {
                    "controller_target": target,
                    "controller_uploaded": True,
                    "controller_readback_verified": True,
                    "controller_readback_manifest": str(manifest_path.relative_to(tmp_root)),
                }
            )
            readiness_path = tmp_root / v30["local_analysis_evidence"]["offline_readiness"]
            readiness = validator.load_json(readiness_path)
            readiness["package"]["controller_readback_verified"] = True
            readiness["package"]["controller_readback_manifest"] = str(
                manifest_path.relative_to(tmp_root)
            )
            readiness_path.write_text(json.dumps(readiness), encoding="utf-8")
            v30["local_analysis_evidence"]["offline_readiness_sha256"] = (
                validator.file_sha256(readiness_path)
            )
            table_path.write_text(json.dumps(table), encoding="utf-8")

            failures = validator.validate(tmp_root)

        self.assertEqual([], failures)

    def test_v30_p0_gate_cannot_drift_from_current_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT, tmp_root, dirs_exist_ok=True)
            table_path = tmp_root / "config" / "step5_stage_table.json"
            table = validator.load_json(table_path)
            v30 = next(
                row for row in table["stages"] if row.get("id") == validator.V30_PROGRAM
            )
            v30["p0_v8_gate"]["passed"] = True
            table_path.write_text(json.dumps(table), encoding="utf-8")

            failures = validator.validate(tmp_root)

        self.assertIn("v30 P0 v8 gate is inconsistent with current_stage.json", failures)

    def test_p0_v8_layout_hash_and_fingerprint_are_cross_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT, tmp_root, dirs_exist_ok=True)
            table_path = tmp_root / "config" / "step5_stage_table.json"
            table = validator.load_json(table_path)
            p0_v8 = next(
                row for row in table["stages"] if row.get("id") == validator.P0_V8_PROGRAM
            )
            p0_v8["guard"]["stage25_allowed_layout_tags"] = [523.0, 524.0]
            p0_v8["package_delivery"]["semantic_fingerprint"] = "0" * 64
            table_path.write_text(json.dumps(table), encoding="utf-8")

            failures = validator.validate(tmp_root)

        self.assertIn("P0 v8 must allow only Stage25 layout 524", failures)
        self.assertIn(
            "P0 v8 current-stage semantic fingerprint does not match stage table",
            failures,
        )
        self.assertIn("P0 v8 marker/package hash/fingerprint binding mismatch", failures)

    def test_review_v2_index_detects_historical_evidence_hash_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT, tmp_root, dirs_exist_ok=True)
            historical = tmp_root / "config" / "step5d_v30_milestone_reviews.json"
            historical.write_text(historical.read_text(encoding="utf-8") + "\n", encoding="utf-8")

            failures = validator.validate(tmp_root)

        self.assertIn(
            "Review v2 historical evidence hash/size mismatch: config/step5d_v30_milestone_reviews.json",
            failures,
        )

    def test_v30_stage_binds_current_source_solver_10k_hard_failure(self) -> None:
        table = validator.load_json(ROOT / "config" / "step5_stage_table.json")
        v30 = next(
            item
            for item in table["stages"]
            if item.get("id") == "step5d_strict_rnn_ablation_v30"
        )
        evidence = v30["local_analysis_evidence"]
        raw_path = ROOT / evidence["current_source_solver_10k_raw"]
        raw = validator.load_json(raw_path)

        self.assertEqual(
            hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            evidence["current_source_solver_10k_raw_sha256"],
        )
        self.assertEqual(raw["classification"], "failed_hard_solver_deadline")
        self.assertFalse(raw["acceptance_eligible"])
        self.assertEqual(raw["solver"]["samples"], 10_000)
        self.assertGreater(raw["solver"]["compute_deadline_miss_count"], 0)

    def test_awaiting_v29_requires_matching_readiness_pointer_and_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            shutil.copy2(ROOT / "STEP5_FLOW.md", tmp_root / "STEP5_FLOW.md")
            current_path = tmp_root / "config" / "current_stage.json"
            table_path = tmp_root / "config" / "step5_stage_table.json"
            current = validator.load_json(current_path)
            table = validator.load_json(table_path)
            row = next(item for item in table["stages"] if item.get("id") == current["current_stage_id"])
            current["liveprep_status"] = {
                "state": "awaiting_live_authorization",
                "readiness_artifact": "runs/final/liveprep_readiness.json",
                "blockers": [],
            }
            row["blocked"] = False
            row["liveprep_status"] = {
                "state": "awaiting_live_authorization",
                "readiness_artifact": "runs/final/liveprep_readiness.json",
            }
            current_path.write_text(json.dumps(current), encoding="utf-8")
            table_path.write_text(json.dumps(table), encoding="utf-8")

            failures = validator.validate(tmp_root)
            self.assertTrue(any("readiness sha" in failure for failure in failures), failures)

            current["liveprep_status"]["readiness_sha256"] = "a" * 64
            row["liveprep_status"]["readiness_sha256"] = "a" * 64
            current_path.write_text(json.dumps(current), encoding="utf-8")
            table_path.write_text(json.dumps(table), encoding="utf-8")
            failures = validator.validate(tmp_root)
            self.assertFalse(any("readiness sha" in failure for failure in failures), failures)

    def test_live_startup_gates_are_not_cacheable(self) -> None:
        table = validator.load_json(ROOT / "config" / "step5_stage_table.json")
        profile = validator.dotted_get(table, "startup_gate_profiles.prepared_fast_bridge_v1")
        live_gates = [gate for gate in profile["gates"] if gate.get("liveness_required") is True]

        self.assertGreaterEqual(len(live_gates), 5)
        self.assertTrue(all(gate.get("cacheable") is False for gate in live_gates))

    def test_bridge_startup_policy_stage_ids_are_derived_from_policy_refs(self) -> None:
        step5 = validator.load_json(ROOT / "config" / "step5_stage_table.json")
        step6 = validator.load_json(ROOT / "config" / "step6_stage_table.json")

        derived = set(validator.derived_bridge_startup_policy_stage_ids(step5, step6))
        redundant = set(step5["bridge_startup_policy"]["applies_to_stage_ids"])

        self.assertEqual(derived, redundant)
        self.assertEqual(len(derived), 11)
        self.assertIn("step5d_strict_rnn_ablation_v29", derived)
        self.assertIn("step5d_strict_rnn_no_contact_p0_v4", derived)
        self.assertIn("step5d_strict_rnn_no_contact_p0_v7", derived)
        self.assertIn("step6_contact_eight_baseline_v2", derived)

    def test_triggerable_bridge_row_without_startup_policy_ref_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            table_path = tmp_root / "config" / "step5_stage_table.json"
            table = validator.load_json(table_path)
            table["stages"].append(
                {
                    "id": "step5d_strict_rnn_ablation_v99_test",
                    "stage": "Step5d-test",
                    "owner": "bridge+TP",
                    "active": True,
                    "shape": "cycloid",
                    "frame": "config/step5_safe_frame.json",
                    "contact": True,
                    "bridge": True,
                    "success_condition": "test-only triggerable bridge row",
                    "operator_lifecycle": {
                        "entrypoint": "scripts/step5d-liveprep-operator.sh",
                        "base_operator": "scripts/bridge-line-operator.sh",
                        "mode": "line-bridge-fast",
                    },
                }
            )
            table_path.write_text(json.dumps(table), encoding="utf-8")

            failures = validator.validate(tmp_root)

        self.assertTrue(
            any("step5:step5d_strict_rnn_ablation_v99_test triggerable bridge row missing bridge_startup_policy ref" in failure for failure in failures),
            failures,
        )

    def test_no_contact_p0_delivery_must_match_current_capture_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            add_no_contact_p0_capture_fixture(tmp_root)
            table_path = tmp_root / "config" / "step5_stage_table.json"
            table = validator.load_json(table_path)
            row = next(row for row in table["stages"] if row.get("id") == "step5d_strict_rnn_no_contact_p0_v7")
            row["package_delivery"]["controller_dir"] = "/programs/andyl/kunwei/step5/step5d"
            row["package_delivery"][
                "controller_target"
            ] = "/programs/andyl/kunwei/step5/step5d/step5d_strict_rnn_no_contact_p0_v7.urp"
            table_path.write_text(json.dumps(table), encoding="utf-8")

            failures = validator.validate(tmp_root)

        self.assertTrue(
            any("strict RNN no-contact P0 package_delivery.controller_target" in failure for failure in failures),
            failures,
        )

    def test_no_contact_p0_capture_profile_must_not_be_stale_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            add_no_contact_p0_capture_fixture(tmp_root)
            current = validator.load_json(tmp_root / "config" / "current_stage.json")
            current["bridge_trigger"]["no_contact_p0_capture"]["profile"] = "step5d_strict_rnn_no_contact_p0_v2"
            (tmp_root / "config" / "current_stage.json").write_text(
                json.dumps(current),
                encoding="utf-8",
            )
            failures = validator.validate(tmp_root)

        self.assertTrue(
            any("strict RNN no-contact P0 row is missing from Step5 table: step5d_strict_rnn_no_contact_p0_v2" in failure for failure in failures),
            failures,
        )

    def test_no_contact_p0_delivery_and_capture_manifest_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            add_no_contact_p0_capture_fixture(tmp_root)
            table = validator.load_json(tmp_root / "config" / "step5_stage_table.json")
            row = next(row for row in table["stages"] if row.get("id") == "step5d_strict_rnn_no_contact_p0_v7")
            row["package_delivery"]["controller_readback_manifest"] = "runs/controller_readback_step5d_strict_rnn_no_contact_p0_v7_LOCAL_PENDING_READBACK/manifest.json"
            (tmp_root / "config" / "step5_stage_table.json").write_text(json.dumps(table), encoding="utf-8")
            current = validator.load_json(tmp_root / "config" / "current_stage.json")
            current["bridge_trigger"]["no_contact_p0_capture"]["controller_readback_manifest"] = (
                "runs/controller_readback_step5d_strict_rnn_no_contact_p0_v7_LOCAL_PENDING_READBACK/bad_manifest.json"
            )
            (tmp_root / "config" / "current_stage.json").write_text(json.dumps(current), encoding="utf-8")
            failures = validator.validate(tmp_root)

        self.assertTrue(
            any(
                "strict RNN no-contact P0 package_delivery.controller_readback_manifest does not match current capture pointer"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_v27_failed_evidence_separates_stop_reason_from_control_classification(self) -> None:
        table = validator.load_json(ROOT / "config" / "step5_stage_table.json")
        stage = next(stage for stage in table["stages"] if stage.get("id") == "step5d_strict_rnn_ablation_v27")
        timing = stage["local_analysis_evidence"]["v27_20260706_024815_live_timing"]
        shadow_experiment = stage["local_analysis_evidence"]["v27_20260706_033032_orientation_shadow_experiment"]
        force_overshoot = stage["local_analysis_evidence"]["v27_20260706_040900_step5d_outer_linear_live_fix_validation"]
        fix_validation = stage["local_analysis_evidence"][
            "v27_20260706_045513_step5b_live_step5d_shadow_fix_validation"
        ]
        evidence_refs = stage["evidence_refs"]

        self.assertEqual(timing["terminal_stop_reason"], "step5d_contact_safety:force_norm_hard_stop")
        self.assertEqual(timing["analysis_classification"], "stage25_control_force_oscillation/low_load_timeout")
        self.assertEqual(timing["control_oscillation_trigger"], "low_load_repress_window")
        self.assertEqual(shadow_experiment["terminal_stop_reason"], "v25_speedl_hard_low_load_timeout")
        self.assertEqual(
            shadow_experiment["evidence_classification"],
            "stage25_orientation_shadow_experiment_failed_low_load_timeout",
        )
        self.assertEqual(force_overshoot["summary_stop_reason"], "step5d_contact_safety:force_norm_hard_stop")
        self.assertEqual(force_overshoot["analysis_classification"], "stage25_control_force_oscillation/force_norm_hard_stop")
        self.assertEqual(
            force_overshoot["evidence_classification"],
            "old_v27_paper_outer_linear_live_gain_mismatch_force_norm_hard_stop",
        )
        self.assertEqual(fix_validation["analysis_classification"], "stage25_fix_validation_success")
        self.assertEqual(fix_validation["result"], "successful_10s_fix_validation_not_60s_reproduction")
        self.assertEqual(fix_validation["reproduction_status"], "pending_60s_step5b_equivalent_run")
        self.assertEqual(evidence_refs["latest_terminal_stop_reason"], "tp_normal_stop_reason_1")
        self.assertEqual(evidence_refs["latest_analysis_classification"], "stage25_fix_validation_success")
        self.assertEqual(
            evidence_refs["latest_evidence_classification"],
            "v27_10s_step5b_live_step5d_shadow_fix_validation_success",
        )
        self.assertEqual(
            evidence_refs["previous_failed_live_run_analysis"],
            "runs/bridge_step5d_strict_rnn_ablation_v27_20260706_040900/step5d_bridge_analysis.json",
        )


if __name__ == "__main__":
    unittest.main()
