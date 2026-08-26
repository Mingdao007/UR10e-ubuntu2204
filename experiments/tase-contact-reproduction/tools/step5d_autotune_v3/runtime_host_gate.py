"""Shared ROS-host import and no-I/O startup gate for Step5d V3.

This module is intentionally usable from the bootstrap interpreter.  It never
opens a sensor/controller socket; it selects the immutable runtime pointer,
constructs the exact managed environment, and performs the import/prewarm in a
managed control child before a public bridge entrypoint is allowed to proceed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .runtime_environment import RUNTIME_SOURCE, production_runtime_environment
from .runtime_installation import (
    RuntimeInstallationError,
    load_runtime_contract,
    load_runtime_pointer_identity,
)


EXPERIMENT_ROOT = Path(__file__).resolve().parents[2]
HOST_RUNTIME_SCHEMA = "step5d.autotune-v3/host-runtime-gate-v1"
HOST_GATE_MARKER = "STEP5D_V3_HOST_RUNTIME_GATE"
ENTRYPOINT_RUNTIME_MISMATCH = "ENTRYPOINT_RUNTIME_MISMATCH"
HOST_IMPORT_MISSING = "HOST_IMPORT_MISSING"
HOST_CONTRACT_MISMATCH = "HOST_CONTRACT_MISMATCH"
RUNTIME_LOCK_MISMATCH = "RUNTIME_LOCK_MISMATCH"
_DPKG_QUERY = Path("/usr/bin/dpkg-query")
_GATE_TIMEOUT_S = 45.0
DEFAULT_PREWARM_ARGV = (
    "--bridge-mode",
    "line",
    "--bridge-profile",
    "step5d_strict_rnn_autotune_v1",
    "--step5d-rnn-backend",
    "numpy",
    "--step5d-rnn-inner-iterations",
    "4",
)


_PROBE_SOURCE = r'''
import importlib
import json
import pathlib
import sys
import traceback

paths = json.loads(sys.argv[1])
required = json.loads(sys.argv[2])
bridge_argv = json.loads(sys.argv[3])
ros_prefix = pathlib.Path(sys.argv[4]).resolve()
runtime_source = pathlib.Path(sys.argv[5]).resolve()
result = {
    "schema": "step5d.autotune-v3/host-runtime-gate-v1",
    "ok": False,
    "host_runtime_ready": False,
    "startup_prewarm_passed": False,
    "sensor_connectivity": "not_run",
    "live_acceptance": False,
    "imports": {},
}

def under(path, root):
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return False
    return True

try:
    sys.path[:0] = [str(pathlib.Path(value).resolve()) for value in paths]
    for name in sorted(required):
        try:
            module = importlib.import_module(name)
        except (ModuleNotFoundError, ImportError) as exc:
            result.update({
                "reason_code": "HOST_IMPORT_MISSING",
                "detail": "host import %r unavailable: %s" % (name, exc),
            })
            print(json.dumps(result, sort_keys=True))
            raise SystemExit(2)
        module_file = getattr(module, "__file__", None)
        if not module_file or not under(pathlib.Path(module_file), ros_prefix):
            result.update({
                "reason_code": "HOST_CONTRACT_MISMATCH",
                "detail": "host import %r resolved outside ROS prefix: %s" % (name, module_file),
            })
            print(json.dumps(result, sort_keys=True))
            raise SystemExit(3)
        result["imports"][name] = str(pathlib.Path(module_file).resolve())

    runtime_module = importlib.import_module("ur10e_experiment_runtime")
    runtime_file = getattr(runtime_module, "__file__", None)
    if not runtime_file or not under(pathlib.Path(runtime_file), runtime_source):
        result.update({
            "reason_code": "ENTRYPOINT_RUNTIME_MISMATCH",
            "detail": "ur10e_experiment_runtime resolved outside managed source: %s" % runtime_file,
        })
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(4)
    result["imports"]["ur10e_experiment_runtime"] = str(pathlib.Path(runtime_file).resolve())

    wrapper = importlib.import_module("run_step5d_autotune_v3_bridge")
    prewarm = wrapper.check_v3_runtime_prewarm(bridge_argv)
    result.update({
        "ok": True,
        "host_runtime_ready": True,
        "startup_prewarm_passed": prewarm.get("ok") is True,
        "prewarm": prewarm,
    })
    if not result["startup_prewarm_passed"]:
        result.update({
            "reason_code": "ENTRYPOINT_RUNTIME_MISMATCH",
            "detail": "managed Step5d startup prewarm returned ok=false",
        })
        result["ok"] = False
        result["host_runtime_ready"] = False
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(4)
    print(json.dumps(result, sort_keys=True))
except SystemExit as exc:
    if result.get("reason_code"):
        raise
    result.update({
        "reason_code": "ENTRYPOINT_RUNTIME_MISMATCH",
        "detail": "managed Step5d startup prewarm exited unexpectedly: "
        + str(exc),
    })
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(4)
except (ModuleNotFoundError, ImportError) as exc:
    missing = getattr(exc, "name", None)
    known_host = set(required)
    if missing in known_host:
        reason = "HOST_IMPORT_MISSING"
        detail = "host import %r unavailable: %s" % (missing, exc)
    else:
        reason = "ENTRYPOINT_RUNTIME_MISMATCH"
        detail = "managed Step5d import/prewarm failed: %s" % exc
    result.update({"reason_code": reason, "detail": detail})
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(4)
except Exception as exc:
    result.update({
        "reason_code": "ENTRYPOINT_RUNTIME_MISMATCH",
        "detail": "managed Step5d startup prewarm failed: %s" % exc,
    })
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(4)
'''


def _required_host_imports(contract: Mapping[str, Any]) -> dict[str, str]:
    ros = contract.get("ros")
    if not isinstance(ros, Mapping):
        raise RuntimeInstallationError(
            HOST_CONTRACT_MISMATCH, "ROS host contract is missing"
        )
    mapping = ros.get("required_python_imports")
    packages = ros.get("packages")
    if (
        not isinstance(mapping, Mapping)
        or not mapping
        or not isinstance(packages, Mapping)
        or any(
            not isinstance(module, str)
            or not module
            or not isinstance(package, str)
            or not package
            or package not in packages
            for module, package in mapping.items()
        )
    ):
        raise RuntimeInstallationError(
            HOST_CONTRACT_MISMATCH,
            "ROS required Python imports do not bind to declared packages",
        )
    return {str(module): str(package) for module, package in mapping.items()}


def _verify_host_packages(contract: Mapping[str, Any]) -> None:
    ros = contract["ros"]
    packages = ros["packages"]
    if not _DPKG_QUERY.is_file() or not os.access(_DPKG_QUERY, os.X_OK):
        raise RuntimeInstallationError(
            HOST_CONTRACT_MISMATCH, "dpkg-query is unavailable"
        )
    environment = {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
    for package, expected in sorted(packages.items()):
        if not isinstance(package, str) or not package or not isinstance(expected, str):
            raise RuntimeInstallationError(
                HOST_CONTRACT_MISMATCH, "ROS package contract contains an invalid row"
            )
        completed = subprocess.run(
            [str(_DPKG_QUERY), "-W", "-f=${Version}", package],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
        )
        observed = completed.stdout.strip()
        if completed.returncode != 0 or observed != expected:
            raise RuntimeInstallationError(
                HOST_CONTRACT_MISMATCH,
                f"ROS package {package} differs: expected {expected}, observed {observed or 'missing'}",
            )


def _marker(pointer: Mapping[str, Any]) -> str:
    return ":".join(
        (
            str(pointer["bundle_id"]),
            str(pointer["attestation_sha256"]),
            str(pointer["profiles"]["control"]["python_executable"]),
        )
    )


def _current_process_is_bound(pointer: Mapping[str, Any]) -> bool:
    control_python = Path(
        str(pointer["profiles"]["control"]["python_executable"])
    )
    try:
        executable_matches = Path(sys.executable).resolve() == control_python.resolve()
    except OSError:
        executable_matches = False
    return executable_matches and os.environ.get(HOST_GATE_MARKER) == _marker(pointer)


def _child_result(
    pointer: Mapping[str, Any],
    contract: Mapping[str, Any],
    environment: Mapping[str, str],
    bridge_argv: Sequence[str],
) -> dict[str, Any]:
    path_entries = [value for value in environment.get("PYTHONPATH", "").split(os.pathsep) if value]
    command = [
        str(pointer["profiles"]["control"]["python_executable"]),
        "-B",
        "-I",
        "-c",
        _PROBE_SOURCE,
        json.dumps(path_entries, sort_keys=True),
        json.dumps(_required_host_imports(contract), sort_keys=True),
        json.dumps(list(bridge_argv)),
        str(Path(contract["ros"]["prefix"]).resolve()),
        str(RUNTIME_SOURCE.resolve()),
    ]
    child_environment = dict(environment)
    child_environment.pop(HOST_GATE_MARKER, None)
    completed = subprocess.run(
        command,
        cwd=str(EXPERIMENT_ROOT),
        env=child_environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=_GATE_TIMEOUT_S,
    )
    payload: dict[str, Any] | None = None
    for line in reversed(completed.stdout.splitlines()):
        if not line.strip():
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if payload is None:
        raise RuntimeInstallationError(
            ENTRYPOINT_RUNTIME_MISMATCH,
            "managed host gate returned no structured result",
        )
    if completed.returncode != 0 or payload.get("ok") is not True:
        reason = payload.get("reason_code")
        detail = payload.get("detail")
        if not isinstance(reason, str) or not reason:
            reason = ENTRYPOINT_RUNTIME_MISMATCH
        if not isinstance(detail, str) or not detail:
            detail = "managed host gate exited before readiness"
        raise RuntimeInstallationError(reason, detail)
    return payload


def run_host_runtime_gate(
    *,
    runtime_pointer: Mapping[str, Any] | None = None,
    bridge_argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate ROS host imports and prewarm the exact managed control path."""

    source = os.environ if environ is None else environ
    pointer = (
        load_runtime_pointer_identity(environ=source)
        if runtime_pointer is None
        else runtime_pointer
    )
    if _current_process_is_bound(pointer):
        return {
            "schema": HOST_RUNTIME_SCHEMA,
            "ok": True,
            "host_runtime_ready": True,
            "startup_prewarm_passed": True,
            "sensor_connectivity": "not_run",
            "live_acceptance": False,
            "cached": True,
            "runtime_bundle_id": pointer["bundle_id"],
        }
    contract = load_runtime_contract()
    _required_host_imports(contract)
    _verify_host_packages(contract)
    environment = production_runtime_environment(
        source,
        profile="control",
        runtime_pointer=pointer,
    )
    payload = _child_result(
        pointer,
        contract,
        environment,
        DEFAULT_PREWARM_ARGV if bridge_argv is None else bridge_argv,
    )
    payload.setdefault("runtime_bundle_id", pointer["bundle_id"])
    payload.setdefault("sensor_connectivity", "not_run")
    payload.setdefault("live_acceptance", False)
    os.environ[HOST_GATE_MARKER] = _marker(pointer)
    return payload


