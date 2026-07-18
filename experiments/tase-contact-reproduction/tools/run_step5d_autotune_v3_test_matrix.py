#!/usr/bin/env python3
"""Run the hermetic V3 test lanes with bounded, explicit parallelism."""

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


class TestMatrixError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_commands(path: Path, lanes: Sequence[str], workers: int) -> dict[str, list[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "step5d.autotune-v3/test-matrix-v2":
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
        if command[:4] != ["python3", "-m", "pytest", "-q"]:
            raise TestMatrixError(f"{lane_name} command is not governed pytest")
        if lane_name == "small" and workers > 1:
            command[4:4] = ["-p", "xdist.plugin", "-n", str(workers), "--dist", "loadgroup"]
        result[lane_name] = command
    return result


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
        "dependency_mode": (
            "hosted_ci_parser_stubs"
            if environment.get("STEP5D_V3_HERMETIC_PARSER_CI") == "1"
            else "installed_runtime_dependencies"
        ),
        "log": log.relative_to(ROOT).as_posix() if log.is_relative_to(ROOT) else str(log),
        "log_sha256": _sha256(log),
    }


def run(lanes: Sequence[str], *, workers: int, output: Path) -> dict[str, Any]:
    commands = load_commands(MATRIX, lanes, workers)
    output = output.absolute()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    started = time.monotonic()
    max_concurrent_lanes = min(len(commands), 2)
    with ThreadPoolExecutor(max_workers=max_concurrent_lanes) as executor:
        futures = {
            name: executor.submit(_run_lane, name, command, output)
            for name, command in commands.items()
        }
        results = [futures[name].result() for name in commands]
    payload = {
        "schema": SCHEMA,
        "ok": all(item["returncode"] == 0 for item in results),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": time.monotonic() - started,
        "parallel_policy": {
            "max_concurrent_lanes": max_concurrent_lanes,
            "small_xdist_workers": workers,
            "medium_internal_parallelism": 1,
            "native_thread_caps": 1,
            "large_and_hil_forbidden": True,
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
        type=int,
        default=min(4, max(1, (os.cpu_count() or 2) - 1)),
    )
    parser.add_argument("--serial", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = run(
            args.lanes,
            workers=1 if args.serial else args.workers,
            output=args.output,
        )
    except Exception as exc:
        print(json.dumps({"schema": SCHEMA, "ok": False, "blocker": str(exc)}))
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
