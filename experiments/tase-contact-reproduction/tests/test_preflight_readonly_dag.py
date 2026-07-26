#!/usr/bin/env python3
"""Mocked/no-motion checks for the staged read-only preflight DAG."""

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

import preflight_readonly as preflight  # noqa: E402


def _create_p0_v9_binding_fixture(root: Path) -> str:
    manifest_rel = "runs/controller_readback_step5d_strict_rnn_no_contact_p0_v9_20260714_185412/manifest.json"
    manifest_path = root / manifest_rel
    run_dir = manifest_path.parent
    run_dir.mkdir(parents=True)
    artifacts = {
        ".script": b"noop script fixture for preflight test",
        ".txt": b"noop txt fixture for preflight test",
        ".urp": b"noop urp fixture for preflight test",
    }
    file_shas = {
        extension: hashlib.sha256(content).hexdigest() for extension, content in artifacts.items()
    }
    for extension, content in artifacts.items():
        (run_dir / f"{preflight.P0_V9_PROFILE}{extension}").write_bytes(content)

    capture_sha = {
        ".script": file_shas[".script"],
        ".txt": file_shas[".txt"],
        ".urp": file_shas[".urp"],
    }

    manifest = {
        "status": "controller read-back verified",
        "target_dir": "/programs/andyl/kunwei/step5",
        "validation": {
            "program": preflight.P0_V9_PROFILE,
            "target_dir": "/programs/andyl/kunwei/step5",
            "script_node_path": f"/programs/andyl/kunwei/step5/{preflight.P0_V9_PROFILE}.script",
        },
        "sha256": {
            "local": capture_sha,
            "controller": capture_sha,
            "readback": capture_sha,
        },
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    config_root = root / "config"
    config_root.mkdir(parents=True)
    (config_root / "current_stage.json").write_text(
        json.dumps(
            {
                "bridge_trigger": {
                    "no_contact_p0_v9_capture": {
                        "profile": preflight.P0_V9_PROFILE,
                        "capture_authorized": True,
                        "controller_readback_verified": True,
                        "controller_target": f"/programs/andyl/kunwei/step5/{preflight.P0_V9_PROFILE}.urp",
                        "controller_readback_manifest": manifest_rel,
                        "sha256": capture_sha,
                    },
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (config_root / "step5_stage_table.json").write_text(
        json.dumps(
            {
                "stages": [
                    {
                        "id": preflight.P0_V9_PROFILE,
                        "package_delivery": {
                            "status": "controller_readback_verified",
                            "controller_readback_verified": True,
                            "controller_readback_manifest": manifest_rel,
                            "sha256": capture_sha,
                        },
                    },
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest_rel


class PreflightReadonlyDagTest(unittest.TestCase):
    def test_dashboard_predicate_does_not_cross_match_other_response_fields(self) -> None:
        result = preflight.dashboard_predicate({
            "remote_control": False,
            "safetymode": "NORMAL",
            "robotmode": "POWER_OFF",
            "programState": "UNKNOWN",
            "running": True,
        })
        self.assertFalse(result["ok"])
        self.assertFalse(result["checks"]["remote_control_mode"])
        self.assertFalse(result["checks"]["robot_mode"])
        self.assertFalse(result["checks"]["program_state"])

    def test_tp_local_p0_requires_local_not_remote_control(self) -> None:
        result = preflight.dashboard_predicate(
            {
                "remote_control": False,
                "safetymode": "NORMAL",
                "robotmode": "RUNNING",
                "programState": "STOPPED",
            },
            expected_remote_control=False,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["checks"]["remote_control_mode"])

    def test_step5d_contact_bridge_profiles_use_tp_local(self) -> None:
        self.assertTrue(
            preflight.bridge_profile_uses_tp_local("step5d_strict_rnn_ablation_v32")
        )
        self.assertTrue(
            preflight.bridge_profile_uses_tp_local("step5d_strict_rnn_autotune_v1")
        )
        self.assertFalse(preflight.bridge_profile_uses_tp_local("step5b_contact_cycloid_baseline_v1"))

    def test_p0_v9_uses_its_capture_binding_instead_of_current_v29(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp_root = Path(directory)
            manifest_rel = _create_p0_v9_binding_fixture(temp_root)
            result = preflight.p0_controller_binding(preflight.P0_V9_PROFILE, temp_root)

            self.assertTrue(result["ok"])
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["program"], preflight.P0_V9_PROFILE)
            self.assertEqual(
                result["controller_target"],
                "/programs/andyl/kunwei/step5/step5d_strict_rnn_no_contact_p0_v9.urp",
            )
            self.assertEqual(result["manifest"], manifest_rel)

    def test_open_probe_accepts_open_only_payloads(self) -> None:
        self.assertTrue(preflight._open({"open": True}))
        self.assertFalse(preflight._open({"ok": False, "open": True}))

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
