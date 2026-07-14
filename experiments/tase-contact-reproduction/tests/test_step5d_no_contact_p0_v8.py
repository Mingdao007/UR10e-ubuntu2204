#!/usr/bin/env python3
"""Offline package, runtime, authorization, and evidence checks for P0 v8."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_liveprep as liveprep  # noqa: E402
import step5d_p0_v8_gate as gate  # noqa: E402
import step5d_p0_v8_bridge as p0_bridge  # noqa: E402
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
    def test_stage_reset_retains_prewarmed_v30_runtime(self) -> None:
        state = p0_bridge.base.BridgeState()
        diagnostics = object()
        policy = object()
        state.step5d_v30_deferred_diagnostics = diagnostics
        state.step5d_v30_policy = policy

        state.reset_line_contact()

        self.assertIs(state.step5d_v30_deferred_diagnostics, diagnostics)
        self.assertIs(state.step5d_v30_policy, policy)

    def test_bridge_ready_requires_live_sensor_and_complete_v30_runtime(self) -> None:
        args = SimpleNamespace(
            bridge_profile=PROFILE,
            sensor_stale_s=0.1,
            rtde_hz=500.0,
            output_dir=ROOT / "runs" / "test",
        )
        state = p0_bridge.base.BridgeState()
        state.step5d_model_bundle = object()
        state.step5d_tcp_offset_tool0 = object()
        state.step5d_solver = object()
        state.step5d_v30_deferred_diagnostics = object()
        state.step5d_v30_policy = object()
        metadata = {
            "runtime_scheduler": {"policy": "SCHED_FIFO", "priority": 20}
        }
        prewarm = {"status": "ok"}

        missing_sensor = p0_bridge.base.step5d_bridge_ready_payload(
            args,
            metadata,
            state,
            prewarm,
            rtde_connected=True,
            rtde_send_succeeded=True,
            samples=0,
            baseline_ready=False,
            sensor_age_s=float("inf"),
            parse_errors=0,
        )
        self.assertIsNone(missing_sensor)

        ready = p0_bridge.base.step5d_bridge_ready_payload(
            args,
            metadata,
            state,
            prewarm,
            rtde_connected=True,
            rtde_send_succeeded=True,
            samples=1001,
            baseline_ready=True,
            sensor_age_s=0.001,
            parse_errors=0,
        )
        self.assertIsNotNone(ready)
        assert ready is not None
        self.assertEqual(ready["ready_schema"], "step5d_bridge_ready_v2")
        self.assertTrue(ready["sensor_stream_ready"])
        self.assertTrue(ready["v30_runtime_complete"])

        state.step5d_v30_policy = None
        self.assertIsNone(
            p0_bridge.base.step5d_bridge_ready_payload(
                args,
                metadata,
                state,
                prewarm,
                rtde_connected=True,
                rtde_send_succeeded=True,
                samples=1001,
                baseline_ready=True,
                sensor_age_s=0.001,
                parse_errors=0,
            )
        )

    def test_qualified_canary_clock_resets_across_unconsumed_tick(self) -> None:
        state = p0_bridge.base.BridgeState()

        self.assertEqual(
            p0_bridge.update_qualified_consumed_time(
                state, consumed=True, now_s=10.000
            ),
            0.0,
        )
        self.assertAlmostEqual(
            p0_bridge.update_qualified_consumed_time(
                state, consumed=True, now_s=10.002
            ),
            0.002,
        )
        self.assertEqual(
            p0_bridge.update_qualified_consumed_time(
                state, consumed=False, now_s=10.004
            ),
            0.0,
        )
        self.assertEqual(
            p0_bridge.update_qualified_consumed_time(
                state, consumed=True, now_s=10.006
            ),
            0.0,
        )

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
        self.assertIn("DEADLINE_OVERRUN_LAST_COMMAND_HOLD", script)
        self.assertIn("if not heartbeat_fresh:", script)
        self.assertIn("local stage25_have_accepted_command = False", script)
        self.assertIn("stage25_have_accepted_command = True", script)
        self.assertIn(
            "stage25_command_consumed = 0\n      write_output_float_register(47, stage25_command_consumed)",
            script,
        )
        self.assertIn(
            "else:\n            sync()\n          end\n        elif cmd_valid < 0.5",
            script,
        )
        self.assertIn("reuses only the last", txt)
        self.assertIn("if stale_s2 > 0.250:", script)

        p0_spec = liveprep.spec_for(PROFILE)
        self.assertEqual(p0_spec.stage25_stale_command_hold_s, 0.250)
        self.assertTrue(p0_spec.publish_guard_approved_late_command)
        v30_spec = liveprep.spec_for(interface.STEP5D_ABLATION_V30_STAGE_ID)
        self.assertEqual(v30_spec.stage25_stale_command_hold_s, 0.020)
        self.assertFalse(v30_spec.publish_guard_approved_late_command)

        bridge_source = (ROOT / "tools" / "step5d_p0_v8_bridge.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "qualified_s >= requested_phase_s",
            bridge_source,
        )
        self.assertNotIn(
            "state.step5d_active_stage25_s >= canary_phase_s",
            bridge_source,
        )

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
        self.assertEqual(runtime.hard_contract["runtime_profile"]["inner_iterations"], 512)
        self.assertEqual(
            runtime.hard_contract["runtime_scheduler"],
            {"policy": "SCHED_FIFO", "priority": 20},
        )
        self.assertTrue(runtime.hard_contract["v30_control_contract"])
        self.assertTrue(runtime.hard_contract["offline_candidate"])
        capture = json.loads(
            (ROOT / "config" / "current_stage.json").read_text(encoding="utf-8")
        )["bridge_trigger"]["no_contact_p0_v8_capture"]
        expected_target = (
            "/programs/andyl/kunwei/step5/step5d_strict_rnn_no_contact_p0_v8.urp"
            if capture["controller_readback_verified"]
            else "LOCAL_ONLY_NOT_DELIVERED"
        )
        self.assertEqual(runtime.controller_target, expected_target)
        self.assertIn("accepts only 47=524", runtime.register_contract["stage25_0"])
        self.assertEqual(
            json.loads(
                (ROOT / "config" / "current_stage.json").read_text(encoding="utf-8")
            )["p0_v8_candidate"]["canary_policy"],
            {
                "enabled": True,
                "allowed_phases_s": [2.0, 10.0, 60.0],
                "serial_same_fingerprint_sequence_required": True,
                "final_continuous_phase_s": 60.0,
            },
        )

    def test_parser_gate_allows_only_explicit_v8_canary_phases(self) -> None:
        self.assertEqual(gate.validate_canary_phase(PROFILE, 0.0), 0.0)
        self.assertEqual(gate.validate_canary_phase(PROFILE, 2.0), 2.0)
        self.assertEqual(gate.validate_canary_phase(PROFILE, 10.0), 10.0)
        self.assertEqual(gate.validate_canary_phase(PROFILE, 60.0), 60.0)
        with self.assertRaisesRegex(ValueError, "exactly 2, 10, or 60"):
            gate.validate_canary_phase(PROFILE, 5.0)
        with self.assertRaisesRegex(ValueError, "restricted to P0 v8"):
            gate.validate_canary_phase("step5d_strict_rnn_ablation_v29", 60.0)
        bridge_source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("validate_p0_v8_canary_phase(", bridge_source)
        self.assertIn("authorize_p0_v8_canary(args, current)", bridge_source)

    def test_canary_authorization_requires_same_fingerprint_serial_sequence(self) -> None:
        fingerprint = "a" * 64
        current = {
            "p0_v8_candidate": {
                "evidence_frozen": True,
                "composite_fingerprint": fingerprint,
                "completed_canaries": [],
                "review_v3": {
                    "policy_id": "ur10e_review_policy_v3",
                    "required_stack": "0+0",
                    "status": "not_required",
                    "composite_fingerprint": fingerprint,
                    "deterministic_canaries_still_required": True,
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
        current["p0_v8_candidate"]["completed_canaries"] = [
            {
                "phase_s": 2.0,
                "composite_fingerprint": fingerprint,
                "canary_passed": True,
            },
            {
                "phase_s": 10.0,
                "composite_fingerprint": fingerprint,
                "canary_passed": True,
            },
        ]
        args = SimpleNamespace(step5d_stop_register_canary_s=60.0)

        with mock.patch.object(gate, "validate_completed_canary_ledger") as ledger:
            result = gate.authorize_canary(args, current)
        ledger.assert_called_once_with(current["p0_v8_candidate"], fingerprint, (2.0, 10.0))
        self.assertEqual(result["phase_s"], 60.0)
        self.assertEqual(result["composite_fingerprint"], fingerprint)

    def test_canary_authorization_rejects_retired_review_v2_waiver(self) -> None:
        fingerprint = "b" * 64
        current = {
            "p0_v8_candidate": {
                "evidence_frozen": True,
                "composite_fingerprint": fingerprint,
                "completed_canaries": [],
                "review_v2": {
                    "status": "waived_by_user",
                    "composite_fingerprint": fingerprint,
                    "manifest": None,
                    "waiver": {
                        "waiver_id": "user-direct-bridge-20260714",
                        "issued_at": "2026-07-14T05:00:00+08:00",
                        "authorized_by": "user",
                        "explicit": True,
                        "scope": "p0_v8_pre_live_review",
                        "composite_fingerprint": fingerprint,
                        "authorization_evidence": "直接开bridge吧 不要再review了",
                        "reason": "user explicitly waived the pre-live review",
                    },
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
        args = SimpleNamespace(step5d_stop_register_canary_s=60.0)

        with self.assertRaisesRegex(ValueError, "Review v3 0\+0"):
            gate.authorize_canary(args, current)

    def test_contract_rows_require_dls_shadow_but_never_use_it_as_fallback(self) -> None:
        rows = [accepted_contract_row(), accepted_contract_row(terminal=True)]

        blockers, metrics = verifier.validate_contract_rows(rows, phase_s=60.0)

        self.assertEqual(blockers, [])
        self.assertEqual(metrics["consumption_ratio"], 1.0)
        self.assertEqual(metrics["terminal_rows"], 1)
        rows[0]["_step5d_dls_shadow_runtime_fallback_allowed"] = "1"
        blockers, _ = verifier.validate_contract_rows(rows, phase_s=60.0)
        self.assertIn("dls_runtime_fallback_observed", blockers)


if __name__ == "__main__":
    unittest.main()
