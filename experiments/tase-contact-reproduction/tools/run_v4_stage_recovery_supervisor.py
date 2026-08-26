#!/usr/bin/env python3
"""Durable serial supervisor for recoverable V4 stage failures.

The child runner owns the stage ledger and the live writer.  This supervisor
never opens a robot transport itself: after a child exits with a typed
``recoverable_resume_pending`` receipt, it runs the supplied
``prepare_live --resume-existing`` command, then starts the same child again.
Hard-terminal receipts, missing receipts, preparation failures, and unresolved
in-flight dispatches are terminal and are never retried.
"""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping, Sequence


SUPERVISOR_SCHEMA = "step5d.autotune-v4/v4-stage-recovery-supervisor-v1"


@dataclass(frozen=True)
class RecoverySupervisorConfig:
    runner_command: tuple[str, ...]
    prepare_command: tuple[str, ...]
    run_dir: Path
    status_path: Path
    event_log: Path
    cwd: Path
    max_recoveries: int | None = None

    def __post_init__(self) -> None:
        for name in ("runner_command", "prepare_command"):
            value = getattr(self, name)
            if not isinstance(value, tuple) or not value or any(
                not isinstance(item, str) or not item for item in value
            ):
                raise ValueError(f"{name} must be a non-empty command tuple")
        for name in ("run_dir", "status_path", "event_log", "cwd"):
            if not isinstance(getattr(self, name), Path):
                raise ValueError(f"{name} must be a Path")
        if not self.run_dir.is_dir():
            raise ValueError("recovery supervisor run_dir must exist")
        if not self.cwd.is_dir():
            raise ValueError("recovery supervisor cwd must exist")
        if "--resume-existing" not in self.prepare_command:
            raise ValueError("prepare command must include --resume-existing")
        if self.max_recoveries is not None and (
            isinstance(self.max_recoveries, bool) or self.max_recoveries < 0
        ):
            raise ValueError("max_recoveries must be nonnegative or null")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RecoverySupervisorConfig":
        if value.get("schema") != SUPERVISOR_SCHEMA or value.get("version") != 1:
            raise ValueError("recovery supervisor schema/version differs")
        def command(name: str) -> tuple[str, ...]:
            raw = value.get(name)
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
                raise ValueError(f"{name} must be a command array")
            return tuple(str(item) for item in raw)

        max_recoveries = value.get("max_recoveries")
        if max_recoveries is not None:
            if isinstance(max_recoveries, bool):
                raise ValueError("max_recoveries must be nonnegative or null")
            max_recoveries = int(max_recoveries)
        return cls(
            runner_command=command("runner_command"),
            prepare_command=command("prepare_command"),
            run_dir=Path(str(value["run_dir"])).resolve(),
            status_path=Path(str(value["status_path"])).resolve(),
            event_log=Path(str(value["event_log"])).resolve(),
            cwd=Path(str(value["cwd"])).resolve(),
            max_recoveries=max_recoveries,
        )


def _load_status(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _append_event(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(value), sort_keys=True, allow_nan=False) + "\n")
        stream.flush()


def _recoverable_status(status: Mapping[str, Any] | None) -> bool:
    if not isinstance(status, Mapping) or status.get("state") != "recoverable_resume_pending":
        return False
    recovery = status.get("recovery")
    return isinstance(recovery, Mapping) and recovery.get("disposition") == "recoverable_resume"


def run_supervisor(
    config: RecoverySupervisorConfig,
    *,
    run_command: Callable[[Sequence[str], Path], int] | None = None,
) -> dict[str, Any]:
    """Run serially until completion or a typed non-recoverable stop."""

    def invoke(command: Sequence[str]) -> int:
        if run_command is not None:
            return int(run_command(command, config.cwd))
        return int(subprocess.run(tuple(command), cwd=str(config.cwd), check=False).returncode)

    recoveries = 0
    while True:
        runner_exit = invoke(config.runner_command)
        status = _load_status(config.status_path)
        _append_event(
            config.event_log,
            {
                "event": "runner_exit",
                "exit_code": runner_exit,
                "state": None if status is None else status.get("state"),
                "recovery_count": recoveries,
            },
        )
        if runner_exit == 0:
            return {
                "schema": SUPERVISOR_SCHEMA,
                "status": "complete",
                "recovery_count": recoveries,
                "next_dispatch_permitted": False,
            }
        if not _recoverable_status(status):
            return {
                "schema": SUPERVISOR_SCHEMA,
                "status": "terminal",
                "recovery_count": recoveries,
                "next_dispatch_permitted": False,
                "child_state": None if status is None else status.get("state"),
            }
        if config.max_recoveries is not None and recoveries >= config.max_recoveries:
            return {
                "schema": SUPERVISOR_SCHEMA,
                "status": "recovery_budget_exhausted",
                "recovery_count": recoveries,
                "next_dispatch_permitted": False,
            }
        prepare_exit = invoke(config.prepare_command)
        _append_event(
            config.event_log,
            {
                "event": "fresh_prepare_exit",
                "exit_code": prepare_exit,
                "recovery_count": recoveries + 1,
            },
        )
        if prepare_exit != 0:
            return {
                "schema": SUPERVISOR_SCHEMA,
                "status": "prepare_failed",
                "recovery_count": recoveries,
                "next_dispatch_permitted": False,
            }
        recoveries += 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    raw = json.loads(args.config.read_text(encoding="utf-8"))
    config = RecoverySupervisorConfig.from_mapping(raw)
    result = run_supervisor(config)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
