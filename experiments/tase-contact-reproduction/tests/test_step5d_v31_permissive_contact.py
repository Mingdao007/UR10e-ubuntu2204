#!/usr/bin/env python3
"""Offline tests for the v31 permissive-contact hard/diagnostic boundary."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_control_contract import (  # noqa: E402
    ControlCandidate,
    SafetyEnvelope,
    Step5dObservation,
)
from step5d_runtime_interface import (  # noqa: E402
    STEP5D_ABLATION_V31_STAGE_ID,
    STEP5D_V31_QDOT_CAP_RAD_S,
    STEP5D_V31_RNN_INNER_ITERATIONS,
    STEP5D_V31_SENSOR_STALE_S,
    build_stage_env,
)
from build_step5d_v31_review_binding import compute_payloads  # noqa: E402


def observation(**overrides: object) -> Step5dObservation:
    values: dict[str, object] = {
        "sequence": 1,
        "timestamp_s": 1.0,
        "q": (0.0,) * 6,
        "qd": (0.0,) * 6,
        "tcp_pose": (0.0,) * 6,
        "tcp_twist": (0.0,) * 6,
        "wrench": (0.0,) * 6,
        "jacobian": tuple(tuple(float(i == j) for j in range(6)) for i in range(6)),
        "desired_twist": (0.0, 0.0, -0.02, 0.0, 0.0, 0.0),
        "reaction_normal": (0.0, 0.0, -1.0),
        "approach_normal": (0.0, 0.0, 1.0),
        "command_frame": "base",
        "normal_frame": "base",
        "normal_motion_policy": "frame_contract_only",
    }
    values.update(overrides)
    return Step5dObservation(**values)  # type: ignore[arg-type]


def candidate(**overrides: object) -> ControlCandidate:
    values: dict[str, object] = {
        "qdot": (0.0, 0.0, -0.01, 0.0, 0.0, 0.0),
        "predicted_twist": (0.0, 0.0, -0.01, 0.0, 0.0, 0.0),
        "residual_norm": 0.01,
        "active_bounds_count": 3,
        "frame_id": "base",
        "solver_status": "40.0",
    }
    values.update(overrides)
    return ControlCandidate(**values)  # type: ignore[arg-type]


class V31PermissiveContactTest(unittest.TestCase):
    def test_motion_direction_residual_and_active_bounds_are_diagnostic(self) -> None:
        decision = SafetyEnvelope(qdot_cap_rad_s=0.5, max_residual_norm=1e-6).evaluate(
            observation(), candidate()
        )
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.action, "execute")

    def test_frame_opposition_remains_hard(self) -> None:
        decision = SafetyEnvelope(qdot_cap_rad_s=0.5).evaluate(
            observation(approach_normal=(0.0, 0.0, -1.0)), candidate()
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "normal_contract_mismatch")

    def test_qdot_cap_remains_hard(self) -> None:
        decision = SafetyEnvelope(qdot_cap_rad_s=0.5).evaluate(
            observation(),
            candidate(
                qdot=(0.501, 0.0, 0.0, 0.0, 0.0, 0.0),
                predicted_twist=(0.501, 0.0, 0.0, 0.0, 0.0, 0.0),
            ),
        )
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "qdot_bound_exceeded")

    def test_stage_env_binds_permissive_values(self) -> None:
        env = build_stage_env(STEP5D_ABLATION_V31_STAGE_ID)
        self.assertEqual(STEP5D_V31_QDOT_CAP_RAD_S, 0.5)
        self.assertEqual(STEP5D_V31_RNN_INNER_ITERATIONS, 512)
        self.assertEqual(STEP5D_V31_SENSOR_STALE_S, 2.0)
        self.assertEqual(env["MAX_NORMAL_FORCE_N"], "60")
        self.assertEqual(env["MAX_FORCE_NORM_N"], "100")
        self.assertEqual(env["MAX_TORQUE_NORM_NM"], "3.0")

    def test_resolved_operator_config_matches_dedicated_wrapper(self) -> None:
        _binding, evidence = compute_payloads()
        resolved = evidence["resolved_operator_config"]
        self.assertEqual(resolved["baseline_s"], 1.0)
        self.assertEqual(resolved["rezero_s"], 1.0)
        self.assertEqual(resolved["normal_follow_mode"], "filtered_live")
        self.assertEqual(resolved["normal_min_force_n"], 2.0)
        wrapper = (ROOT / "scripts/step5d-strict-rnn-contact-v31.sh").read_text()
        for assignment in (
            'STEP5D_BASELINE_S="1.0"',
            'STEP5D_REZERO_S="1.0"',
            'STEP5D_NORMAL_FOLLOW_MODE="filtered_live"',
            'STEP5D_NORMAL_MIN_FORCE_N="2.0"',
        ):
            self.assertIn(assignment, wrapper)

    def test_formal_timing_and_live_gate_are_bound(self) -> None:
        _binding, evidence = compute_payloads()
        self.assertEqual(evidence["timing_raw"]["path"], "config/step5d_v31_formal_timing_raw.json")
        summary = __import__("json").loads((ROOT / evidence["timing_summary"]["path"]).read_text())
        self.assertTrue(summary["overall_pass"])
        sources = evidence["source_files_sha256"]
        for path in (
            "scripts/step5d-liveprep-operator.sh",
            "scripts/bridge-line-operator.sh",
            "tools/tase_protocol_table.py",
            "tools/verify_step5d_current_binding.py",
        ):
            self.assertIn(path, sources)
        bridge = (ROOT / "tools/kunwei_rtde_bridge.py").read_text()
        self.assertIn("return verify_v31_evidence_freeze(", bridge)


if __name__ == "__main__":
    unittest.main()
