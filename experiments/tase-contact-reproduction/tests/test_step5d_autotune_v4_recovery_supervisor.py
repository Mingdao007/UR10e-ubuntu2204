from __future__ import annotations

import json
from pathlib import Path

from run_v4_stage_recovery_supervisor import (
    RecoverySupervisorConfig,
    SUPERVISOR_SCHEMA,
    run_supervisor,
)


def _config(tmp_path: Path, *, max_recoveries: int | None = None) -> RecoverySupervisorConfig:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return RecoverySupervisorConfig(
        runner_command=("runner", "--run"),
        prepare_command=("prepare", "--resume-existing"),
        run_dir=run_dir,
        status_path=run_dir / "status.json",
        event_log=run_dir / "supervisor.jsonl",
        cwd=tmp_path,
        max_recoveries=max_recoveries,
    )


def test_supervisor_prepares_fresh_epoch_then_restarts_same_runner(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls: list[tuple[str, ...]] = []
    runner_states = iter(
        [
            (1, {"state": "recoverable_resume_pending", "recovery": {"disposition": "recoverable_resume"}}),
            (0, {"state": "search_complete"}),
        ]
    )

    def invoke(command: tuple[str, ...], _cwd: Path) -> int:
        calls.append(command)
        if command == config.prepare_command:
            return 0
        exit_code, status = next(runner_states)
        config.status_path.write_text(json.dumps(status), encoding="utf-8")
        return exit_code

    result = run_supervisor(config, run_command=invoke)
    assert result["status"] == "complete"
    assert result["recovery_count"] == 1
    assert calls == [config.runner_command, config.prepare_command, config.runner_command]
    events = [json.loads(line)["event"] for line in config.event_log.read_text().splitlines()]
    assert events == ["runner_exit", "fresh_prepare_exit", "runner_exit"]


def test_supervisor_never_prepares_hard_terminal_status(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls: list[tuple[str, ...]] = []

    def invoke(command: tuple[str, ...], _cwd: Path) -> int:
        calls.append(command)
        config.status_path.write_text(
            json.dumps({"state": "terminal_failure", "recovery": {"disposition": "hard_terminal"}}),
            encoding="utf-8",
        )
        return 1

    result = run_supervisor(config, run_command=invoke)
    assert result["status"] == "terminal"
    assert calls == [config.runner_command]


def test_supervisor_stops_when_fresh_prepare_fails(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls: list[tuple[str, ...]] = []
    step = 0

    def invoke(command: tuple[str, ...], _cwd: Path) -> int:
        nonlocal step
        calls.append(command)
        step += 1
        if step == 1:
            config.status_path.write_text(
                json.dumps({"state": "recoverable_resume_pending", "recovery": {"disposition": "recoverable_resume"}}),
                encoding="utf-8",
            )
            return 1
        return 1

    result = run_supervisor(config, run_command=invoke)
    assert result["status"] == "prepare_failed"
    assert calls == [config.runner_command, config.prepare_command]
