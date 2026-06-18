#!/usr/bin/env python3
"""Offline tests for the Step5b live-contact authorization status gate."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5b_authorization_status as gate  # noqa: E402


ROS2_ROUTE = "ros2_remote_control_headless"
TP_ROUTE = "tp_bridge_step5b_v1"

LOCKED = {
    "force_source": "kunwei_software_baselined_stream",
    "zero_policy": {
        "ur_zero_ftsensor_called": False,
        "kunwei_hardware_tare_or_config_written": False,
        "software_baseline_subtraction": True,
    },
    "route": ROS2_ROUTE,
    "params_source": ["config/step5_stage_table.json#step5_contact_cycloid_baseline_v1"],
    "user_step5b_contact_authorization": True,
}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def make_ledger(path: Path, *, operator_trigger: bool = False, user_auth: bool = True) -> None:
    locked = dict(LOCKED)
    locked["user_step5b_contact_authorization"] = user_auth
    write_json(path, {"version": 1, "locked": locked, "open_gates": {"operator_final_trigger_received": operator_trigger}})


def make_acceptance(path: Path, *, accepted: bool = False, entrypoint: str = "", route: str = "") -> None:
    write_json(path, {"version": 1, "accepted": accepted, "live_runner_entrypoint": entrypoint, "live_runner_route": route})


def make_readiness_summary(runs_dir: Path, *, ok: bool, force_source: str = LOCKED["force_source"], zero_policy: dict | None = None, stamp: str = "20260618_000000") -> None:
    summary = {
        "ok": ok,
        "force_source": force_source,
        "zero_policy": zero_policy if zero_policy is not None else dict(LOCKED["zero_policy"]),
        "failure_reason": None if ok else "synthetic_failure",
    }
    write_json(runs_dir / f"step5b_zero_policy_readiness_{stamp}" / "summary.json", summary)


class Step5bAuthorizationStatusTest(unittest.TestCase):
    def _paths(self, tmp: str) -> tuple[Path, Path, Path]:
        root = Path(tmp)
        return root / "ledger.json", root / "acceptance.json", root / "runs"

    def _runner(self, tmp: str) -> Path:
        runner = Path(tmp) / "ros2_live_runner.py"
        runner.write_text("print('ok')\n", encoding="utf-8")
        return runner

    def test_all_open_unmet_blocks_and_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, acceptance, runs = self._paths(tmp)
            make_ledger(ledger, operator_trigger=False)
            make_acceptance(acceptance, accepted=False)
            runs.mkdir()
            report = gate.evaluate(ledger_path=ledger, acceptance_path=acceptance, runs_dir=runs)
            self.assertFalse(report["authorized"])
            reasons = " ".join(report["blocking_reasons"])
            self.assertIn("readiness_artifact_pass", reasons)
            self.assertIn("live_runner_auditor_accepted", reasons)
            self.assertIn("operator_final_trigger_received", reasons)
            self.assertEqual(report["locked"]["route"], ROS2_ROUTE)
            rc = gate.main(["--ledger", str(ledger), "--acceptance", str(acceptance), "--runs-dir", str(runs), "--json"])
            self.assertNotEqual(rc, 0)

    def test_already_given_user_auth_is_never_reported_missing(self) -> None:
        # B2: even with everything else open, given authorization must not block.
        with tempfile.TemporaryDirectory() as tmp:
            ledger, acceptance, runs = self._paths(tmp)
            make_ledger(ledger, operator_trigger=False, user_auth=True)
            make_acceptance(acceptance, accepted=False)
            runs.mkdir()
            report = gate.evaluate(ledger_path=ledger, acceptance_path=acceptance, runs_dir=runs)
            self.assertTrue(report["user_step5b_contact_authorization"])
            self.assertFalse(any("user_step5b_contact_authorization" in r for r in report["blocking_reasons"]))

    def test_all_gates_green_authorizes_and_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, acceptance, runs = self._paths(tmp)
            runner = self._runner(tmp)
            make_ledger(ledger, operator_trigger=True, user_auth=True)
            make_acceptance(acceptance, accepted=True, entrypoint=str(runner), route=ROS2_ROUTE)
            make_readiness_summary(runs, ok=True)
            report = gate.evaluate(ledger_path=ledger, acceptance_path=acceptance, runs_dir=runs)
            self.assertTrue(report["authorized"], report["blocking_reasons"])
            self.assertFalse(report["motion_authorized"])
            self.assertFalse(report["policy_drift"]["mismatch"])
            rc = gate.main(["--ledger", str(ledger), "--acceptance", str(acceptance), "--runs-dir", str(runs), "--json"])
            self.assertEqual(rc, 0)

    def test_route_mismatch_blocks_even_when_other_gates_pass(self) -> None:
        # B1: a TP runner under a ROS2-locked ledger must be unauthorized.
        with tempfile.TemporaryDirectory() as tmp:
            ledger, acceptance, runs = self._paths(tmp)
            runner = self._runner(tmp)
            make_ledger(ledger, operator_trigger=True, user_auth=True)
            make_acceptance(acceptance, accepted=True, entrypoint=str(runner), route=TP_ROUTE)
            make_readiness_summary(runs, ok=True)
            report = gate.evaluate(ledger_path=ledger, acceptance_path=acceptance, runs_dir=runs)
            self.assertFalse(report["authorized"])
            self.assertFalse(report["open_gates"]["live_runner_auditor_accepted"]["met"])
            self.assertIn("route_mismatch", report["open_gates"]["live_runner_auditor_accepted"]["reason"])
            self.assertTrue(any("route_mismatch" in r for r in report["blocking_reasons"]))

    def test_policy_drift_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, acceptance, runs = self._paths(tmp)
            runner = self._runner(tmp)
            make_ledger(ledger, operator_trigger=True, user_auth=True)
            make_acceptance(acceptance, accepted=True, entrypoint=str(runner), route=ROS2_ROUTE)
            make_readiness_summary(runs, ok=True, force_source="ur_internal_wrench")
            report = gate.evaluate(ledger_path=ledger, acceptance_path=acceptance, runs_dir=runs)
            self.assertFalse(report["authorized"])
            self.assertTrue(report["policy_drift"]["mismatch"])
            self.assertTrue(any(r.startswith("policy_drift") for r in report["blocking_reasons"]))

    def test_accepted_true_but_missing_entrypoint_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, acceptance, runs = self._paths(tmp)
            make_ledger(ledger, operator_trigger=True, user_auth=True)
            make_acceptance(acceptance, accepted=True, entrypoint=str(Path(tmp) / "does_not_exist.py"), route=ROS2_ROUTE)
            make_readiness_summary(runs, ok=True)
            report = gate.evaluate(ledger_path=ledger, acceptance_path=acceptance, runs_dir=runs)
            self.assertFalse(report["authorized"])
            self.assertFalse(report["open_gates"]["live_runner_auditor_accepted"]["met"])
            self.assertIn("entrypoint_missing", report["open_gates"]["live_runner_auditor_accepted"]["reason"])

    def test_readiness_not_ok_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger, acceptance, runs = self._paths(tmp)
            runner = self._runner(tmp)
            make_ledger(ledger, operator_trigger=True, user_auth=True)
            make_acceptance(acceptance, accepted=True, entrypoint=str(runner), route=ROS2_ROUTE)
            make_readiness_summary(runs, ok=False)
            report = gate.evaluate(ledger_path=ledger, acceptance_path=acceptance, runs_dir=runs)
            self.assertFalse(report["authorized"])
            self.assertFalse(report["open_gates"]["readiness_artifact_pass"]["met"])


if __name__ == "__main__":
    unittest.main()
