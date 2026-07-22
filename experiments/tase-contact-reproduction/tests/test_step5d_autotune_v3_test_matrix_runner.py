from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import run_step5d_autotune_v3_test_matrix as runner  # noqa: E402
import validate_step5d_autotune_v3_refactor as validator  # noqa: E402


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
    monkeypatch.setattr(
        runner,
        "load_installed_runtime_command",
        lambda _path: ["/governed/control/bin/python", "-m", "pytest", "-q"],
    )
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


def test_installed_runtime_lane_uses_governed_cuda_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = ["/control/python", "/optimizer/python", *(["a" * 64] * 6)]
    binding.extend(["GPU-fixture", "/runtime/nvidia", "/runtime/cupy-cache"])
    observed: dict[str, str] = {}

    def fake_run(*_args, **kwargs):
        observed.update(kwargs["env"])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner, "_runtime_binding", lambda: binding)
    monkeypatch.setattr(runner, "_pytest_overlay", lambda output: output)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner._run_lane(
        "local_installed_runtime",
        ["/control/python", "-m", "pytest", "-q"],
        tmp_path,
    )

    assert result["returncode"] == 0
    assert observed["CUDA_VISIBLE_DEVICES"] == "GPU-fixture"
    assert observed["LD_LIBRARY_PATH"] == "/runtime/nvidia"
    assert observed["CUPY_CACHE_DIR"] == "/runtime/cupy-cache"


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


def test_authoritative_gate_runs_every_active_file_and_no_obsolete_file() -> None:
    payload = json.loads(runner.MATRIX.read_text(encoding="utf-8"))
    gate = payload["authoritative_bridge_gate"]
    classifications = gate["classified_test_files"]
    commanded = {
        token
        for lane in payload["lanes"].values()
        for command in lane["commands"]
        for token in command
        if token.startswith("tests/")
    }
    commanded.update(
        token
        for token in payload["local_installed_runtime_gate"]["command"]
        if token.startswith("tests/")
    )
    classified = set().union(*map(set, classifications.values()))

    assert gate["current_tp_program_id"] == "step5d_strict_rnn_autotune_v3_r010"
    assert gate["unclassified_failure_policy"] == "block"
    assert set(classifications["active"]).issubset(commanded)
    assert set(classifications["obsolete"]).isdisjoint(commanded)
    assert set(classifications["active"]).isdisjoint(classifications["obsolete"])
    assert validator.step5d_v3_test_paths(ROOT) <= classified


def test_authoritative_gate_binds_the_production_vertical_slice() -> None:
    payload = json.loads(runner.MATRIX.read_text(encoding="utf-8"))
    gate = payload["authoritative_bridge_gate"]

    assert gate["canonical_launcher"] == "scripts/step5d-autotune-v3.sh bridge"
    assert gate["status_reanchor"] == "scripts/step5d-autotune-v3.sh status --json"
    assert gate["acceptance_path"][1] == "release_manifest_v3_verified"
    assert gate["acceptance_path"][-5:] == [
        "one_trial_completed",
        "command_bound_next_arm_grant",
        "next_arm_acknowledged",
        "bridge_process_still_alive_at_campaign_outcome",
        "campaign_terminal_attested_before_bridge_cleanup",
    ]

    active = set(gate["classified_test_files"]["active"])
    assert {
        "tests/test_step5d_autotune_v3_qualification_production.py",
        "tests/test_step5d_autotune_v3_tp_delivery_transaction.py",
        "tests/test_step5d_runtime_environment.py",
        "tests/test_step5d_autotune_live_driver.py",
        "tests/test_step5d_autotune_v3_bridge_wrapper.py",
        "tests/test_step5d_autotune_v3_trial_overlay_mailbox.py",
    } <= active
