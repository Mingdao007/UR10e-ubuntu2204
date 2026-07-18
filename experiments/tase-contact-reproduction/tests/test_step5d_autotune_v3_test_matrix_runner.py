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


def test_auto_workers_use_affinity_and_reserve_medium_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner.os, "sched_getaffinity", lambda _pid: {0, 1})
    assert runner.resolve_workers("auto", ["small", "medium"]) == 1
    monkeypatch.setattr(runner.os, "sched_getaffinity", lambda _pid: set(range(16)))
    assert runner.resolve_workers("auto", ["small", "medium"]) == 4


def test_serial_fallback_serializes_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    def fake_run(name: str, _command: list[str], _output: Path) -> dict[str, object]:
        observed.append(name)
        return {"lane": name, "returncode": 0}

    monkeypatch.setattr(runner, "_run_lane", fake_run)
    payload = runner.run(
        ["small", "medium"],
        workers="auto",
        output=tmp_path / "serial",
        serial=True,
    )

    assert observed == ["small", "medium"]
    assert payload["parallel_policy"]["max_concurrent_lanes"] == 1
    assert payload["parallel_policy"]["serial_fallback"] is True


def test_installed_runtime_runs_only_after_passing_hermetic_lanes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    def fake_run(name: str, _command: list[str], _output: Path) -> dict[str, object]:
        observed.append(name)
        return {"lane": name, "returncode": 0}

    monkeypatch.setattr(runner, "_run_lane", fake_run)
    payload = runner.run(
        ["small", "medium"],
        workers=2,
        output=tmp_path / "installed",
        include_installed_runtime=True,
    )

    assert set(observed[:2]) == {"small", "medium"}
    assert observed[-1] == "local_installed_runtime"
    assert payload["parallel_policy"]["installed_runtime_status"] == (
        "executed_serial_after_hermetic"
    )


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
