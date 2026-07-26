#!/usr/bin/env python3
"""Run V3 test lanes with bounded parallelism and a true serial fallback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "config/step5d_autotune_v3_test_matrix.json"
SCHEMA = "step5d.autotune-v3/parallel-test-run/v3"
INSTALLED_RUNTIME_PRECONDITION_SCHEMA = (
    "step5d.autotune-v3/installed-runtime-precondition-v1"
)
PRECONDITION_DETAIL_MAX_CHARS = 512
HERMETIC_PYTHON_TOKEN = "@hermetic-python"
ALLOWED_LANES = {"small", "medium"}
MAX_SMALL_WORKERS = 4
FAILURE_TAIL_BYTES = 64 * 1024
FAILURE_TAIL_LINES = 200

_ACTIVE_HERMETIC_PYTHON: Path | None = None
_ACTIVE_HERMETIC_BINDING: dict[str, str] | None = None


class TestMatrixError(RuntimeError):
    pass


def _repository_binding() -> dict[str, Any]:
    repository = ROOT.parents[1]

    def git(*arguments: str) -> bytes:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise TestMatrixError(
                f"repository binding command failed: git {' '.join(arguments)}"
            )
        return completed.stdout

    head = git("rev-parse", "HEAD").decode("ascii").strip()
    status = git(
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    return {
        "root": str(repository.resolve(strict=True)),
        "head": head,
        "clean": not status,
        "status_sha256": hashlib.sha256(status).hexdigest(),
        "matrix_sha256": _sha256(MATRIX),
    }


def _runtime_binding_evidence(fields: Sequence[str]) -> dict[str, str]:
    names = (
        "control_python",
        "optimizer_python",
        "runtime_bundle_id",
        "runtime_attestation_sha256",
        "runtime_contract_sha256",
        "uv_lock_sha256",
        "control_environment_id",
        "optimizer_environment_id",
        "gpu_uuid",
        "ld_library_path",
        "cupy_cache_dir",
    )
    if len(fields) != len(names):
        raise TestMatrixError("installed runtime binding fields differ")
    return dict(zip(names, fields, strict=True))


def _installed_runtime_precondition() -> dict[str, Any]:
    """Validate deployed-current before spending time on its installed gate."""
    try:
        from step5d_autotune_v3.release_identity import load_current_release

        release = load_current_release(ROOT)
    except Exception as exc:
        detail = " ".join(f"{type(exc).__name__}: {exc}".split())
        return {
            "schema": INSTALLED_RUNTIME_PRECONDITION_SCHEMA,
            "ok": False,
            "release_mode": "deployed-current",
            "reason_code": "CURRENT_RELEASE_INVALID",
            "detail": detail[:PRECONDITION_DETAIL_MAX_CHARS],
            "program_id": "",
            "manifest_path": "",
            "manifest_sha256": "",
        }
    return {
        "schema": INSTALLED_RUNTIME_PRECONDITION_SCHEMA,
        "ok": True,
        "release_mode": "deployed-current",
        "reason_code": "CURRENT_RELEASE_VALID",
        "detail": "",
        "program_id": release.program_id,
        "manifest_path": release.manifest_path,
        "manifest_sha256": release.manifest_sha256,
    }


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


def _resolve_hermetic_python(value: str | Path | None = None) -> Path:
    candidate = Path(value) if value is not None else Path(sys.executable)
    try:
        executable = Path(os.path.abspath(candidate.expanduser()))
    except (OSError, RuntimeError) as exc:
        raise TestMatrixError(
            f"hermetic Python executable is unavailable: {candidate}"
        ) from exc
    if not executable.is_file():
        raise TestMatrixError("hermetic Python executable is not a regular file")
    return executable


def _hermetic_binding(executable: Path) -> dict[str, str]:
    try:
        completed = subprocess.run(
            [
                str(executable),
                "-I",
                "-c",
                (
                    "import json,sys,sysconfig; print(json.dumps({"
                    "'executable': sys.executable,"
                    "'python_version': sys.version.split()[0],"
                    "'purelib': sysconfig.get_paths()['purelib']"
                    "}, sort_keys=True))"
                ),
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        payload = json.loads(completed.stdout.strip())
        purelib = Path(str(payload["purelib"])).resolve(strict=True)
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError) as exc:
        raise TestMatrixError("hermetic Python metadata is unavailable") from exc
    if completed.returncode != 0:
        raise TestMatrixError("hermetic Python metadata probe failed")
    return {
        "executable": str(executable),
        "executable_sha256": _sha256(executable),
        "python_version": str(payload["python_version"]),
        "purelib": str(purelib),
    }


def load_commands(
    path: Path,
    lanes: Sequence[str],
    workers: int,
    hermetic_python: str | Path | None = None,
) -> dict[str, list[str]]:
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
        if command[:4] != [HERMETIC_PYTHON_TOKEN, "-m", "pytest", "-q"]:
            raise TestMatrixError(f"{lane_name} command is not governed pytest")
        command[0] = str(_resolve_hermetic_python(hermetic_python))
        if lane_name == "small" and workers > 1:
            command[4:4] = ["-p", "xdist.plugin", "-n", str(workers), "--dist", "loadgroup"]
        result[lane_name] = command
    return result


def _runtime_binding() -> list[str]:
    resolver = ROOT / "tools/resolve_step5d_autotune_v3_runtime.py"
    completed = subprocess.run(
        ["/usr/bin/python3.10", "-B", "-I", str(resolver), "--shell-binding"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20.0,
        check=False,
    )
    fields = completed.stdout.strip().split("\t")
    if completed.returncode != 0 or len(fields) != 11 or not all(fields):
        raise TestMatrixError(
            "governed control runtime is unavailable for installed-runtime gate"
        )
    return fields


def load_installed_runtime_command(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    gate = payload.get("local_installed_runtime_gate")
    command = gate.get("command") if isinstance(gate, dict) else None
    if (
        not isinstance(command, list)
        or command[:4] != ["@control-runtime-python", "-m", "pytest", "-q"]
        or command[4:] != [
            "tests/test_step5d_autotune_v3_contract.py",
            "tests/test_step5d_autotune_runtime.py",
            "tests/test_step5d_autotune_live_driver.py",
            "tests/test_step5d_autotune_v3_bridge_wrapper.py",
            "tests/test_step5d_release_contract.py",
            "tests/test_step5d_autotune_v3_installed_runtime.py",
            "tests/test_step5d_no_contact_p0.py",
        ]
        or gate.get("ci") is not False
        or gate.get("serial") is not True
    ):
        raise TestMatrixError("local installed-runtime gate differs")
    resolved = list(command)
    resolved[0] = _runtime_binding()[0]
    return resolved


def _pytest_overlay(output: Path) -> Path:
    overlay = output / "control-pytest-overlay"
    overlay.mkdir(parents=True, exist_ok=True, mode=0o700)
    hermetic_python = _resolve_hermetic_python(
        _ACTIVE_HERMETIC_PYTHON or sys.executable
    )
    completed = subprocess.run(
        [
            str(hermetic_python),
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )
    if completed.returncode != 0:
        raise TestMatrixError("frozen hermetic pytest environment is unavailable")
    hermetic_site = Path(completed.stdout.strip()).resolve(strict=True)
    prefixes = (
        "_pytest",
        "pytest",
        "pluggy",
        "iniconfig",
        "packaging",
        "pygments",
        "tomli",
        "typing_extensions",
        "exceptiongroup",
    )
    for source in hermetic_site.iterdir():
        normalized = source.name.lower().replace("-", "_")
        if source.name != "py.py" and not any(
            normalized == prefix or normalized.startswith(prefix + "_")
            for prefix in prefixes
        ):
            continue
        destination = overlay / source.name
        if destination.exists() or destination.is_symlink():
            continue
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=False)
        else:
            shutil.copy2(source, destination, follow_symlinks=True)
    if not (overlay / "pytest").is_dir() or not (overlay / "_pytest").is_dir():
        raise TestMatrixError("frozen hermetic pytest packages are incomplete")
    return overlay


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
    if name == "local_installed_runtime":
        binding = _runtime_binding()
        ros_paths = [
            path
            for path in (
                Path("/opt/ros/humble/lib/python3.10/site-packages"),
                Path("/opt/ros/humble/local/lib/python3.10/dist-packages"),
            )
            if path.is_dir()
        ]
        environment["PYTHONPATH"] = os.pathsep.join(
            [
                str(_pytest_overlay(output)),
                str(ROOT / "tools"),
                str(runtime_source),
                *(str(path) for path in ros_paths),
            ]
        )
        environment["PYTHONNOUSERSITE"] = "1"
        environment["CUDA_VISIBLE_DEVICES"] = binding[8]
        environment["LD_LIBRARY_PATH"] = binding[9]
        environment["CUPY_CACHE_DIR"] = binding[10]
        environment["STEP5D_V3_CONTROL_PYTHON"] = binding[0]
        environment["STEP5D_V3_OPTIMIZER_PYTHON"] = binding[1]
        environment["STEP5D_V3_RUNTIME_BUNDLE_ID"] = binding[2]
        environment["STEP5D_V3_RUNTIME_ATTESTATION_SHA256"] = binding[3]
    else:
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
        "hermetic_python": dict(_ACTIVE_HERMETIC_BINDING or {}),
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
    require_clean: bool = False,
    hermetic_python: str | Path | None = None,
) -> dict[str, Any]:
    global _ACTIVE_HERMETIC_PYTHON, _ACTIVE_HERMETIC_BINDING
    _ACTIVE_HERMETIC_PYTHON = _resolve_hermetic_python(hermetic_python)
    _ACTIVE_HERMETIC_BINDING = _hermetic_binding(_ACTIVE_HERMETIC_PYTHON)
    repository_before = _repository_binding()
    resolved_workers = 1 if serial else resolve_workers(workers, lanes)
    commands = load_commands(
        MATRIX, lanes, resolved_workers, _ACTIVE_HERMETIC_PYTHON
    )
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
    installed_runtime_binding = None
    installed_runtime_precondition = None
    if include_installed_runtime:
        if all(item["returncode"] == 0 for item in results):
            installed_runtime_precondition = _installed_runtime_precondition()
            if installed_runtime_precondition["ok"]:
                installed_runtime_binding = _runtime_binding_evidence(
                    _runtime_binding()
                )
                results.append(
                    _run_lane(
                        "local_installed_runtime",
                        load_installed_runtime_command(MATRIX),
                        output,
                    )
                )
                installed_runtime_status = "executed_serial_after_hermetic"
            else:
                installed_runtime_status = (
                    "blocked_by_current_release_precondition"
                )
        else:
            installed_runtime_status = "blocked_by_hermetic_failure"
    repository_after = _repository_binding()
    repository_stable = repository_after == repository_before
    clean_requirement_satisfied = bool(
        not require_clean
        or (repository_before["clean"] and repository_after["clean"])
    )
    installed_runtime_requirement_satisfied = bool(
        not include_installed_runtime
        or installed_runtime_status == "executed_serial_after_hermetic"
    )
    payload = {
        "schema": SCHEMA,
        "ok": bool(
            all(item["returncode"] == 0 for item in results)
            and repository_stable
            and clean_requirement_satisfied
            and installed_runtime_requirement_satisfied
        ),
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
            "hermetic_python": dict(_ACTIVE_HERMETIC_BINDING),
        },
        "repository_binding": {
            "before": repository_before,
            "after": repository_after,
            "stable": repository_stable,
            "require_clean": require_clean,
            "clean_requirement_satisfied": clean_requirement_satisfied,
        },
        "installed_runtime_binding": installed_runtime_binding,
        "installed_runtime_precondition": installed_runtime_precondition,
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
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument(
        "--hermetic-python",
        type=Path,
        help="Hermetic lane interpreter; defaults to the interpreter launching this runner",
    )
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
            require_clean=args.require_clean,
            hermetic_python=args.hermetic_python,
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
