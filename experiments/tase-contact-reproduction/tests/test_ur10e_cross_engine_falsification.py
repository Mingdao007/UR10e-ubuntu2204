#!/usr/bin/env python3
"""Focused tests for the offline cross-engine falsification lane."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import build_ur10e_cross_engine_falsification as builder  # noqa: E402
import verify_ur10e_cross_engine_falsification as verifier  # noqa: E402


CONFIG = ROOT / "config" / "cross_engine_domain_randomization_v1.json"
GAZEBO_CONTRACT = ROOT / "config" / "gazebo_v2_lane_contract.json"


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _model_fixture(root: Path) -> Path:
    outputs: dict[str, object] = {}
    for index, key in enumerate(builder.OUTPUT_KEYS):
        path = root / f"{key}.xml"
        path.write_text(f"<mujoco model='{key}' index='{index}'/>\n", encoding="utf-8")
        outputs[key] = {
            "path": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": builder.sha256_file(path),
            "role": f"mujoco_{key}_plant",
            "claim_level": "geometry_provisional",
        }
    manifest = {
        "schema": "ur10e_mujoco_model_bundle_v1",
        "rates_hz": {"physics": 2000, "control": 500, "dbil": 200},
        "integer_schedule": {"physics_per_control": 4, "physics_per_dbil": 10},
        "outputs": outputs,
        "active_tcp_offset_tool0_m": [0.0, 0.0, 0.12209917288991741],
        "no_contact_scene": {"native_contact_enabled": True},
        "claim_boundary": {
            "calibrated_physics_claim_allowed": False,
            "p0_sim_physics_pass_allowed": False,
            "live_motion_authorized": False,
            "package_accepted": False,
            "live_accepted": False,
            "reproduction_complete": False,
        },
        "blockers": [
            "combined_eoat_mass_cog_inertia_unvalidated",
            "surface_material_contact_parameters_unvalidated",
        ],
    }
    path = root / "model_manifest.json"
    _write_json(path, manifest)
    return path


class CrossEngineFalsificationTest(unittest.TestCase):
    def test_parameter_config_has_required_falsification_coverage(self) -> None:
        config = builder.load_object(CONFIG)

        self.assertEqual(builder.validate_config(config), [])
        self.assertEqual(set(config["parameter_groups"]), set(builder.REQUIRED_PARAMETER_GROUPS))
        parameter_ids = {
            parameter_id
            for _, parameter_id, _ in builder.flattened_parameters(config)
        }
        for token in (
            "surface_height",
            "surface_tilt",
            "surface_friction",
            "contact_stiffness",
            "contact_damping",
            "tcp_",
            "eoat_",
            "force_bias",
            "sensor_delay",
            "servo_lag",
            "command_drop",
        ):
            self.assertTrue(any(token in parameter_id for parameter_id in parameter_ids), token)
        self.assertIn("force_noise_std_n", parameter_ids)
        self.assertIn("torque_noise_std_nm", parameter_ids)

    def test_seeded_builder_is_byte_deterministic_and_extrema_complete(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cross_engine_deterministic_") as tmp:
            root = Path(tmp)
            model = _model_fixture(root)
            first = root / "first"
            second = root / "second"

            first_scenarios, first_evidence = builder.build(
                mujoco_manifest_path=model,
                gazebo_contract_path=GAZEBO_CONTRACT,
                config_path=CONFIG,
                output_dir=first,
            )
            second_scenarios, second_evidence = builder.build(
                mujoco_manifest_path=model,
                gazebo_contract_path=GAZEBO_CONTRACT,
                config_path=CONFIG,
                output_dir=second,
            )

            self.assertEqual(first_scenarios.read_bytes(), second_scenarios.read_bytes())
            self.assertEqual(first_evidence.read_bytes(), second_evidence.read_bytes())
            scenarios = builder.load_object(first_scenarios)
            config = builder.load_object(CONFIG)
            expected_count = 1 + 2 * len(builder.REQUIRED_PARAMETER_GROUPS) + int(
                config["combined_scenario_count"]
            )
            self.assertEqual(scenarios["scenario_count"], expected_count)
            self.assertTrue(
                scenarios["coverage"]["all_required_groups_have_low_and_high_extrema"]
            )
            by_id = {row["id"]: row for row in scenarios["scenarios"]}
            defaults = builder.nominal_parameters(config)
            for group_name in builder.REQUIRED_PARAMETER_GROUPS:
                for bound_name, bound_key in (("low", "minimum"), ("high", "maximum")):
                    row = by_id[f"oat_{group_name}_{bound_name}"]
                    for parameter_id, bounds in config["parameter_groups"][group_name][
                        "parameters"
                    ].items():
                        self.assertEqual(row["parameters"][parameter_id], float(bounds[bound_key]))
                    unaffected = set(defaults) - set(
                        config["parameter_groups"][group_name]["parameters"]
                    )
                    self.assertTrue(
                        all(row["parameters"][name] == defaults[name] for name in unaffected)
                    )
            self.assertEqual(len({row["realization_seed"] for row in scenarios["scenarios"]}), expected_count)

    def test_missing_runtime_is_explicitly_blocked_and_cannot_promote_claims(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cross_engine_fail_closed_") as tmp:
            root = Path(tmp)
            _, evidence_path = builder.build(
                mujoco_manifest_path=_model_fixture(root),
                gazebo_contract_path=GAZEBO_CONTRACT,
                config_path=CONFIG,
                output_dir=root / "out",
            )
            evidence = builder.load_object(evidence_path)

            self.assertEqual(
                evidence["status"],
                "falsification_plan_ready_runtime_equivalence_blocked",
            )
            self.assertTrue(evidence["engine_comparison"]["static_rate_contract_match"])
            self.assertFalse(evidence["engine_comparison"]["contact_equivalence_pass"])
            self.assertFalse(evidence["engine_comparison"]["timing_equivalence_pass"])
            self.assertFalse(evidence["domain_randomization"]["robustness_pass"])
            self.assertEqual(evidence["domain_randomization"]["scenario_executed_count"], 0)
            for claim in verifier.FALSE_CLAIMS:
                self.assertFalse(evidence["claims"][claim], claim)
            for blocker in builder.RUNTIME_BLOCKERS:
                self.assertIn(blocker, evidence["blockers"])
            self.assertFalse(evidence["claim_boundary"]["live_motion_authorized"])
            self.assertFalse(evidence["claim_boundary"]["reproduction_complete"])

    def test_verifier_accepts_frozen_artifact_and_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cross_engine_verify_") as tmp:
            root = Path(tmp)
            scenario_path, evidence_path = builder.build(
                mujoco_manifest_path=_model_fixture(root),
                gazebo_contract_path=GAZEBO_CONTRACT,
                config_path=CONFIG,
                output_dir=root / "out",
            )
            self.assertTrue(verifier.verify(evidence_path)["structurally_valid"])

            evidence = builder.load_object(evidence_path)
            evidence["claims"]["cross_engine_contact_equivalence"] = True
            _write_json(evidence_path, evidence)
            report = verifier.verify(evidence_path)
            self.assertFalse(report["structurally_valid"])
            self.assertIn(
                "claim_not_false:cross_engine_contact_equivalence",
                report["issues"],
            )

            builder.build(
                mujoco_manifest_path=root / "model_manifest.json",
                gazebo_contract_path=GAZEBO_CONTRACT,
                config_path=CONFIG,
                output_dir=root / "out",
            )
            scenario = builder.load_object(scenario_path)
            scenario["scenarios"][0]["parameters"]["servo_lag_s"] = 1.0
            _write_json(scenario_path, scenario)
            report = verifier.verify(evidence_path)
            self.assertFalse(report["structurally_valid"])
            self.assertIn("scenario_manifest:hash_mismatch", report["issues"])

    def test_invalid_static_rate_is_reported_not_promoted(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cross_engine_bad_static_") as tmp:
            root = Path(tmp)
            contract = builder.load_object(GAZEBO_CONTRACT)
            contract["rates_hz"]["physics"] = 1000
            contract_path = root / "bad_gazebo_contract.json"
            _write_json(contract_path, contract)
            _, evidence_path = builder.build(
                mujoco_manifest_path=_model_fixture(root),
                gazebo_contract_path=contract_path,
                config_path=CONFIG,
                output_dir=root / "out",
            )
            evidence = builder.load_object(evidence_path)

            self.assertEqual(evidence["status"], "invalid_static_inputs_blocked")
            self.assertFalse(evidence["claims"]["static_rate_contract_match"])
            self.assertIn("static_input:gazebo_rates_invalid", evidence["blockers"])
            self.assertIn("cross_engine_static_physics_rate_mismatch", evidence["blockers"])
            self.assertTrue(verifier.verify(evidence_path)["structurally_valid"])

    def test_builder_has_no_simulator_or_process_execution_surface(self) -> None:
        source = Path(builder.__file__).read_text(encoding="utf-8")

        self.assertNotIn("import subprocess", source)
        self.assertNotIn("import mujoco", source)
        self.assertNotIn("ign gazebo", source)
        self.assertNotIn("os.system", source)


if __name__ == "__main__":
    unittest.main()
