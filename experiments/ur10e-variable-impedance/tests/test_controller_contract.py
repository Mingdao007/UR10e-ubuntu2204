import copy
import unittest

from ur10e_vic.controller_contract import evaluate_controller_runtime_contract


SHA = "a" * 64


def complete_record():
    return {
        "schema": "ur10e_controller_runtime_contract/v1",
        "polyscope_version": "5.26.0.140462",
        "probe_mode": "read_only",
        "controller_verified": False,
        "identity": {
            "controller_serial": "controller",
            "robot_serial": "robot",
            "evidence_sha256": SHA,
        },
        "urcaps": [
            {
                "name": "external_control",
                "compatibility": "compatible",
                "evidence_sha256": SHA,
            }
        ],
        "configuration_artifacts": [
            {"role": role, "sha256": SHA}
            for role in ("installation", "safety_configuration", "tcp_payload")
        ],
        "calibration_artifacts": [
            {"role": role, "sha256": SHA}
            for role in (
                "robot_calibration",
                "sensor_calibration",
                "sensor_to_tcp_transform",
            )
        ],
        "runtime_readback": {
            "safety_status": "NORMAL",
            "program_state": "STOPPED",
            "dashboard": True,
            "rtde": True,
            "network": True,
            "evidence_sha256": SHA,
        },
        "direct_torque_api_readback": {
            "available_apis": [
                "direct_torque_v2",
                "get_jacobian",
                "get_coriolis_and_centrifugal_torques",
            ],
            "evidence_sha256": SHA,
        },
        "actions_observed": {
            "program_played": False,
            "bridge_started": False,
            "torque_sent": False,
            "force_torque_zeroed": False,
            "robot_motion_commanded": False,
            "controller_setting_written": False,
        },
        "authorization": {
            "live_motion": False,
            "contact": False,
            "model_active": False,
            "upload": False,
        },
    }


class ControllerRuntimeContractTests(unittest.TestCase):
    def test_complete_hash_bound_readback_can_verify_controller_only(self):
        decision = evaluate_controller_runtime_contract(complete_record())
        self.assertTrue(decision.accepted)
        self.assertTrue(decision.controller_verified)
        self.assertTrue(decision.version_observed)

    def test_version_only_observation_fails_closed(self):
        record = complete_record()
        record["urcaps"] = []
        record["configuration_artifacts"] = []
        record["calibration_artifacts"] = []
        record["direct_torque_api_readback"] = {
            "available_apis": [],
            "evidence_sha256": "",
        }
        decision = evaluate_controller_runtime_contract(record)
        self.assertTrue(decision.version_observed)
        self.assertFalse(decision.accepted)
        self.assertFalse(decision.controller_verified)
        self.assertIn("required_urcap_missing_external_control", decision.blockers)

    def test_self_asserted_verification_and_authorization_are_rejected(self):
        record = copy.deepcopy(complete_record())
        record["controller_verified"] = True
        record["runtime_readback"]["evidence_sha256"] = "missing"
        record["authorization"]["model_active"] = True
        decision = evaluate_controller_runtime_contract(record)
        self.assertFalse(decision.controller_verified)
        self.assertIn("input_must_not_self_assert_controller_verified", decision.blockers)
        self.assertIn("runtime_contract_cannot_authorize_model_active", decision.blockers)


if __name__ == "__main__":
    unittest.main()
