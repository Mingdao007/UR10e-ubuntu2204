import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import export_stage_env  # noqa: E402


P0_STAGE = "step5d_strict_rnn_no_contact_p0_v5"


class ExportStageEnvTest(unittest.TestCase):
    def test_p0_env_comes_from_stage_table_fields(self) -> None:
        env = export_stage_env.build_stage_env(P0_STAGE, ROOT)

        expected = {
            "BRIDGE_PROFILE": P0_STAGE,
            "BRIDGE_DURATION_S": "180",
            "BRIDGE_BASELINE_S": "1",
            "BRIDGE_REZERO_S": "0.25",
            "BRIDGE_RTDE_HZ": "500",
            "BRIDGE_SENSOR_STALE_S": "0.10",
            "BRIDGE_SOCKET_TIMEOUT_S": "0.0",
            "BRIDGE_TARGET_FORCE_N": "1.0",
            "BRIDGE_FORCE_P_GAIN": "0.001",
            "BRIDGE_FORCE_I_GAIN": "0.00001",
            "BRIDGE_FORCE_DAMPING": "7.0",
            "BRIDGE_INTEGRAL_LIMIT_N_S": "1.0",
            "MAX_NORMAL_FORCE_N": "2",
            "MAX_FORCE_NORM_N": "5",
            "MAX_TORQUE_NORM_NM": "3.0",
            "BRIDGE_NORMAL_FOLLOW_MODE": "locked",
            "BRIDGE_NORMAL_FILTER_ALPHA": "0.55",
            "BRIDGE_NORMAL_MIN_FORCE_N": "0.001",
            "BRIDGE_MOTION_LIMIT_M_S": "0.004",
            "BRIDGE_TOTAL_LINEAR_LIMIT_M_S": "0.004",
            "BRIDGE_NORMAL_VELOCITY_LIMIT_M_S": "0.003",
            "BRIDGE_ANGULAR_LIMIT_RAD_S": "0.015",
            "STEP5D_STAGE25_CONTROL_MODE": "speedj_rnn_live",
            "STEP5D_QDOT_LIMIT_RAD_S": "0.150",
            "STEP5D_QDOT_SLEW_RAD_S2": "0.000",
            "STEP5D_PRELOAD_FILTERED_MIN_N": "0.0",
            "STEP5D_PRELOAD_FILTERED_MAX_N": "2.0",
            "STEP5D_PRELOAD_RAW_MIN_N": "0.0",
            "STEP5D_PRELOAD_RAW_MAX_N": "2.0",
            "STEP5D_PRELOAD_FORCE_NORM_MAX_N": "5.0",
            "STEP5D_PRELOAD_HOLD_S": "0.0",
            "STEP5D_PRELOAD_TIMEOUT_S": "1.0",
        }
        self.assertEqual(env, expected)

    def test_cli_outputs_shell_safe_assignments(self) -> None:
        completed = subprocess.run(
            ["python3", str(ROOT / "tools" / "export_stage_env.py"), P0_STAGE],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn(f"BRIDGE_PROFILE={P0_STAGE}", completed.stdout)
        self.assertIn("BRIDGE_FORCE_I_GAIN=0.00001", completed.stdout)
        self.assertNotIn("STEP5D_P0_CONFIRM", completed.stdout)
        self.assertNotIn("BRIDGE_ALLOW_NO_CONTACT_P0_CAPTURE", completed.stdout)

    def test_missing_stage_fails_closed(self) -> None:
        with self.assertRaisesRegex(export_stage_env.StageEnvError, "missing stage table row"):
            export_stage_env.build_stage_env("missing_stage", ROOT)

    def test_missing_mapped_field_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            table_path = tmp_root / "config" / "step5_stage_table.json"
            table = json.loads(table_path.read_text(encoding="utf-8"))
            row = next(row for row in table["stages"] if row.get("id") == P0_STAGE)
            del row["guard"]["path_cap_m_s"]
            table_path.write_text(json.dumps(table), encoding="utf-8")

            with self.assertRaisesRegex(export_stage_env.StageEnvError, "guard.path_cap_m_s"):
                export_stage_env.build_stage_env(P0_STAGE, tmp_root)


if __name__ == "__main__":
    unittest.main()