def host_runtime_status(
    *,
    runtime_pointer: Mapping[str, Any] | None = None,
    bridge_argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    try:
        return run_host_runtime_gate(
            runtime_pointer=runtime_pointer,
            bridge_argv=bridge_argv,
            environ=environ,
        )
    except RuntimeInstallationError as exc:
        return {
            "schema": HOST_RUNTIME_SCHEMA,
            "ok": False,
            "host_runtime_ready": False,
            "startup_prewarm_passed": False,
            "sensor_connectivity": "not_run",
            "live_acceptance": False,
            "reason_code": exc.reason_code,
            "detail": exc.detail,
        }
    except Exception as exc:
        return {
            "schema": HOST_RUNTIME_SCHEMA,
            "ok": False,
            "host_runtime_ready": False,
            "startup_prewarm_passed": False,
            "sensor_connectivity": "not_run",
            "live_acceptance": False,
            "reason_code": HOST_CONTRACT_MISMATCH,
            "detail": f"host runtime gate failed closed: {exc}",
        }


def require_host_runtime(
    *,
    runtime_pointer: Mapping[str, Any] | None = None,
    bridge_argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    result = run_host_runtime_gate(
        runtime_pointer=runtime_pointer,
        bridge_argv=bridge_argv,
        environ=environ,
    )
    if result.get("ok") is not True:
        raise RuntimeInstallationError(
            str(result.get("reason_code") or ENTRYPOINT_RUNTIME_MISMATCH),
            str(result.get("detail") or "host runtime gate did not pass"),
        )
    return result


__all__ = [
    "DEFAULT_PREWARM_ARGV",
    "ENTRYPOINT_RUNTIME_MISMATCH",
    "HOST_CONTRACT_MISMATCH",
    "HOST_GATE_MARKER",
    "HOST_IMPORT_MISSING",
    "HOST_RUNTIME_SCHEMA",
    "RUNTIME_LOCK_MISMATCH",
    "host_runtime_status",
    "require_host_runtime",
    "run_host_runtime_gate",
]
