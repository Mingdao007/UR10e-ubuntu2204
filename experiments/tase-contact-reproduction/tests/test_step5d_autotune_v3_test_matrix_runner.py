from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import run_step5d_autotune_v3_test_matrix as runner  # noqa: E402


def test_small_uses_bounded_xdist_while_medium_remains_serial() -> None:
    commands = runner.load_commands(runner.MATRIX, ["small", "medium"], 4)
    assert commands["small"][4:10] == [
        "-p", "xdist.plugin", "-n", "4", "--dist", "loadgroup"
    ]
    assert "-n" not in commands["medium"]


def test_serial_fallback_has_no_xdist() -> None:
    commands = runner.load_commands(runner.MATRIX, ["small", "medium"], 1)
    assert all("-n" not in command for command in commands.values())


@pytest.mark.parametrize("lanes", [["large_ursim"], ["hil_no_motion"], []])
def test_runner_refuses_nonhermetic_or_empty_selection(lanes: list[str]) -> None:
    with pytest.raises(runner.TestMatrixError):
        runner.load_commands(runner.MATRIX, lanes, 2)


def test_failed_lane_emits_bounded_log_tail_without_polluting_manifest(
    tmp_path: Path,
) -> None:
    failed = tmp_path / "failed.log"
    failed.write_text(
        "\n".join(f"diagnostic-{index:03d}" for index in range(250)) + "\n",
        encoding="utf-8",
    )
    passed = tmp_path / "passed.log"
    passed.write_text("successful-secret\n", encoding="utf-8")
    payload = {
        "results": [
            {"lane": "small", "returncode": 1, "log": str(failed)},
            {"lane": "medium", "returncode": 0, "log": str(passed)},
        ]
    }
    stream = io.StringIO()

    runner.emit_failure_logs(payload, stream=stream)

    output = stream.getvalue()
    assert "BEGIN FAILED LANE small LOG TAIL" in output
    assert "diagnostic-249" in output
    assert "diagnostic-049" not in output
    assert "successful-secret" not in output
