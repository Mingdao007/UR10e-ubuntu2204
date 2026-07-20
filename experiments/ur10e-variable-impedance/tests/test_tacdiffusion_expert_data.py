import json
from pathlib import Path
import tempfile
import unittest

from tacdiffusion_expert_fixtures import write_expert_trace_manifest
from ur10e_vic.tacdiffusion.expert_data import (
    ExpertForceDefinitionArtifact,
    ExpertTraceManifestV2,
    deterministic_expert_f_ff,
    validate_episode_manifest_artifacts,
)


class ExpertDataContractTests(unittest.TestCase):
    def test_complete_episode_graph_is_rehashed_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_expert_trace_manifest(Path(directory), "episode-1", "train")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.pop("fingerprint_sha256")
            manifest = ExpertTraceManifestV2(**payload)
            self.assertFalse(manifest.training_eligible)
            artifacts = validate_episode_manifest_artifacts(manifest, path)
            self.assertTrue(artifacts["controller_decision"].controller_verified)
            self.assertEqual(artifacts["task_zft"].episode_id, "episode-1")

    def test_tampered_or_unverified_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_expert_trace_manifest(root, "episode-1", "train")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.pop("fingerprint_sha256")
            manifest = ExpertTraceManifestV2(**payload)
            calibration = root / manifest.artifact_bindings["sensor_calibration"]["path"]
            calibration.write_text(calibration.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "artifact hash mismatch"):
                validate_episode_manifest_artifacts(manifest, path)

            invalid_path = write_expert_trace_manifest(
                root,
                "episode-2",
                "validation",
                controller_contract_valid=False,
            )
            invalid_payload = json.loads(invalid_path.read_text(encoding="utf-8"))
            invalid_payload.pop("fingerprint_sha256")
            invalid = ExpertTraceManifestV2(**invalid_payload)
            with self.assertRaisesRegex(ValueError, "do not verify the controller"):
                validate_episode_manifest_artifacts(invalid, invalid_path)

    def test_deterministic_expert_force_semantics_and_clamp(self):
        definition = ExpertForceDefinitionArtifact(
            schema="ur10e_expert_force_definition/v1",
            definition_id="task_zft_minus_impedance_term_v1",
            stiffness=(10.0,) * 6,
            damping=(2.0,) * 6,
            component_abs_max=(3.0,) * 6,
            source_code_sha256="a" * 64,
            canonical_frame_id="tool0_tcp",
        )
        output = deterministic_expert_f_ff(
            (1.0,) * 6,
            (0.5,) * 6,
            (0.25,) * 6,
            definition,
        )
        self.assertEqual(output, (-3.0,) * 6)
        self.assertEqual(
            output,
            deterministic_expert_f_ff((1.0,) * 6, (0.5,) * 6, (0.25,) * 6, definition),
        )


if __name__ == "__main__":
    unittest.main()
