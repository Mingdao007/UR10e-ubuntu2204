import ast
import json
from pathlib import Path
import unittest

from ur10e_vic.ursim_transport import (
    FakeURSimTransport,
    build_request,
    evaluate_capture,
    execute_with_transport,
    validate_spec,
)


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "config" / "ursim_5_26_protocol.json"


def load_spec():
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


class URSimTransportTests(unittest.TestCase):
    def test_spec_is_hash_bound_and_request_is_loopback_only(self):
        spec = load_spec()
        self.assertEqual(validate_spec(spec, experiment_root=ROOT), ())
        request = build_request(spec, experiment_root=ROOT)
        self.assertTrue(request.execution_authorized)
        self.assertEqual(request.host, "127.0.0.1")
        self.assertEqual(request.target_version, "5.26.0")

    def test_fake_transport_never_promotes_simulation(self):
        request = build_request(load_spec(), experiment_root=ROOT)
        capture, decision = execute_with_transport(FakeURSimTransport(), request)
        self.assertTrue(capture["protocol_execution_performed"])
        self.assertFalse(decision.accepted)
        self.assertFalse(decision.simulation_run)
        self.assertEqual(decision.stage, "deterministic_tested")
        self.assertIn("non_ursim_transport_cannot_promote_simulation", decision.blockers)

    def test_well_formed_external_loopback_capture_is_evaluable(self):
        request = build_request(load_spec(), experiment_root=ROOT)
        capture = dict(FakeURSimTransport().execute(request))
        capture.update(
            {
                "transport_kind": "ursim_loopback",
                "runtime_kind": "ursim",
                "runtime_image_digest": "sha256:" + "b" * 64,
                "runtime_fingerprint_sha256": "c" * 64,
            }
        )
        decision = evaluate_capture(capture, request)
        self.assertTrue(decision.accepted, decision.blockers)
        self.assertTrue(decision.simulation_run)

    def test_module_has_no_production_io_imports(self):
        source = (ROOT / "ur10e_vic" / "ursim_transport.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports.update(
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertTrue(imports.isdisjoint({"socket", "subprocess", "docker", "rtde", "rclpy"}))


if __name__ == "__main__":
    unittest.main()
