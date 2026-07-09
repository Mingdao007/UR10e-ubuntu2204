#!/usr/bin/env python3
"""Offline tests for Step5d v29 timing, DLS shadow, and readiness state."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
READINESS_PATH = TOOLS / "step5d_liveprep_readiness.py"
sys.path.insert(0, str(TOOLS))


def load_readiness_module():
    if not READINESS_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("step5d_liveprep_readiness", READINESS_PATH)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


readiness = load_readiness_module()


class FakeSolver:
    def __init__(self, qdot: list[float] | None = None) -> None:
        self.qdot = qdot or [0.0] * 6

    def solve(self, *, actual_q, actual_qd, target_state):  # noqa: ANN001
        class Result:
            pass

        result = Result()
        result.qdot = tuple(self.qdot)
        result.residual_norm = 0.0
        result.diagnostics = {"solve_wall_ms": 0.25}
        return result


class Step5dLiveprepReadinessTest(unittest.TestCase):
    def require_module(self):
        self.assertIsNotNone(readiness, f"missing offline readiness tool: {READINESS_PATH}")
        return readiness

    def test_state_machine_stops_at_awaiting_authorization(self) -> None:
        module = self.require_module()

        self.assertEqual(
            module.derive_workflow_state(
                package_ready=True,
                offline_ready=True,
                live_motion_authorized=False,
                bridge_has_started=False,
                live_run_state="not_started",
                reproduction_state="incomplete",
            ),
            "awaiting_live_authorization",
        )
        self.assertEqual(
            module.derive_workflow_state(
                package_ready=True,
                offline_ready=False,
                live_motion_authorized=False,
                bridge_has_started=False,
                live_run_state="not_started",
                reproduction_state="incomplete",
            ),
            "liveprep_blocked",
        )

    def test_microbenchmark_alone_cannot_make_liveprep_ready(self) -> None:
        module = self.require_module()
        evidence = {
            "runtime_profile_match": True,
            "timing": {
                "microbenchmark": {"pass": True},
                "synthetic_tick": {"pass": False},
                "safe_hold": {"pass": True},
            },
            "dls_shadow": {"pass": True, "diagnostic_only": True, "runtime_fallback_allowed": False},
        }

        result = module.evaluate_offline_evidence(evidence)

        self.assertFalse(result["pass"])
        self.assertIn("synthetic_tick_timing_failed", result["blockers"])

    def test_dls_shadow_sign_mismatch_blocks_readiness(self) -> None:
        module = self.require_module()
        shadow = module.build_dls_shadow(
            jacobian=np.eye(6),
            xdot_c=np.array([0.001, 0.0, 0.0, 0.0, 0.0, 0.0]),
            qdot_rnn=np.array([-0.001, 0.0, 0.0, 0.0, 0.0, 0.0]),
            omega_minus=np.full(6, -0.05),
            omega_plus=np.full(6, 0.05),
            approach_normal=np.array([1.0, 0.0, 0.0]),
        )

        self.assertFalse(shadow["pass"])
        self.assertFalse(shadow["normal_sign_consistent"])
        self.assertTrue(shadow["diagnostic_only"])
        self.assertFalse(shadow["runtime_fallback_allowed"])

    def test_nonfinite_synthetic_input_fails_closed_to_zero_qdot(self) -> None:
        module = self.require_module()
        result = module.run_synthetic_tick(
            FakeSolver(qdot=[0.01] * 6),
            jacobian=np.eye(6),
            xdot_c=np.array([math.nan, 0.0, 0.0, 0.0, 0.0, 0.0]),
            omega_minus=np.full(6, -0.05),
            omega_plus=np.full(6, 0.05),
            profile={"epsilon": 0.01, "sigr_exponent_r": 0.8, "qdot_cap_rad_s": 0.05},
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "nonfinite_input")
        np.testing.assert_allclose(result["qdot"], np.zeros(6))
        self.assertLess(result["wall_ms"], 2.0)

    def test_nonnumeric_and_bad_shape_synthetic_inputs_fail_closed_to_zero_qdot(self) -> None:
        module = self.require_module()
        cases = (
            {"xdot_c": ["not-a-number"] * 6},
            {"xdot_c": [0.0] * 5},
            {"jacobian": [[0.0] * 6] * 5},
            {"omega_minus": [0.0] * 5},
        )
        defaults = {
            "jacobian": np.eye(6),
            "xdot_c": np.zeros(6),
            "omega_minus": np.full(6, -0.05),
            "omega_plus": np.full(6, 0.05),
        }
        for case in cases:
            with self.subTest(case=case):
                inputs = {**defaults, **case}
                result = module.run_synthetic_tick(
                    FakeSolver(qdot=[0.01] * 6),
                    profile={"epsilon": 0.01, "sigr_exponent_r": 0.8, "qdot_cap_rad_s": 0.05},
                    **inputs,
                )
                self.assertFalse(result["accepted"])
                self.assertEqual(result["reason"], "invalid_input")
                np.testing.assert_allclose(result["qdot"], np.zeros(6))

    def test_review_source_binding_covers_live_runtime_operator_and_bridge(self) -> None:
        module = self.require_module()
        self.assertTrue(
            {
                "config/tase_protocol_table.json",
                "config/step_pose_contract_table.json",
                "config/step5_safe_frame.json",
                "tools/contact_semantics.py",
                "tools/kunwei_rtde_bridge.py",
                "tools/step_pose_contract.py",
                "tools/step5_table.py",
                "tools/step5c_calibrated_kinematics_audit.py",
                "tools/step5d_paper_outer_loop.py",
                "tools/tase_protocol_table.py",
                "tools/verify_current_stage_readback.py",
                "tools/verify_step5d_current_binding.py",
                "tools/analyze_step5d_bridge_run.py",
                "tools/summarize_stage_frequency.py",
                "scripts/step5d-liveprep-operator.sh",
                "scripts/bridge-line-operator.sh",
            }.issubset(set(module.REVIEW_SOURCE_FILES))
        )

    def test_review_source_hash_survives_only_workflow_authorization_transitions(self) -> None:
        module = self.require_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in module.REVIEW_SOURCE_FILES:
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, destination)
            before = module.reviewed_source_sha256(root)
            binding_before = module.workflow_binding_sha256(root)

            current_path = root / "config" / "current_stage.json"
            current = json.loads(current_path.read_text(encoding="utf-8"))
            current["status"] = "live_authorized"
            current["updated_at"] = "2099-01-01T00:00:00Z"
            current["liveprep_status"] = {
                "state": "awaiting_live_authorization",
                "readiness_artifact": "runs/final/liveprep_readiness.json",
                "readiness_sha256": "a" * 64,
                "blockers": [],
            }
            current["live_run_status"] = {"state": "not_started"}
            current["bridge_trigger"]["live_motion_authorized"] = True
            current["bridge_trigger"]["bridge_has_started"] = False
            current["v29_contact_candidate"]["live_authorized"] = True
            current_path.write_text(json.dumps(current), encoding="utf-8")

            table_path = root / "config" / "step5_stage_table.json"
            table = json.loads(table_path.read_text(encoding="utf-8"))
            row = next(item for item in table["stages"] if item.get("id") == "step5d_strict_rnn_ablation_v29")
            row["blocked"] = False
            row["block_reason"] = None
            row["liveprep_status"] = {
                "state": "awaiting_live_authorization",
                "readiness_artifact": "runs/final/liveprep_readiness.json",
                "readiness_sha256": "a" * 64,
            }
            table_path.write_text(json.dumps(table), encoding="utf-8")

            self.assertEqual(module.reviewed_source_sha256(root), before)
            self.assertEqual(module.workflow_binding_sha256(root), binding_before)

            current["bridge_profile"]["step5d_qdot_limit_rad_s"] = 0.06
            current_path.write_text(json.dumps(current), encoding="utf-8")
            self.assertNotEqual(module.reviewed_source_sha256(root), before)
            self.assertNotEqual(module.workflow_binding_sha256(root), binding_before)

    def test_package_evidence_hashes_only_current_stage_local_triplet_stem(self) -> None:
        module = self.require_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            program = "step5d_strict_rnn_ablation_v29"
            declared_stem = root / "programs" / "step5" / program
            decoy_stem = root / "programs" / "step5" / "step5d" / program
            declared_stem.parent.mkdir(parents=True)
            decoy_stem.parent.mkdir(parents=True)
            expected = {}
            payloads = ((".script", b"declared script"), (".txt", b"declared txt"), (".urp", b"declared urp"))
            for extension, payload in payloads:
                declared_stem.with_suffix(extension).write_bytes(payload)
                expected[extension] = hashlib.sha256(payload).hexdigest()
            current = {
                "program": program,
                "local_triplet": f"programs/step5/{program}",
                "sha256": expected,
            }
            row = {
                "package_delivery": {"sha256": expected},
                "local_delivery_evidence": {"sha256": expected},
            }

            evidence = module.package_evidence(root, current, row)
            self.assertTrue(evidence["package_hashes_match"])
            self.assertEqual(evidence["observed_local_sha256"], expected)

            for extension, payload in payloads:
                decoy_stem.with_suffix(extension).write_bytes(payload)
            declared_stem.with_suffix(".script").write_bytes(b"tampered declared script")
            evidence = module.package_evidence(root, current, row)
            self.assertFalse(evidence["package_hashes_match"])

    def test_canonical_benchmark_uses_calibrated_jacobian_and_v29_profile(self) -> None:
        module = self.require_module()
        benchmark = module.load_benchmark_contract(ROOT)

        self.assertEqual(benchmark["schema_version"], "step5d_v29_liveprep_benchmark_v1")
        self.assertEqual(benchmark["calibration_hash"], "calib_7367377276742883610")
        self.assertEqual(np.asarray(benchmark["jacobian_base_tcp"]).shape, (6, 6))
        self.assertEqual(
            benchmark["runtime_profile"],
            {
                "backend": "cupy",
                "inner_iterations": 1024,
                "epsilon": 0.01,
                "sigr_exponent_r": 0.8,
                "qdot_cap_rad_s": 0.05,
                "control_mode": "speedj_rnn_live",
                "joint_layout_code": 524.0,
            },
        )

    def test_solver_factory_uses_verified_liveprep_truth_not_pending_step5c_truth(self) -> None:
        module = self.require_module()
        captured = {}

        class StubSolver:
            def __init__(self, config):  # noqa: ANN001
                captured["config"] = config

        original = module.StrictTaseRnnSolver
        module.StrictTaseRnnSolver = StubSolver
        try:
            module._new_solver(
                ROOT,
                {
                    "backend": "numpy",
                    "inner_iterations": 1024,
                    "epsilon": 0.01,
                    "sigr_exponent_r": 0.8,
                    "qdot_cap_rad_s": 0.05,
                },
            )
        finally:
            module.StrictTaseRnnSolver = original

        self.assertEqual(
            captured["config"].paper_truth_path,
            ROOT / "config" / "step5d_liveprep_solver_gate.json",
        )

    def test_review_manifest_requires_all_saved_lanes_and_source_binding(self) -> None:
        module = self.require_module()
        self.assertFalse(module.validate_review_manifest({"ok": True})["ok"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lanes = []
            for lane in ("control_claim", "timing_runtime", "physical_operator_safety"):
                artifact = root / f"{lane}.md"
                artifact.write_text(f"# {lane}\n\nACCEPTED\n", encoding="utf-8")
                artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
                runtime_evidence = root / f"{lane}.json"
                runtime_evidence.write_text(
                    (
                        '{"schema_version":"step5d_reviewer_runtime_evidence_v1",'
                        '"model":"gpt-5.6-sol","reasoning_effort":"max",'
                        '"sandbox":"read-only","exit_code":0,'
                        f'"artifact":"{artifact.name}","artifact_sha256":"{artifact_sha256}"}}\n'
                    ),
                    encoding="utf-8",
                )
                lanes.append(
                    {
                        "id": lane,
                        "result": "accepted",
                        "artifact": artifact.name,
                        "artifact_sha256": artifact_sha256,
                        "model": "gpt-5.6-sol",
                        "reasoning_effort": "max",
                        "runtime_evidence": runtime_evidence.name,
                        "runtime_evidence_sha256": hashlib.sha256(runtime_evidence.read_bytes()).hexdigest(),
                    }
                )
            payload = {
                "schema_version": "step5d_liveprep_milestone_review_v1",
                "ok": True,
                "reviewed_source_sha256": "a" * 64,
                "lanes": lanes,
            }
            self.assertTrue(module.validate_review_manifest(payload, manifest_dir=root)["ok"])
            payload["lanes"][0]["artifact_sha256"] = "b" * 64
            self.assertFalse(module.validate_review_manifest(payload, manifest_dir=root)["ok"])

            payload["lanes"][0]["artifact_sha256"] = hashlib.sha256(
                (root / payload["lanes"][0]["artifact"]).read_bytes()
            ).hexdigest()
            runtime_path = root / payload["lanes"][0]["runtime_evidence"]
            runtime_path.write_text(
                (
                    '{"schema_version":"step5d_reviewer_runtime_evidence_v1",'
                    '"model":"other","reasoning_effort":"max","sandbox":"read-only",'
                    '"exit_code":0,"artifact":"control_claim.md",'
                    f'"artifact_sha256":"{payload["lanes"][0]["artifact_sha256"]}"}}\n'
                ),
                encoding="utf-8",
            )
            payload["lanes"][0]["runtime_evidence_sha256"] = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
            self.assertFalse(module.validate_review_manifest(payload, manifest_dir=root)["ok"])


if __name__ == "__main__":
    unittest.main()
