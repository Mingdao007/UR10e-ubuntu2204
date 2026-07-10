from __future__ import annotations

import csv
from pathlib import Path
import tempfile
import unittest

import numpy as np

from ur10e_vic.backends import VelocityAdmittanceSurrogate
from ur10e_vic.constraints import ImpedanceBounds
from ur10e_vic.policies import DBILPrediction, FixedImpedancePolicy
from ur10e_vic.trace import (
    _rotate_vectors_wxyz,
    _slerp_to_grid,
    convert_ur_bridge_trace,
    load_ur_trace_dataset,
    run_shadow_policy_ablation,
)


class _NominalPredictor:
    def predict(self, observation):
        return DBILPrediction(observation.nominal_zft, 1.0, "d" * 64)


class TraceTests(unittest.TestCase):
    def test_quaternion_slerp_and_wrench_rotation_are_frame_consistent(self) -> None:
        source_time = np.asarray((0.0, 1.0))
        quaternion = np.asarray(((1.0, 0.0, 0.0, 0.0), (-1.0, 0.0, 0.0, 0.0)))
        interpolated = _slerp_to_grid(source_time, quaternion, np.asarray((0.5,)))
        self.assertTrue(np.allclose(interpolated, ((1.0, 0.0, 0.0, 0.0))))

        half = np.sqrt(0.5)
        rotation_z_90 = np.asarray(((half, 0.0, 0.0, half),))
        force_and_moment = np.asarray(((1.0, 0.0, 0.0),))
        rotated_force = _rotate_vectors_wxyz(rotation_z_90, force_and_moment)
        rotated_moment = _rotate_vectors_wxyz(rotation_z_90, force_and_moment)
        self.assertTrue(np.allclose(rotated_force, ((0.0, 1.0, 0.0)), atol=1e-7))
        self.assertTrue(np.array_equal(rotated_force, rotated_moment))

    def _write_trace(self, path: Path) -> None:
        fields = ["t_monotonic_s"]
        fields += [f"ur_actual_TCP_pose_{index}" for index in range(6)]
        fields += [f"ur_actual_TCP_speed_{index}" for index in range(6)]
        fields += [f"ur_actual_q_{index}" for index in range(6)]
        fields += [f"ur_actual_qd_{index}" for index in range(6)]
        fields += [
            "step4e_cmd_vx_m_s",
            "step4e_cmd_vy_m_s",
            "step4e_cmd_vz_m_s",
            "step4e_cmd_wx_rad_s",
            "step4e_cmd_wy_rad_s",
            "step4e_cmd_wz_rad_s",
            "step4e_cmd_valid",
        ]
        fields += [
            "fx_n_zeroed",
            "fy_n_zeroed",
            "fz_n_zeroed",
            "mx_nm_zeroed",
            "my_nm_zeroed",
            "mz_nm_zeroed",
        ]
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row_index in range(100):
                row = {name: 0.0 for name in fields}
                row["t_monotonic_s"] = row_index * 0.002
                row["ur_actual_TCP_pose_0"] = 0.4 + row_index * 1e-5
                row["ur_actual_TCP_pose_3"] = 0.01
                row["fz_n_zeroed"] = -5.0
                row["step4e_cmd_valid"] = 1.0
                for index, name in enumerate(
                    (
                        "step4e_cmd_vx_m_s",
                        "step4e_cmd_vy_m_s",
                        "step4e_cmd_vz_m_s",
                        "step4e_cmd_wx_rad_s",
                        "step4e_cmd_wy_rad_s",
                        "step4e_cmd_wz_rad_s",
                    )
                ):
                    row[name] = index * 1e-4
                writer.writerow(row)

    def test_real_shape_converter_and_four_policy_shadow_are_command_inert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bridge.csv"
            dataset = root / "trace.npz"
            manifest = root / "manifest.json"
            self._write_trace(source)
            result = convert_ur_bridge_trace(source, dataset, manifest)
            self.assertGreater(result["observations"], 1)
            self.assertEqual(result["source_csv_artifact"], "bridge.csv")
            self.assertEqual(result["dataset_artifact"], "trace.npz")
            self.assertNotIn("source_csv", result)
            trace = load_ur_trace_dataset(dataset)
            self.assertIsNone(trace.observation(0).jacobian_base)
            self.assertEqual(trace.actual_command_kind, "step5b_twist")
            self.assertFalse(trace.lineage_verified)
            self.assertFalse(trace.task_zft_verified)
            ablation = run_shadow_policy_ablation(
                trace, _NominalPredictor(), max_observations=10
            )
            self.assertEqual(
                set(ablation["policies"]),
                {"fixed", "scripted", "deterministic", "dbil_shadow"},
            )
            self.assertTrue(ablation["baseline_command_bitwise_unchanged"])
            self.assertIn("no copied after-array", ablation["comparison_method"])
            for summary in ablation["policies"].values():
                self.assertEqual(summary["mux_shadow_bypasses"], 10)
                self.assertTrue(summary["output_matches_shadow_off"])
                self.assertEqual(summary["claim_valid_proposals"], 0)
            self.assertFalse(ablation["claim_evidence_valid"])
            proposal = FixedImpedancePolicy(ImpedanceBounds()).propose(
                trace.observation(0)
            )
            command = VelocityAdmittanceSurrogate().command(
                trace.observation(0), proposal
            )
            self.assertEqual(command.mode, "stop")
            self.assertEqual(
                dict(command.diagnostics)["reason"],
                "shadow_proposal_not_command_capable",
            )

    def test_missing_actual_command_fails_instead_of_substituting_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bridge.csv"
            self._write_trace(source)
            rows = source.read_text(encoding="utf-8").splitlines()
            header = rows[0].split(",")
            command_index = header.index("step4e_cmd_vx_m_s")
            values = rows[1].split(",")
            values[command_index] = ""
            rows[1] = ",".join(values)
            source.write_text("\n".join(rows) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "actual command"):
                convert_ur_bridge_trace(
                    source,
                    root / "trace.npz",
                    root / "manifest.json",
                )


if __name__ == "__main__":
    unittest.main()
