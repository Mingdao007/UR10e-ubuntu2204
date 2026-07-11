#!/usr/bin/env python3
"""Offline package, runtime, authorization, and evidence checks for P0 v8."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_liveprep as liveprep  # noqa: E402
import step5d_p0_v8_gate as gate  # noqa: E402
import step5d_runtime_interface as interface  # noqa: E402
import verify_step5d_no_contact_p0_v8 as verifier  # noqa: E402


PROFILE = interface.STEP5D_NO_CONTACT_P0_V8_STAGE_ID


def accepted_contract_row(*, terminal: bool = False) -> dict[str, str]:
    row = {
        "ur_output_double_register_35": "25.0",
        "_step5d_stage25_control_mode": "speedj_rnn_live",
        "_step5d_stage25_echo_consumed": "1",
        "_step5d_p0_rnn_accepted": "1",
        "_step5d_p0_safe_hold_active": "0",
        "_step5d_solver_status": "40",
        "_step5d_p0_v8_contract_active": "1",
        "_step5d_dls_shadow_present": "1",
        "_step5d_dls_shadow_runtime_fallback_allowed": "0",
        "_step5d_dls_shadow_normal_sign_difference": "0",
        "_step5d_constraint_residual_norm": "0.00001",
        "_step5d_qdot_max_abs_rad_s": "0.01",
        "_step5d_p0_effective_ko": "0.01",
        "_step5d_dls_shadow_residual_norm": "0.00002",
        "_step5d_dls_shadow_qdot_delta_norm": "0.001",
        "_step5d_dls_shadow_twist_delta_norm": "0.001",
        "_step5d_p0_v8_canary_stop_active": "1" if terminal else "0",
        "step4e_cmd_vx_m_s": "0" if terminal else "0.001",
        "step4e_cmd_vy_m_s": "0",
        "step4e_cmd_vz_m_s": "0",
        "step4e_cmd_wx_rad_s": "0",
        "step4e_cmd_wy_rad_s": "0",
        "step4e_cmd_wz_rad_s": "0",
        "stop_request": "1" if terminal else "0",
        "step4e_cmd_valid": "1",
    }
    return row


class Step5dNoContactP0V8Test(unittest.TestCase):
    def test_generated_triplet_is_hash_bound_and_joint_layout_only(self) -> None:
        stem = ROOT / "programs" / "step5" / "step5d" / PROFILE
        marker_path = stem.parent / f".{PROFILE}.local_candidate.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        script = stem.with_suffix(".script").read_text(encoding="utf-8")
        txt = stem.with_suffix(".txt").read_text(encoding="utf-8")

        self.assertTrue(marker["local_only"])
        self.assertTrue(marker["not_delivered"])
        self.assertEqual(marker["program"], PROFILE)
        self.assertEqual(len(marker["semantic_fingerprint"]), 64)
        for ext in (".script", ".txt", ".urp"):
            path = stem.with_suffix(ext)
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(), marker["sha256"][ext]
            )
        self.assertIn("accepts only register 47=524.0", script)
        self.assertIn("if cmd_valid < 0.5 or not joint_layout_ok", script)
        self.assertNotIn("speedl([cmd_vx", script)
        self.assertIn("local qdot_cap_rad_s = 0.050", script)
        self.assertIn("Low-load effective_ko is 0.01", txt)
        self.assertIn("DLS shadow-only", script + txt)
        self.assertIn("DEADLINE_OVERRUN_HOLD", script)
        self.assertIn("if not heartbeat_fresh:", script)
        self.assertIn(
            "speedj([0.0, 0.0, 0.0, 0.0, 0.0, 0.0]", script
        )
        self.assertIn("A repeated heartbeat is never consumed", txt)
        self.assertIn("if stale_s2 > 0.006:", script)

        spec = liveprep.spec_for(PROFILE)
        liveprep.validate_package(
            script,
            txt,
            stem.with_suffix(".urp").read_bytes(),
            marker["stamp"],
            spec,
        )

    def test_runtime_interface_binds_v30_contract_without_live_claim(self) -> None:
        runtime = interface.resolve_runtime_interface(program=PROFILE, root=ROOT, env={})

        self.assertEqual(runtime.program, PROFILE)
        self.assertEqual(runtime.hard_contract["runtime_profile"]["qdot_cap_rad_s"], 0.05)
        self.assertEqual(runtime.hard_contract["runtime_profile"]["inner_iterations"], 128)
        self.assertTrue(runtime.hard_contract["v30_control_contract"])
        self.assertTrue(runtime.hard_contract["offline_candidate"])
        self.assertEqual(runtime.controller_target, "LOCAL_ONLY_NOT_DELIVERED")
        self.assertIn("accepts only 47=524", runtime.register_contract["stage25_0"])

    def test_parser_gate_allows_only_explicit_v8_canary_phases(self) -> None:
        self.assertEqual(gate.validate_canary_phase(PROFILE, 0.0), 0.0)
        self.assertEqual(gate.validate_canary_phase(PROFILE, 10.0), 10.0)
        with self.assertRaisesRegex(ValueError, "exactly 2, 10, or 60"):
            gate.validate_canary_phase(PROFILE, 3.0)
        with self.assertRaisesRegex(ValueError, "restricted to P0 v8"):
            gate.validate_canary_phase("step5d_strict_rnn_ablation_v29", 2.0)
        bridge_source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("validate_p0_v8_canary_phase(", bridge_source)
        self.assertIn("authorize_p0_v8_canary(args, current)", bridge_source)

    def test_canary_authorization_is_fingerprint_and_sequence_bound(self) -> None:
        fingerprint = "a" * 64
        current = {
            "p0_v8_candidate": {
                "evidence_frozen": True,
                "composite_fingerprint": fingerprint,
                "completed_canaries": [],
                "review_v2": {
                    "status": "accepted",
                    "composite_fingerprint": fingerprint,
                    "manifest": "runs/review.json",
                },
            },
            "bridge_trigger": {
                "no_contact_p0_v8_capture": {
                    "profile": PROFILE,
                    "controller_readback_verified": True,
                    "capture_authorized": True,
                    "sha256": {".script": "1" * 64},
                }
            },
        }
        args = SimpleNamespace(step5d_stop_register_canary_s=10.0)

        with self.assertRaisesRegex(ValueError, "requires prior 2s pass"):
            gate.authorize_canary(args, current)
        current["p0_v8_candidate"]["completed_canaries"] = [
            {
                "phase_s": 2.0,
                "composite_fingerprint": fingerprint,
                "canary_passed": True,
            }
        ]
        result = gate.authorize_canary(args, current)
        self.assertEqual(result["phase_s"], 10.0)
        self.assertEqual(result["composite_fingerprint"], fingerprint)

    def test_contract_rows_require_dls_shadow_but_never_use_it_as_fallback(self) -> None:
        rows = [accepted_contract_row(), accepted_contract_row(terminal=True)]

        blockers, metrics = verifier.validate_contract_rows(rows, phase_s=2.0)

        self.assertEqual(blockers, [])
        self.assertEqual(metrics["consumption_ratio"], 1.0)
        self.assertEqual(metrics["terminal_rows"], 1)
        rows[0]["_step5d_dls_shadow_runtime_fallback_allowed"] = "1"
        blockers, _ = verifier.validate_contract_rows(rows, phase_s=2.0)
        self.assertIn("dls_runtime_fallback_observed", blockers)


if __name__ == "__main__":
    unittest.main()
