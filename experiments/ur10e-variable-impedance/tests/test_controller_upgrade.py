import unittest

from ur10e_vic.controller_upgrade import (
    REQUIRED_BACKUP_ROLES,
    evaluate_post_upgrade_readback,
    evaluate_upgrade_preflight,
)


HASHES = {role: (str(index + 1) * 64)[:64] for index, role in enumerate(sorted(REQUIRED_BACKUP_ROLES))}


def valid_preflight() -> dict:
    return {
        "current_polyscope_version": "5.11.9.1010452",
        "target_polyscope_version": "5.25.2",
        "probe_mode": "read_only",
        "controller_verified": False,
        "controller_serial": "controller-redacted",
        "robot_serial": "robot-redacted",
        "tcp_payload_binding_sha256": "c" * 64,
        "safety_status": "NORMAL",
        "artifacts": [
            {"role": role, "sha256": sha256} for role, sha256 in HASHES.items()
        ],
        "urcaps": [
            {"name": "External Control", "compatibility": "compatible"},
            {"name": "OnRobot", "compatibility": "compatible"},
        ],
        "profisafe": {"in_use": False, "breaking_change_reviewed": False},
        "urup_sha256": "a" * 64,
        "official_urup_sha256": "a" * 64,
        "usb_filesystem": "FAT32",
        "program_played": False,
        "bridge_started": False,
        "torque_sent": False,
        "robot_motion_observed": False,
    }


class ControllerUpgradeGateTests(unittest.TestCase):
    def test_complete_read_only_preflight_allows_only_install_step(self) -> None:
        decision = evaluate_upgrade_preflight(valid_preflight())
        self.assertTrue(decision.accepted)
        self.assertFalse(decision.controller_verified)
        self.assertEqual(decision.stage, "upgrade_preflight")

    def test_profisafe_and_unknown_urcap_stop_at_preflight(self) -> None:
        evidence = valid_preflight()
        evidence["profisafe"] = {"in_use": True, "breaking_change_reviewed": False}
        evidence["urcaps"][0]["compatibility"] = "unknown"
        decision = evaluate_upgrade_preflight(evidence)
        self.assertFalse(decision.accepted)
        self.assertIn("profisafe_breaking_change_not_reviewed", decision.blockers)
        self.assertIn("urcap_incompatible_or_unverified", decision.blockers)

    def test_hash_or_filesystem_mismatch_fails_closed(self) -> None:
        evidence = valid_preflight()
        evidence["official_urup_sha256"] = "b" * 64
        evidence["usb_filesystem"] = "exFAT"
        decision = evaluate_upgrade_preflight(evidence)
        self.assertIn("urup_sha256_missing_or_mismatch", decision.blockers)
        self.assertIn("upgrade_usb_not_fat32", decision.blockers)

    def test_existing_target_is_readback_not_reinstall(self) -> None:
        evidence = valid_preflight()
        evidence["current_polyscope_version"] = "5.25.2"
        decision = evaluate_upgrade_preflight(evidence)
        self.assertFalse(decision.accepted)
        self.assertIn(
            "target_already_installed_use_post_upgrade_readback", decision.blockers
        )

    def test_newer_controller_blocks_target_as_downgrade(self) -> None:
        evidence = valid_preflight()
        evidence["current_polyscope_version"] = "5.26.0.140462"
        decision = evaluate_upgrade_preflight(evidence)
        self.assertFalse(decision.accepted)
        self.assertFalse(decision.controller_verified)
        self.assertIn("target_would_be_a_downgrade", decision.blockers)

    def test_post_upgrade_readback_does_not_imply_motion_authorization(self) -> None:
        evidence = {
            "polyscope_version": "5.25.2",
            "controller_serial": "controller-redacted",
            "robot_serial": "robot-redacted",
            "tcp_payload_binding_sha256": "c" * 64,
            "safety_status": "NORMAL",
            "firmware_update_completed": True,
            "preserved_artifact_hashes": HASHES,
            "urcaps": [
                {"name": "External Control", "loaded": True},
                {"name": "OnRobot", "loaded": True},
            ],
            "interfaces": {"dashboard": True, "rtde": True, "network": True},
            "available_apis": [
                "direct_torque_v2",
                "get_jacobian",
                "get_coriolis_and_centrifugal_torques",
            ],
            "program_played": False,
            "bridge_started": False,
            "torque_sent": False,
            "robot_motion_observed": False,
            "live_motion_authorized": False,
        }
        decision = evaluate_post_upgrade_readback(
            evidence,
            preflight_artifact_hashes=HASHES,
            preflight_identity={
                "controller_serial": "controller-redacted",
                "robot_serial": "robot-redacted",
                "tcp_payload_binding_sha256": "c" * 64,
            },
        )
        self.assertTrue(decision.accepted)
        self.assertTrue(decision.controller_verified)
        evidence["robot_serial"] = "wrong-robot"
        identity_mismatch = evaluate_post_upgrade_readback(
            evidence,
            preflight_artifact_hashes=HASHES,
            preflight_identity={
                "controller_serial": "controller-redacted",
                "robot_serial": "robot-redacted",
                "tcp_payload_binding_sha256": "c" * 64,
            },
        )
        self.assertFalse(identity_mismatch.accepted)
        self.assertIn("robot_serial_identity_mismatch", identity_mismatch.blockers)
        evidence["robot_serial"] = "robot-redacted"
        evidence["live_motion_authorized"] = True
        self.assertFalse(
            evaluate_post_upgrade_readback(
                evidence,
                preflight_artifact_hashes=HASHES,
                preflight_identity={
                    "controller_serial": "controller-redacted",
                    "robot_serial": "robot-redacted",
                    "tcp_payload_binding_sha256": "c" * 64,
                },
            ).accepted
        )


if __name__ == "__main__":
    unittest.main()
