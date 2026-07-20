import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
CLAIM = "UR10e 500 Hz force-domain diffusion adaptation"


def load(name: str) -> dict:
    return json.loads((CONFIG / name).read_text(encoding="utf-8"))


class TacDiffusionManifestTests(unittest.TestCase):
    def test_upstream_source_and_noncommercial_notice_are_pinned(self) -> None:
        lock = load("tacdiffusion_upstream_lock.json")
        self.assertEqual(
            lock["repository"]["commit"],
            "6a5567c829c54b7d03164cf40779d2451de4099e",
        )
        self.assertEqual(lock["paper"]["version"], "v2")
        self.assertRegex(lock["paper"]["pdf_sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(lock["repository"]["license_file_present"])
        self.assertEqual(
            lock["repository"]["license_status"],
            "noncommercial_notice_in_readme_not_standard_spdx",
        )
        self.assertFalse(lock["portable_scope"]["source_code_reuse_allowed"])
        self.assertIn("clean_room", lock["portable_scope"]["implementation_method"])
        self.assertEqual(lock["reference_model_configuration"]["denoising_steps"], 50)
        self.assertEqual(lock["reference_model_configuration"]["mlp_hidden_width"], 512)
        self.assertFalse(lock["active_control_enabled"])

    def test_experiment_contract_is_force_only_and_has_real_label_boundary(self) -> None:
        plan = load("tacdiffusion_experiment_plan.json")
        self.assertEqual(plan["claim"], CLAIM)
        self.assertFalse(plan["task"]["vision_enabled"])
        dimensions = plan["observation_contract"]["per_time_slice"]
        self.assertEqual(2 * sum(dimensions.values()), 36)
        self.assertTrue(plan["observation_contract"]["internal_wrench_must_not_copy_external_wrench"])
        self.assertFalse(plan["offline_replay"]["formal_label_source"])
        self.assertTrue(plan["offline_replay"]["shadow_must_preserve_active_command_bit_for_bit"])
        self.assertEqual(
            set(plan["model_configuration"]["candidate_model_rates_hz"]),
            {50, 100, 200, 500},
        )
        self.assertEqual(
            [stage["episodes"] for stage in plan["dataset_stages"]],
            [50, 200, 1500],
        )
        self.assertFalse(plan["active_model_enabled"])
        self.assertFalse(plan["live_motion_authorized"])
        self.assertFalse(plan["contact_authorized"])

    def test_upgrade_plan_cannot_predeclare_controller_readiness(self) -> None:
        preflight = load("controller_5_25_2_upgrade_preflight.json")
        self.assertEqual(preflight["target_controller_version"], "5.25.2")
        self.assertEqual(
            preflight["observed_controller"]["polyscope_version"],
            "5.26.0.140462",
        )
        self.assertEqual(
            preflight["checkpoint_decision"]["reason"],
            "target_would_be_a_downgrade",
        )
        self.assertFalse(preflight["controller_verified"])
        self.assertFalse(preflight["upgrade_route"]["upgrade_is_conditional"])
        self.assertFalse(preflight["upgrade_route"]["fallback_to_5_23_allowed"])
        self.assertFalse(preflight["upgrade_route"]["downgrade_allowed"])
        stops = " ".join(preflight["mandatory_stop_conditions"])
        self.assertIn("URCap", stops)
        self.assertIn("PROFIsafe", stops)
        boundary = preflight["installation_boundary"]
        self.assertFalse(boundary["controller_upgrade_allowed"])
        self.assertFalse(boundary["controller_upgrade_after_all_preflight_passes"])
        for prohibited in (
            "play_program",
            "start_bridge",
            "send_torque",
            "zero_force_torque",
            "produce_robot_motion",
        ):
            self.assertFalse(boundary[prohibited])

        observation_path = ROOT / preflight["observed_controller"]["evidence"]
        observation = json.loads(observation_path.read_text(encoding="utf-8"))
        self.assertEqual(
            observation["controller"]["polyscope_version"],
            "5.26.0.140462",
        )
        self.assertEqual(
            observation["checkpoint_decision"]["status"],
            "closed_no_install",
        )
        self.assertFalse(observation["checkpoint_decision"]["controller_verified"])
        self.assertFalse(
            any(observation["actions_performed_by_capture"].values())
        )

    def test_validation_ledger_stages_are_monotonic_and_isolated(self) -> None:
        ledger = load("validation_ledger.json")
        stage_order = ledger["stage_order"]
        self.assertEqual(
            stage_order,
            ["implemented", "deterministic_tested", "simulation_run", "hardware_run"],
        )
        for component in ledger["components"].values():
            seen_false = False
            for stage in stage_order:
                value = component[stage]
                self.assertIsInstance(value, bool)
                if seen_false:
                    self.assertFalse(value)
                seen_false = seen_false or not value
        self.assertEqual(
            set(ledger["independent_acceptance_domains"]),
            {"v29", "v30", "P0", "VIC", "DBIL", "Gazebo", "URSim", "TacDiffusion"},
        )
        self.assertFalse(ledger["controller_verified"])
        self.assertFalse(ledger["live_motion_authorized"])
        self.assertFalse(ledger["contact_authorized"])


if __name__ == "__main__":
    unittest.main()
