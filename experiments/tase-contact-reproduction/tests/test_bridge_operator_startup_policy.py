from pathlib import Path
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def read_script(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


class BridgeOperatorStartupPolicyTest(unittest.TestCase):
    def test_bridge_line_operator_exposes_explicit_long_gate_skip_knob(self) -> None:
        script = read_script("bridge-line-operator.sh")

        self.assertIn("BRIDGE_SKIP_BENCH_GATE", script)
        self.assertIn("BRIDGE_SKIP_LONG_CHECKS", script)
        self.assertIn("skipping long bench gate by request", script)

    def test_step5d_contact_bridge_defaults_to_short_start_path(self) -> None:
        script = read_script("step5d-liveprep-operator.sh")

        self.assertIn('"${BRIDGE_OPERATOR}" line-bridge-fast', script)
        self.assertNotIn('BRIDGE_SKIP_BENCH_GATE="${BRIDGE_SKIP_BENCH_GATE:-1}"', script)
        self.assertIn('WAIT_FOR_PLAY_S="${WAIT_FOR_PLAY_S:-10}"', script)
        self.assertIn('AUTOWATCH_WAIT_FOR_PLAY_S="${AUTOWATCH_WAIT_FOR_PLAY_S:-10}"', script)
        self.assertIn('READBACK_GATE="${ROOT}/tools/verify_current_stage_readback.py"', script)

    def test_fast_bridge_requires_fresh_cache_even_when_skip_env_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env.update(
                {
                    "BRIDGE_PROFILE": "step5d_strict_rnn_liveprep_v19",
                    "LONG_CHECK_CACHE": str(Path(tmp) / "missing-cache.json"),
                    "BRIDGE_SKIP_BENCH_GATE": "1",
                    "BRIDGE_SKIP_LONG_CHECKS": "1",
                }
            )
            completed = subprocess.run(
                [str(ROOT / "scripts" / "bridge-line-operator.sh"), "line-bridge-fast"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 24, completed.stdout + completed.stderr)
        self.assertIn("refusing fast bridge", completed.stdout)
        self.assertNotIn("skipping long bench gate cache requirement", completed.stdout)


if __name__ == "__main__":
    unittest.main()
