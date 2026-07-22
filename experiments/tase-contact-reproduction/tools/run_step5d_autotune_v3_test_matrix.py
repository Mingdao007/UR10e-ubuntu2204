#!/usr/bin/env python3
"""Run V3 test lanes with bounded parallelism and a true serial fallback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "config/step5d_autotune_v3_test_matrix.json"
SCHEMA = "step5d.autotune-v3/parallel-test-run/v1"
ALLOWED_LANES = {"small", "medium"}
MAX_SMALL_WORKERS = 4
FAILURE_TAIL_BYTES = 64 * 1024
FAILURE_TAIL_LINES = 200


class TestMatrixError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def emit_failure_logs(payload: Mapping[str, Any], *, stream: Any = sys.stderr) -> None:
    """Expose bounded pytest diagnostics without copying them into evidence JSON."""
    for item in payload.get("results", []):
        if item.get("returncode") == 0:
            continue
        lane = str(item.get("lane", "unknown"))
        log = Path(str(item.get("log", "")))
        if not log.is_absolute():
            log = ROOT / log
        try:
            with log.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - FAILURE_TAIL_BYTES))
                lines = handle.read().decode("utf-8", errors="replace").splitlines()
            excerpt = "\n".join(lines[-FAILURE_TAIL_LINES:])
        except OSError as exc:
            excerpt = f"unable to read failed lane log: {exc}"
        print(f"--- BEGIN FAILED LANE {lane} LOG TAIL ---", file=stream)
        print(excerpt, file=stream)
        print(f"--- END FAILED LANE {lane} LOG TAIL ---", file=stream)


def _available_cpu_count() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def resolve_workers(value: int | str, lanes: Sequence[str]) -> int:
    if value == "auto":
        reserve = 1 if {"small", "medium"}.issubset(set(lanes)) else 0
        return min(MAX_SMALL_WORKERS, max(1, _available_cpu_count() - reserve))
    try:
        workers = int(value)
    except (TypeError, ValueError) as exc:
        raise TestMatrixError("workers must be 'auto' or an integer") from exc
    if workers < 1 or workers > 8:
        raise TestMatrixError("worker count must be within [1, 8]")
    return workers


def load_commands(path: Path, lanes: Sequence[str], workers: int) -> dict[str, list[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "step5d.autotune-v3/test-matrix-v3":
        raise TestMatrixError("test matrix schema differs")
    requested = list(dict.fromkeys(lanes))
    if not requested or not set(requested).issubset(ALLOWED_LANES):
        raise TestMatrixError("only nonempty Small/Medium lane selections are allowed")
    if workers < 1 or workers > 8:
        raise TestMatrixError("worker count must be within [1, 8]")
    result: dict[str, list[str]] = {}
    for lane_name in requested:
        lane = payload["lanes"][lane_name]
        commands = lane.get("commands")
        if not isinstance(commands, list) or len(commands) != 1:
            raise TestMatrixError(f"{lane_name} must have exactly one governed command")
        command = list(commands[0])
        if command[:4] != [".venv/bin/python", "-m", "pytest", "-q"]:
            raise TestMatrixError(f"{lane_name} command is not governed pytest")
        if lane_name == "small" and workers > 1:
            command[4:4] = ["-p", "xdist.plugin", "-n", str(workers), "--dist", "loadgroup"]
        result[lane_name] = command
    return result


def load_installed_runtime_command(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    gate = payload.get("local_installed_runtime_gate")
    command = gate.get("command") if isinstance(gate, dict) else None
    if (
        not isinstance(command, list)
        or command[:4] != [".venv/bin/python", "-m", "pytest", "-q"]
        or command[4:] != [
            "tests/test_step5d_autotune_v3_contract.py",
            "tests/test_step5d_autotune_runtime.py",
            "tests/test_step5d_autotune_live_driver.py",
            "tests/test_step5d_autotune_v3_bridge_wrapper.py",
            "tests/test_step5d_autotune_v3_qualification_production.py",
            "tests/test_step5d_autotune_v3_installed_runtime.py",
        ]
        or gate.get("ci") is not False
        or gate.get("serial") is not True
    ):
        raise TestMatrixError("local installed-runtime gate differs")
    return list(command)


def _run_lane(name: str, command: Sequence[str], output: Path) -> dict[str, Any]:
    log = output / f"{name}.log"
    started = datetime.now(timezone.utc).isoformat()
    monotonic = time.monotonic()
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
    }
    runtime_source = ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(runtime_source), environment.get("PYTHONPATH", ""))
    ).rstrip(os.pathsep)
    with log.open("wb") as handle:
        completed = subprocess.run(
            list(command),
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
        )
    return {
        "lane": name,
        "command": list(command),
        "started_at": started,
        "elapsed_s": time.monotonic() - monotonic,
        "returncode": completed.returncode,
        "dependency_mode": "frozen_uv_environment",
        "log": log.relative_to(ROOT).as_posix() if log.is_relative_to(ROOT) else str(log),
        "log_sha256": _sha256(log),
    }


def run(
    lanes: Sequence[str],
    *,
    workers: int | str,
    output: Path,
    serial: bool = False,
    include_installed_runtime: bool = False,
) -> dict[str, Any]:
    resolved_workers = 1 if serial else resolve_workers(workers, lanes)
    commands = load_commands(MATRIX, lanes, resolved_workers)
    output = output.absolute()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    started = time.monotonic()
    max_concurrent_lanes = 1 if serial else min(len(commands), 2)
    if serial:
        results = [
            _run_lane(name, command, output) for name, command in commands.items()
        ]
    else:
        with ThreadPoolExecutor(max_workers=max_concurrent_lanes) as executor:
            futures = {
                name: executor.submit(_run_lane, name, command, output)
                for name, command in commands.items()
            }
            results = [futures[name].result() for name in commands]
    installed_runtime_status = "not_requested"
    if include_installed_runtime:
        if all(item["returncode"] == 0 for item in results):
            results.append(
                _run_lane(
                    "local_installed_runtime",
                    load_installed_runtime_command(MATRIX),
                    output,
                )
            )
            installed_runtime_status = "executed_serial_after_hermetic"
        else:
            installed_runtime_status = "blocked_by_hermetic_failure"
    payload = {
        "schema": SCHEMA,
        "ok": all(item["returncode"] == 0 for item in results),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": time.monotonic() - started,
        "parallel_policy": {
            "max_concurrent_lanes": max_concurrent_lanes,
            "small_xdist_workers": resolved_workers,
            "medium_internal_parallelism": 1,
            "native_thread_caps": 1,
            "nonhermetic_lanes_absent": True,
            "serial_fallback": serial,
            "installed_runtime_status": installed_runtime_status,
        },
        "results": results,
    }
    manifest = output / "parallel_run_manifest.json"
    manifest.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lanes", nargs="+", default=["small", "medium"])
    parser.add_argument(
        "--workers",
        default="auto",
        help="Small-lane xdist workers: 'auto' uses CPU affinity and reserves Medium capacity",
    )
    parser.add_argument("--serial", action="store_true")
    parser.add_argument("--include-installed-runtime", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = run(
            args.lanes,
            workers=args.workers,
            output=args.output,
            serial=args.serial,
            include_installed_runtime=args.include_installed_runtime,
        )
    except Exception as exc:
        print(json.dumps({"schema": SCHEMA, "ok": False, "blocker": str(exc)}))
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    if not payload["ok"]:
        emit_failure_logs(payload)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
