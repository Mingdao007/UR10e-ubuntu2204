"""Current refusal contract for the retired Step5d entrypoint."""

from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Step5dRetiredEntrypointTest(unittest.TestCase):
    def test_legacy_liveprep_refuses_and_points_to_current_owner(self) -> None:
        operator_path = ROOT / "scripts" / "step5d-liveprep-operator.sh"
        operator = operator_path.read_text(encoding="utf-8")
        base = (ROOT / "scripts" / "step4e-line-v1-operator.sh").read_text(encoding="utf-8")
        bridge_operator = (ROOT / "scripts" / "bridge-line-operator.sh").read_text(encoding="utf-8")
        canonical = ROOT / "scripts" / "step5d-autotune-v3.sh"
        self.assertTrue(canonical.is_file())
        self.assertIn("set -euo pipefail", operator)
        self.assertIn("refusing: duplicate Step5d liveprep entrypoint is retired", operator)
        self.assertIn("use step5d-autotune-v3.sh status --json", operator)
        self.assertIn("or step5d-autotune-v3.sh bridge-live", operator)
        self.assertIn("exit 64", operator)
        self.assertNotIn("current_step5d_version()", operator)
        self.assertNotIn("BRIDGE_OPERATOR=", operator)
        completed = subprocess.run(
            ["bash", str(operator_path)],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 64)
        self.assertEqual(completed.stdout, "")
        self.assertIn("retired", completed.stderr)
        self.assertIn("step5d-autotune-v3.sh status --json", completed.stderr)
        self.assertIn("step5d-autotune-v3.sh bridge-live", completed.stderr)
        self.assertIn('BRIDGE_ANGULAR_LIMIT_RAD_S="${BRIDGE_ANGULAR_LIMIT_RAD_S:-0.150}"', bridge_operator)
        self.assertIn('BRIDGE_ANGULAR_LIMIT_RAD_S="${BRIDGE_ANGULAR_LIMIT_RAD_S:-0.015}"', bridge_operator)
        self.assertIn('STEP4E_ANGULAR_LIMIT_RAD_S="${STEP4E_ANGULAR_LIMIT_RAD_S:-0.150}"', base)
        self.assertIn('STEP4E_ANGULAR_LIMIT_RAD_S="${STEP4E_ANGULAR_LIMIT_RAD_S:-0.015}"', base)
        self.assertIn('--bridge-profile "${BRIDGE_PROFILE}"', bridge_operator)
        self.assertIn('--bridge-mode "${BRIDGE_MODE}"', bridge_operator)
        self.assertIn(
            'Type START_BRIDGE_${CONFIRM_TOKEN}_${BRIDGE_PROFILE_CONFIRM_TOKEN} to continue:',
            bridge_operator,
        )
        self.assertIn('PROGRAM_LINE="/programs/andyl/kunwei/step5/${STEP4E_VERSION}.urp"', base)
        self.assertIn('PROGRAM_LINE="/programs/andyl/kunwei/step5/step5d/${STEP4E_VERSION}.urp"', base)
        self.assertIn('"step5d_strict_rnn_liveprep_v10" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v11"', base)
        self.assertIn('"step5d_strict_rnn_liveprep_v12" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v13" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v14" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v15" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v15a" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v16" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v17" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v18" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v19" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v20"', base)
        self.assertIn("current_step5d_profile()", bridge_operator)
        self.assertIn("refusing Step5d alias: current_stage does not name", bridge_operator)
        self.assertIn("refusing Step5d alias: current_stage does not name", base)
        self.assertNotIn('BRIDGE_PROFILE="${BRIDGE_PROFILE:-step5d_strict_rnn_liveprep_v21}"', bridge_operator)
        self.assertNotIn('STEP4E_VERSION="${STEP4E_VERSION:-step5d_strict_rnn_liveprep_v20}"', base)
        self.assertIn('--step5d-stage25-control-mode "${STEP5D_STAGE25_CONTROL_MODE:-${STEP5D_STAGE25_CONTROL_MODE_DEFAULT}}"', bridge_operator)
        self.assertIn('--step5d-preload-filtered-min-n "${STEP5D_PRELOAD_FILTERED_MIN_N:-${STEP5D_DEFAULT_PRELOAD_FILTERED_MIN_N}}"', bridge_operator)
        self.assertIn("Step5d v26 tube ablation diagnostic", bridge_operator)
        self.assertIn("Step5d v26 tube ablation diagnostic", base)
        self.assertIn("7-18N filtered preload with 5-20N raw sanity", bridge_operator)
        self.assertIn("default speedl_cartesian_oracle", bridge_operator)
        self.assertIn("step5d_live_ready", bridge_operator)
        bridge_source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(encoding="utf-8")
        self.assertIn('v18_v20_locked_normal_settle', bridge_source)
        self.assertIn('v20_low_load_active_reacquire', bridge_source)
        self.assertIn('step5d_contact_safety["action"] == "active_reacquire_solver"', bridge_source)
        self.assertNotIn('if [[ "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v9" ]]; then\n  PROGRAM_LINE="/programs/andyl/kunwei/step5/${STEP4E_VERSION}.urp"', base)
        self.assertIn('EXPECTED_BASENAME="${PROGRAM_LINE##*/}"', base)
        self.assertIn('RUN_LABEL="${EXPECTED_BASENAME%.urp}"', base)
