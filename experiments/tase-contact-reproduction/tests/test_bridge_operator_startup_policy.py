from pathlib import Path
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

        self.assertIn('BRIDGE_SKIP_BENCH_GATE="${BRIDGE_SKIP_BENCH_GATE:-1}"', script)
        self.assertIn('WAIT_FOR_PLAY_S="${WAIT_FOR_PLAY_S:-10}"', script)
        self.assertIn('AUTOWATCH_WAIT_FOR_PLAY_S="${AUTOWATCH_WAIT_FOR_PLAY_S:-10}"', script)


if __name__ == "__main__":
    unittest.main()
