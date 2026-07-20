import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from ur10e_vic.ursim_protocol import (
    REQUIRED_PROTOCOL_STEPS,
    dry_run_packet,
    evaluate_runtime_result,
    validate_protocol_spec,
)


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "config" / "ursim_5_25_2_protocol.json"


def load_spec() -> dict:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def passing_runtime_result() -> dict:
    spec = load_spec()
    fingerprint = validate_protocol_spec(spec, experiment_root=ROOT).protocol_fingerprint_sha256
    return {
        "protocol_fingerprint_sha256": fingerprint,
        "runtime_kind": "ursim",
        "polyscope_version": "5.25.2",
        "host": "127.0.0.1",
        "image_digest": "sha256:" + "a" * 64,
        "protocol_execution_performed": True,
        "controller_connected": False,
        "robot_motion": False,
        "step_outcomes": {step: True for step in REQUIRED_PROTOCOL_STEPS},
        "timing": {
            "duration_s": 60.0,
            "ticks": 30000,
            "p99_s": 0.0017,
            "max_s": 0.00199,
            "deadline_misses": 0,
            "nonfinite_outputs": 0,
        },
    }


class URSimProtocolTests(unittest.TestCase):
    def test_exact_5252_spec_is_hash_bound_and_deterministic_only(self) -> None:
        spec = load_spec()
        decision = validate_protocol_spec(spec, experiment_root=ROOT)
        self.assertTrue(decision.accepted, decision.blockers)
        self.assertEqual(decision.stage, "deterministic_spec_validated")
        self.assertFalse(spec["current_status"]["protocol_execution_performed"])
        self.assertFalse(spec["current_status"]["simulation_run"])

    def test_dry_run_is_structured_and_cannot_touch_runtime(self) -> None:
        packet = dry_run_packet(load_spec(), experiment_root=ROOT)
        self.assertTrue(packet["spec_valid"])
        self.assertFalse(packet["protocol_execution_performed"])
        self.assertFalse(packet["runtime_accepted"])
        self.assertFalse(packet["container_started"])
        self.assertFalse(packet["image_pulled"])
        self.assertFalse(packet["controller_connected"])
        self.assertIn("runtime_not_executed", packet["blockers"])

    def test_cli_runs_portably_without_pythonpath(self) -> None:
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        completed = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "run_ursim_5_25_2_protocol.py")],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        packet = json.loads(completed.stdout)
        self.assertTrue(packet["spec_valid"])
        self.assertFalse(packet["protocol_execution_performed"])

    def test_cli_execute_without_bound_runtime_result_fails_closed(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "run_ursim_5_25_2_protocol.py"),
                "--execute",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 2)
        packet = json.loads(completed.stdout)
        self.assertFalse(packet["protocol_execution_performed"])
        self.assertFalse(packet["runtime_accepted"])
        self.assertIn("runtime_result_required", packet["blockers"])

    def test_runtime_evidence_requires_loopback_exact_version_and_all_steps(self) -> None:
        result = passing_runtime_result()
        result["host"] = "192.168.0.10"
        result["polyscope_version"] = "5.23.0"
        result["step_outcomes"]["heartbeat_loss_controlled_stop"] = False
        decision = evaluate_runtime_result(result, load_spec(), experiment_root=ROOT)
        self.assertFalse(decision.accepted)
        self.assertIn("runtime_host_must_be_loopback", decision.blockers)
        self.assertIn("runtime_version_must_be_exact_5_25_2", decision.blockers)
        self.assertIn(
            "protocol_step_failed:heartbeat_loss_controlled_stop", decision.blockers
        )

    def test_runtime_evidence_cannot_pass_before_adapter_is_hash_bound(self) -> None:
        result = passing_runtime_result()
        unavailable = evaluate_runtime_result(result, load_spec(), experiment_root=ROOT)
        self.assertFalse(unavailable.accepted)
        self.assertIn(
            "runtime_adapter_not_implemented_or_hash_bound", unavailable.blockers
        )
        result["timing"]["max_s"] = 0.002
        result["timing"]["deadline_misses"] = 1
        rejected = evaluate_runtime_result(result, load_spec(), experiment_root=ROOT)
        self.assertIn("timing_max_not_below_2ms", rejected.blockers)
        self.assertIn("timing_deadline_miss", rejected.blockers)


if __name__ == "__main__":
    unittest.main()
