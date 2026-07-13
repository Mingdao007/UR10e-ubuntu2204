#!/usr/bin/env python3
"""Mocked/no-motion checks for the staged read-only preflight DAG."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import preflight_readonly as preflight  # noqa: E402


class PreflightReadonlyDagTest(unittest.TestCase):
    def test_local_stage_precedes_parallel_remote_snapshot(self) -> None:
        def command(args: list[str]) -> dict[str, object]:
            payload: dict[str, object] = {
                "ok": True,
                "returncode": 0,
                "stdout": "ok",
                "stderr": "",
                "args": args,
            }
            if "--json-only" in args:
                payload["stdout"] = json.dumps(
                    {"ok": True, "issues": [], "device": "eth0", "kunwei": {}}
                )
            if args[0] == "pgrep":
                payload["stdout"] = ""
            return payload

        rtde = {
            "actual_TCP_pose": [0.0] * 6,
            "actual_TCP_speed": [0.0] * 6,
            "actual_TCP_force": [0.0] * 6,
            "actual_q": [0.0] * 6,
            "runtime_state": 1,
            "robot_mode": 7,
            "safety_mode": 1,
            "speed_scaling": 1.0,
            "payload": 1.0,
            "payload_cog": [0.0] * 3,
            "tcp_offset": [0.0] * 6,
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            preflight, "run_command", side_effect=command
        ), mock.patch.object(
            preflight, "dashboard_exchange", return_value={
                "remote_control": True,
                "safetymode": "NORMAL",
                "robotmode": "RUNNING",
                "programState": "STOPPED",
            }
        ), mock.patch.object(
            preflight, "read_rtde_once", return_value=rtde
        ), mock.patch.object(
            preflight, "probe_port", return_value={"ok": True}
        ), mock.patch.object(
            preflight, "tcp_connect_only", return_value={"ok": True, "open": True}
        ):
            rc = preflight.main(
                ["--output-dir", directory, "--timeout-s", "0.01", "--json-only"]
            )
            payload = json.loads(
                (Path(directory) / "preflight_readonly.json").read_text(encoding="utf-8")
            )
        self.assertEqual(rc, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["contract_id"], "ur10e_concurrency_contract_v1")
        self.assertEqual(set(payload["stages"]), {"local", "remote"})
        self.assertIn("controller_binding", payload["stages"]["remote"])
        self.assertIn("bench_network", payload["stages"]["remote"])

    def test_remote_stage_is_skipped_when_local_route_fails(self) -> None:
        def command(args: list[str]) -> dict[str, object]:
            ok = not (args[:4] == ["ip", "route", "get", "192.168.1.18"])
            return {
                "ok": ok,
                "returncode": 0 if ok else 2,
                "stdout": "" if args[0] == "pgrep" else "route",
                "stderr": "",
                "args": args,
            }

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            preflight, "run_command", side_effect=command
        ), mock.patch.object(preflight, "dashboard_exchange") as dashboard:
            rc = preflight.main(["--output-dir", directory])
            payload = json.loads(
                (Path(directory) / "preflight_readonly.json").read_text(encoding="utf-8")
            )
        self.assertEqual(rc, 2)
        self.assertEqual(payload["stages"]["remote"], {"skipped": "local_stage_failed"})
        dashboard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
