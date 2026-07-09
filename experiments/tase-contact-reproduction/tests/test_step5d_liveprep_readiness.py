#!/usr/bin/env python3
"""Offline tests for Step5d v29 timing, DLS shadow, and readiness state."""

from __future__ import annotations

import importlib.util
import hashlib
import math
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
                runtime_evidence = root / f"{lane}.jsonl"
                runtime_evidence.write_text(
                    '{"model":"gpt-5.6-sol","reasoning_effort":"max"}\n',
                    encoding="utf-8",
                )
                lanes.append(
                    {
                        "id": lane,
                        "result": "accepted",
                        "artifact": artifact.name,
                        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
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


if __name__ == "__main__":
    unittest.main()
