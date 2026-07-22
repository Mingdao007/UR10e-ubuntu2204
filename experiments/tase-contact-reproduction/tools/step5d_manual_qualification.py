#!/usr/bin/env python3
"""Qualify the real Manual V2 startup path against no-motion localhost endpoints."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Mapping
import uuid

from step5d_autotune_v3.qualification import (
    QUALIFICATION_ENDPOINT_PORTS,
    QualificationBlocked,
    qualification_endpoint_lease,
    read_process_starttime,
)
from step5d_autotune_v3.qualification_endpoints import QualificationEndpointSimulator
from step5d_autotune_v3.runtime_environment import production_runtime_environment
from step5d_autotune_v3.runtime_functional_gates import load_gpu_functional_attestation
from step5d_autotune_v3.runtime_installation import load_runtime_pointer
from step5d_autotune_v3.state import atomic_json
from step5d_manual_bridge import (
    PROGRAM,
    ROOT,
    build_context,
    require_canonical_shell,
    strict_object,
    write_once,
)
from step5d_manual_profile import DEFAULT_LAUNCH_PROFILE
from promote_step5d_manual_release import load_manual_release


RESULT_SCHEMA = "step5d.manual-v2/production-startup-qualification-v1"
CONTRACT_SCHEMA = "step5d.manual-v2/internal-qualification-shell-contract-v1"
CONTRACT_ENV = "STEP5D_MANUAL_INTERNAL_QUALIFICATION_SHELL_CONTRACT"
CONTRACT_SHA_ENV = "STEP5D_MANUAL_INTERNAL_QUALIFICATION_SHELL_CONTRACT_SHA256"
SHELL_PID_ENV = "STEP5D_MANUAL_INTERNAL_QUALIFICATION_SHELL_PID"
QUALIFIED = "MANUAL_BRIDGE_PERSISTENT_NO_ARM_PROVEN"
FAILED = "MANUAL_PRODUCTION_STARTUP_QUALIFICATION_FAILED"
QUALIFICATION_PLAY_TIMEOUT_S = 30.0


class ManualQualificationError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ManualQualificationError(f"required regular file is missing: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_ref(path: Path) -> dict[str, str]:
    target = path.resolve(strict=True)
    return {"path": str(target), "sha256": _sha256(target)}


def _executable_sha256(path: Path) -> str:
    """Hash an interpreter target without discarding its venv path semantics."""

    target = path.resolve(strict=True)
    if not target.is_file():
        raise ManualQualificationError(f"required executable is missing: {path}")
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _executable_ref(path: Path) -> dict[str, str]:
    exact = path.expanduser().absolute()
    if not exact.exists():
        raise ManualQualificationError(f"required executable is missing: {exact}")
    return {"path": str(exact), "sha256": _executable_sha256(exact)}


def _cmdline(pid: int) -> list[str]:
    try:
        return [
            item.decode("utf-8", errors="surrogateescape")
            for item in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if item
        ]
    except OSError:
        return []


def _children(pid: int) -> list[int]:
    try:
        return [
            int(item)
            for item in Path(f"/proc/{pid}/task/{pid}/children")
            .read_text(encoding="ascii")
            .split()
        ]
    except (OSError, ValueError):
        return []


def _descendants(pid: int) -> list[int]:
    pending = _children(pid)
    result: list[int] = []
    seen: set[int] = set()
    while pending:
        child = pending.pop(0)
        if child in seen:
            continue
        seen.add(child)
        result.append(child)
        pending.extend(_children(child))
    return result


def _process_row(pid: int, role: str) -> dict[str, Any]:
    starttime = read_process_starttime(pid)
    argv = _cmdline(pid)
    if starttime is None or not argv:
        raise ManualQualificationError(f"{role} process disappeared")
    return {
        "role": role,
        "pid": pid,
        "starttime_ticks": starttime,
        "argv": argv,
        "argv_sha256": hashlib.sha256(
            json.dumps(argv, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest(),
    }


def _wait_process_tree(
    shell: subprocess.Popen[Any], root: Path, timeout_s: float
) -> dict[str, dict[str, Any]]:
    expected = {
        "manual_owner": str((root / "tools/run_step5d_manual_bridge_live.py").resolve()),
        "production_bridge": str((root / "tools/run_step5d_manual_bridge.py").resolve()),
    }
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if shell.poll() is not None:
            break
        found: dict[str, int] = {}
        for pid in _descendants(shell.pid):
            argv = _cmdline(pid)
            for role, path in expected.items():
                if path in argv:
                    found[role] = pid
        if set(found) == set(expected):
            return {
                "canonical_shell": _process_row(shell.pid, "canonical_shell"),
                **{role: _process_row(found[role], role) for role in expected},
            }
        time.sleep(0.02)
    raise ManualQualificationError(
        f"Manual production process tree was incomplete; shell_rc={shell.poll()}"
    )


def _wait_json(path: Path, process: subprocess.Popen[Any], timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ManualQualificationError(
                f"Manual canonical shell exited before {path.name}; rc={process.returncode}"
            )
        if path.is_file() and not path.is_symlink():
            return strict_object(path, path.name)
        time.sleep(0.02)
    raise ManualQualificationError(f"timed out waiting for {path.name}")


def _csv_rows(path: Path) -> list[dict[str, str]]:
    if path.is_symlink() or not path.is_file():
        return []
    with path.open(newline="") as stream:
        return [
            dict(row)
            for row in csv.DictReader(stream)
            if row.get("write_index") not in {None, ""}
        ]


def _persistent_no_arm_canary(
    shell: subprocess.Popen[Any], csv_path: Path, *, timeout_s: float = 8.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    baseline: dict[str, str] | None = None
    baseline_at = 0.0
    while time.monotonic() < deadline:
        if shell.poll() is not None:
            raise ManualQualificationError(
                f"Manual shell exited during startup canary rc={shell.returncode}"
            )
        rows = _csv_rows(csv_path)
        if rows and baseline is None:
            baseline = rows[-1]
            baseline_at = time.monotonic()
        if baseline is not None and time.monotonic() - baseline_at >= 1.0 and rows:
            later = rows[-1]
            try:
                write_delta = int(float(later["write_index"])) - int(
                    float(baseline["write_index"])
                )
                heartbeat_delta = int(float(later["heartbeat"])) - int(
                    float(baseline["heartbeat"])
                )
                elapsed = float(later["t_monotonic_s"]) - float(
                    baseline["t_monotonic_s"]
                )
                safe = all(
                    int(float(later[name])) == expected
                    for name, expected in {
                        "command": 0,
                        "stop_request": 0,
                        "rtde_connected": 1,
                        "sensor_ok": 1,
                        "ur_safety_mode": 1,
                        "step4e_controller_state": 0,
                        "ur_output_int_register_26": 10,
                    }.items()
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ManualQualificationError(
                    "Manual startup canary row is incomplete"
                ) from exc
            if write_delta < 20 or heartbeat_delta < 20 or elapsed < 0.75 or not safe:
                raise ManualQualificationError(
                    "Manual bridge did not sustain the NO_ARM production canary"
                )
            projection = {
                key: later[key]
                for key in (
                    "write_index",
                    "t_monotonic_s",
                    "heartbeat",
                    "command",
                    "stop_request",
                    "rtde_connected",
                    "sensor_ok",
                    "ur_safety_mode",
                    "step4e_controller_state",
                    "ur_output_int_register_26",
                )
            }
            return {
                "observed_duration_s": elapsed,
                "write_index_delta": write_delta,
                "heartbeat_delta": heartbeat_delta,
                "last_row": projection,
                "last_row_sha256": hashlib.sha256(
                    json.dumps(
                        projection,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode()
                ).hexdigest(),
            }
        time.sleep(0.02)
    raise ManualQualificationError("Manual startup canary timed out")


def _contract_payload(
    root: Path,
    run_root: Path,
    *,
    live_root: Path,
    context_path: Path,
    preflight_path: Path,
    endpoint_path: Path,
    python_executable: str,
    ready_timeout_s: float,
) -> dict[str, Any]:
    launcher = (root / "scripts/step5d-autotune-v3.sh").resolve(strict=True)
    caller_starttime = read_process_starttime(os.getpid())
    if caller_starttime is None:
        raise ManualQualificationError("Manual qualification caller disappeared")
    requested_argv = [
        str(launcher),
        "bridge",
        "--output-root",
        str(live_root),
        "--campaign-root",
        str(run_root / "campaign"),
        "--ready-timeout-s",
        str(ready_timeout_s),
        "--play-timeout-s",
        str(QUALIFICATION_PLAY_TIMEOUT_S),
    ]
    return {
        "schema": CONTRACT_SCHEMA,
        "launch_attempt_id": uuid.uuid4().hex,
        "campaign_id": f"manual-qualification-{uuid.uuid4().hex}",
        "caller": {"pid": os.getpid(), "starttime_ticks": caller_starttime},
        "run_root": str(run_root),
        "output_root": str(live_root),
        "canonical_launcher": _file_ref(launcher),
        "live_owner": _file_ref(root / "tools/run_step5d_manual_bridge_live.py"),
        "python": _executable_ref(Path(python_executable)),
        "bridge_start_context": _file_ref(context_path),
        "preflight": _file_ref(preflight_path),
        "qualification_endpoints": _file_ref(endpoint_path),
        "launch_profile": _file_ref(DEFAULT_LAUNCH_PROFILE),
        "ready_timeout_s": ready_timeout_s,
        "requested_argv": requested_argv,
    }


def _validate_contract(
    root: Path,
    payload: Mapping[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    values = os.environ if environment is None else environment
    required = {
        "schema",
        "launch_attempt_id",
        "campaign_id",
        "caller",
        "run_root",
        "output_root",
        "canonical_launcher",
        "live_owner",
        "python",
        "bridge_start_context",
        "preflight",
        "qualification_endpoints",
        "launch_profile",
        "ready_timeout_s",
        "requested_argv",
    }
    if set(payload) != required or payload.get("schema") != CONTRACT_SCHEMA:
        raise ManualQualificationError("Manual qualification shell contract fields differ")
    launch_attempt_id = payload.get("launch_attempt_id")
    campaign_id = payload.get("campaign_id")
    if (
        not isinstance(launch_attempt_id, str)
        or len(launch_attempt_id) != 32
        or any(character not in "0123456789abcdef" for character in launch_attempt_id)
        or not isinstance(campaign_id, str)
        or not campaign_id.startswith("manual-qualification-")
    ):
        raise ManualQualificationError("Manual qualification campaign identity differs")
    caller = payload.get("caller")
    if not isinstance(caller, Mapping) or set(caller) != {"pid", "starttime_ticks"}:
        raise ManualQualificationError("Manual qualification caller binding differs")
    for role in (
        "canonical_launcher",
        "live_owner",
        "bridge_start_context",
        "preflight",
        "qualification_endpoints",
        "launch_profile",
    ):
        reference = payload.get(role)
        if not isinstance(reference, Mapping) or set(reference) != {"path", "sha256"}:
            raise ManualQualificationError(f"Manual qualification {role} reference differs")
        path = Path(str(reference["path"]))
        if not path.is_absolute() or _sha256(path) != reference["sha256"]:
            raise ManualQualificationError(f"Manual qualification {role} bytes differ")
    python_reference = payload.get("python")
    if not isinstance(python_reference, Mapping) or set(python_reference) != {
        "path",
        "sha256",
    }:
        raise ManualQualificationError("Manual qualification python reference differs")
    python_path = Path(str(python_reference["path"]))
    if (
        not python_path.is_absolute()
        or str(python_path) != values.get("STEP5D_V3_CONTROL_PYTHON")
        or _executable_sha256(python_path) != python_reference["sha256"]
    ):
        raise ManualQualificationError("Manual qualification python binding differs")
    launcher = str((root / "scripts/step5d-autotune-v3.sh").resolve(strict=True))
    if payload["canonical_launcher"]["path"] != launcher:
        raise ManualQualificationError("Manual qualification launcher differs")
    run_root = Path(str(payload["run_root"]))
    output_root = Path(str(payload["output_root"]))
    if not run_root.is_absolute() or not output_root.is_absolute():
        raise ManualQualificationError("Manual qualification run paths must be absolute")
    try:
        output_root.relative_to(run_root)
        for role in ("bridge_start_context", "preflight", "qualification_endpoints"):
            Path(str(payload[role]["path"])).relative_to(run_root)
    except ValueError as exc:
        raise ManualQualificationError("Manual qualification evidence escapes its run") from exc
    expected_argv = [
        launcher,
        "bridge",
        "--output-root",
        str(output_root),
        "--campaign-root",
        str(run_root / "campaign"),
        "--ready-timeout-s",
        str(payload["ready_timeout_s"]),
        "--play-timeout-s",
        str(QUALIFICATION_PLAY_TIMEOUT_S),
    ]
    if payload.get("requested_argv") != expected_argv:
        raise ManualQualificationError("Manual qualification canonical argv differs")
    return dict(payload)


def validate_result(
    root: Path,
    path: Path,
    *,
    release_manifest_sha256: str,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    payload = strict_object(path.expanduser().absolute(), "Manual qualification result")
    required = {
        "schema",
        "ok",
        "state",
        "reason_code",
        "blocker",
        "started_at_unix_ns",
        "completed_at_unix_ns",
        "manual_release_manifest_sha256",
        "manual_host_binding_sha256",
        "source_surface_sha256",
        "runtime",
        "process_tree",
        "bridge_launch",
        "bridge_ready",
        "preflight",
        "qualification_endpoints",
        "endpoint_evidence",
        "gpu_functional",
        "endpoint_pre_stop_sha256",
        "canary",
        "process_shutdown",
        "canonical_shell_returncode",
        "capabilities",
        "play_prompt_ready",
    }
    if (
        set(payload) != required
        or payload.get("schema") != RESULT_SCHEMA
        or payload.get("ok") is not True
        or payload.get("state") != QUALIFIED
        or payload.get("reason_code") != QUALIFIED
        or payload.get("blocker") is not None
        or payload.get("manual_release_manifest_sha256")
        != release_manifest_sha256
        or payload.get("canonical_shell_returncode") not in {0, 130}
        or payload.get("play_prompt_ready") is not False
    ):
        raise ManualQualificationError("Manual production qualification did not pass")
    release = load_manual_release(root.resolve(strict=True))
    if release["manifest_sha256"] != release_manifest_sha256:
        raise ManualQualificationError("Manual qualification release is not current")
    if (
        payload.get("manual_host_binding_sha256")
        != release["host_binding_sha256"]
        or payload.get("source_surface_sha256")
        != release["source_surface_sha256"]
    ):
        raise ManualQualificationError("Manual qualification host source binding drifted")
    runtime = load_runtime_pointer(
        environ=os.environ if environment is None else environment
    )
    _gpu_payload, current_gpu_reference = load_gpu_functional_attestation(
        runtime_pointer=runtime
    )
    if payload.get("gpu_functional") != current_gpu_reference:
        raise ManualQualificationError("Manual qualification GPU evidence drifted")
    observed_runtime = payload.get("runtime")
    if not isinstance(observed_runtime, Mapping) or any(
        (
            observed_runtime.get("bundle_id") != runtime["bundle_id"],
            observed_runtime.get("attestation_sha256")
            != runtime["attestation_sha256"],
            observed_runtime.get("control_environment_id")
            != runtime["profiles"]["control"]["environment_id"],
        )
    ):
        raise ManualQualificationError("Manual qualification runtime drifted")
    capabilities = payload.get("capabilities")
    if capabilities != {
        "bridge": True,
        "play": False,
        "arm": False,
        "motion": False,
        "zero": False,
        "tare": False,
    }:
        raise ManualQualificationError("Manual qualification capability scope differs")
    for role in (
        "bridge_launch",
        "bridge_ready",
        "preflight",
        "qualification_endpoints",
        "endpoint_evidence",
    ):
        reference = payload.get(role)
        if not isinstance(reference, Mapping) or set(reference) != {"path", "sha256"}:
            raise ManualQualificationError(f"Manual qualification {role} reference differs")
        if _sha256(Path(str(reference["path"]))) != reference["sha256"]:
            raise ManualQualificationError(f"Manual qualification {role} bytes drifted")
    control_python = observed_runtime.get("control_python")
    if (
        not isinstance(control_python, Mapping)
        or control_python.get("path")
        != runtime["profiles"]["control"]["python_executable"]
        or _executable_sha256(Path(str(control_python.get("path", ""))))
        != control_python.get("sha256")
    ):
        raise ManualQualificationError("Manual qualification interpreter drifted")
    process_tree = payload.get("process_tree")
    if not isinstance(process_tree, Mapping) or set(process_tree) != {
        "canonical_shell",
        "manual_owner",
        "production_bridge",
    }:
        raise ManualQualificationError("Manual qualification process tree differs")
    expected_paths = {
        "canonical_shell": str(
            (root / "scripts/step5d-autotune-v3.sh").resolve(strict=True)
        ),
        "manual_owner": str(
            (root / "tools/run_step5d_manual_bridge_live.py").resolve(strict=True)
        ),
        "production_bridge": str(
            (root / "tools/run_step5d_manual_bridge.py").resolve(strict=True)
        ),
    }
    for role, expected_path in expected_paths.items():
        row = process_tree.get(role)
        if (
            not isinstance(row, Mapping)
            or row.get("role") != role
            or expected_path not in row.get("argv", [])
            or not isinstance(row.get("pid"), int)
            or not isinstance(row.get("starttime_ticks"), int)
        ):
            raise ManualQualificationError(
                f"Manual qualification {role} provenance differs"
            )
    canary = payload.get("canary")
    if (
        not isinstance(canary, Mapping)
        or canary.get("write_index_delta", 0) < 20
        or canary.get("heartbeat_delta", 0) < 20
        or canary.get("observed_duration_s", 0.0) < 0.75
    ):
        raise ManualQualificationError("Manual qualification startup canary differs")
    shutdown = payload.get("process_shutdown")
    if (
        not isinstance(shutdown, Mapping)
        or shutdown.get("all_processes_gone") is not True
        or shutdown.get("bridge_returncode") not in {0, 130}
    ):
        raise ManualQualificationError("Manual qualification process shutdown differs")
    closure_reference = shutdown.get("bridge_closure")
    if (
        not isinstance(closure_reference, Mapping)
        or set(closure_reference) != {"path", "sha256"}
        or _sha256(Path(str(closure_reference["path"])))
        != closure_reference["sha256"]
    ):
        raise ManualQualificationError("Manual qualification bridge closure drifted")
    closure = strict_object(
        Path(str(closure_reference["path"])), "Manual qualification bridge closure"
    )
    if (
        closure.get("schema") != "step5d.manual-hold/bridge-closure-v1"
        or closure.get("bridge_rc") != shutdown.get("bridge_returncode")
        or closure.get("arm_authorized") is not False
        or closure.get("motion_authorized") is not False
    ):
        raise ManualQualificationError("Manual qualification bridge closure differs")
    endpoint = strict_object(
        Path(payload["endpoint_evidence"]["path"]),
        "Manual qualification endpoint evidence",
    )
    endpoint_config = strict_object(
        Path(payload["qualification_endpoints"]["path"]),
        "Manual qualification endpoint config",
    )
    if endpoint.get("content_sha256") != endpoint_config.get("content_sha256"):
        raise ManualQualificationError("Manual qualification endpoint binding differs")
    return payload


def exec_shell_contract(root: Path, contract_path: Path) -> None:
    require_canonical_shell()
    path = contract_path.expanduser().absolute()
    if path.is_symlink() or not path.is_file() or path.resolve(strict=True) != path:
        raise ManualQualificationError("Manual qualification contract path is unsafe")
    if (
        os.environ.get(CONTRACT_ENV) != str(path)
        or os.environ.get(CONTRACT_SHA_ENV) != _sha256(path)
    ):
        raise ManualQualificationError("Manual qualification contract environment differs")
    contract = _validate_contract(root, strict_object(path, "Manual qualification contract"))
    try:
        shell_pid = int(os.environ[SHELL_PID_ENV])
    except (KeyError, ValueError) as exc:
        raise ManualQualificationError("Manual qualification shell PID is missing") from exc
    if shell_pid != os.getppid():
        raise ManualQualificationError("Manual qualification worker is not a shell child")
    caller = contract["caller"]
    try:
        shell_parent = int(
            next(
                line.split()[1]
                for line in Path(f"/proc/{shell_pid}/status")
                .read_text(encoding="ascii")
                .splitlines()
                if line.startswith("PPid:")
            )
        )
    except (OSError, StopIteration, ValueError) as exc:
        raise ManualQualificationError("Manual qualification shell process differs") from exc
    if (
        shell_parent != caller["pid"]
        or read_process_starttime(caller["pid"]) != caller["starttime_ticks"]
    ):
        raise ManualQualificationError("Manual qualification caller process differs")
    shell_argv = _cmdline(shell_pid)
    requested = contract["requested_argv"]
    if shell_argv[-len(requested) :] != requested:
        raise ManualQualificationError("Manual qualification shell argv differs")
    owner_argv = [
        contract["python"]["path"],
        contract["live_owner"]["path"],
        "--output-root",
        contract["output_root"],
        "--bridge-start-context",
        contract["bridge_start_context"]["path"],
        "--preflight",
        contract["preflight"]["path"],
        "--launch-profile",
        contract["launch_profile"]["path"],
        "--qualification-endpoints",
        contract["qualification_endpoints"]["path"],
        "--launch-attempt-id",
        contract["launch_attempt_id"],
        "--campaign-id",
        contract["campaign_id"],
        "--canonical-owner-pid",
        str(shell_pid),
        "--canonical-owner-starttime",
        str(read_process_starttime(shell_pid)),
        "--ready-timeout-s",
        str(contract["ready_timeout_s"]),
    ]
    os.execve(owner_argv[0], owner_argv, dict(os.environ))


def _stop_group(process: subprocess.Popen[Any] | None) -> int | None:
    if process is None:
        return None
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=12.0)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5.0)
    return process.returncode


def run_qualification(
    root: Path,
    output_root: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    output = output_root.expanduser().absolute()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    result_path = output / "manual-qualification-result.json"
    if result_path.exists() or result_path.is_symlink():
        raise ManualQualificationError("Manual qualification result already exists")
    values = dict(os.environ if environment is None else environment)
    release = load_manual_release(root)
    runtime = load_runtime_pointer(environ=values)
    _gpu_payload, gpu_reference = load_gpu_functional_attestation(
        runtime_pointer=runtime
    )
    control_python = runtime["profiles"]["control"]["python_executable"]
    if Path(sys.executable).resolve() != Path(control_python).resolve():
        raise ManualQualificationError("Manual qualifier is not using the control runtime")
    additions = {
        name: values[name]
        for name in ("UR10E_LOCK_ROOT",)
        if values.get(name)
    }
    clean_environment = production_runtime_environment(
        values,
        additions=additions or None,
        runtime_pointer=runtime,
    )
    run_root = output / "manual-qualification" / "runs" / uuid.uuid4().hex
    run_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    live_root = run_root / "live"
    context_path = run_root / "bridge-start-context.json"
    preflight_path = run_root / "preflight.json"
    preflight_log = run_root / "preflight.log"
    endpoint_path = run_root / "qualification_endpoints.json"
    shell_log_path = run_root / "canonical-shell.log"
    endpoint_evidence_path = run_root / "endpoint-evidence.json"
    started_at = time.time_ns()
    shell: subprocess.Popen[Any] | None = None
    endpoint_pre_stop: Mapping[str, Any] = {}
    process_tree: Mapping[str, Any] | None = None
    launch: Mapping[str, Any] | None = None
    ready: Mapping[str, Any] | None = None
    canary: Mapping[str, Any] | None = None
    process_shutdown: Mapping[str, Any] | None = None
    blocker: str | None = None
    shell_rc: int | None = None
    endpoint_simulator: QualificationEndpointSimulator | None = None
    try:
        with (
            qualification_endpoint_lease(
                run_root,
                environment=values,
                task="step5d-manual-v2-production-qualification",
            ),
            QualificationEndpointSimulator(
                dashboard_port=QUALIFICATION_ENDPOINT_PORTS["dashboard"],
                secondary_port=QUALIFICATION_ENDPOINT_PORTS["secondary"],
                rtde_port=QUALIFICATION_ENDPOINT_PORTS["rtde"],
                kunwei_port=QUALIFICATION_ENDPOINT_PORTS["kunwei"],
                expected_program_id=PROGRAM,
                loaded_program=f"/programs/andyl/kunwei/step5/{PROGRAM}.urp",
            ) as endpoints,
        ):
            endpoint_simulator = endpoints
            atomic_json(
                endpoint_path,
                {
                    "schema": "step5d.autotune-v3/qualification-endpoint-config-v1",
                    "content_sha256": endpoints.content_sha256,
                    "addresses": endpoints.addresses,
                    "motion_capable": False,
                },
            )
            context = build_context(
                root,
                plant_epoch=1,
                launch_profile_path=DEFAULT_LAUNCH_PROFILE,
            )
            write_once(context_path, context)
            preflight_command = [
                control_python,
                str(root / "tools/preflight_step5d_manual_bridge.py"),
                "--robot-host",
                "127.0.0.1",
                "--sensor-ip",
                "127.0.0.1",
                "--sensor-port",
                str(QUALIFICATION_ENDPOINT_PORTS["kunwei"]),
                "--mailbox",
                str(live_root / "runtime/command.json"),
                "--bridge-start-context",
                str(context_path),
                "--launch-profile",
                str(DEFAULT_LAUNCH_PROFILE),
                "--output",
                str(preflight_path),
                "--qualification-endpoints",
                str(endpoint_path),
            ]
            with preflight_log.open("wb") as log:
                preflight = subprocess.run(
                    preflight_command,
                    cwd=root,
                    env=clean_environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=90.0,
                    check=False,
                    close_fds=True,
                )
            if preflight.returncode != 0:
                raise ManualQualificationError(
                    f"Manual production preflight failed rc={preflight.returncode}"
                )
            preflight_payload = strict_object(preflight_path, "Manual qualification preflight")
            if preflight_payload.get("ok") is not True:
                raise ManualQualificationError("Manual production preflight did not pass")
            contract = _contract_payload(
                root,
                run_root,
                live_root=live_root,
                context_path=context_path,
                preflight_path=preflight_path,
                endpoint_path=endpoint_path,
                python_executable=control_python,
                ready_timeout_s=30.0,
            )
            _validate_contract(root, contract, environment=clean_environment)
            contract_path = run_root / "internal-shell-contract.json"
            atomic_json(contract_path, contract)
            shell_environment = {
                **clean_environment,
                CONTRACT_ENV: str(contract_path),
                CONTRACT_SHA_ENV: _sha256(contract_path),
            }
            with shell_log_path.open("wb") as shell_log:
                shell = subprocess.Popen(
                    contract["requested_argv"],
                    cwd=root,
                    env=shell_environment,
                    stdin=subprocess.DEVNULL,
                    stdout=shell_log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    start_new_session=True,
                )
                process_tree = _wait_process_tree(shell, root, 60.0)
                launch = _wait_json(live_root / "bridge_launch.json", shell, 60.0)
                ready = _wait_json(
                    live_root / "runtime/bridge/bridge_ready.json", shell, 60.0
                )
                if any(
                    (
                        launch.get("parent_pid")
                        != process_tree["manual_owner"]["pid"],
                        launch.get("parent_starttime_ticks")
                        != process_tree["manual_owner"]["starttime_ticks"],
                        launch.get("pid")
                        != process_tree["production_bridge"]["pid"],
                        launch.get("pid_starttime_ticks")
                        != process_tree["production_bridge"]["starttime_ticks"],
                        launch.get("launch_attempt_id")
                        != contract["launch_attempt_id"],
                        launch.get("campaign_id") != contract["campaign_id"],
                        launch.get("launch_id") != ready.get("launch_nonce"),
                        launch.get("arm_authorized") is not False,
                        launch.get("motion_authorized") is not False,
                        ready.get("pid") != launch.get("pid"),
                        ready.get("ok") is not True,
                    )
                ):
                    raise ManualQualificationError(
                        "Manual startup process/readiness identity differs"
                    )
                canary = _persistent_no_arm_canary(
                    shell,
                    live_root / "runtime/bridge/bridge_rtde_500hz.csv",
                )
                endpoint_pre_stop = endpoints.evidence()
                counters = endpoint_pre_stop.get("counters", {})
                events = endpoint_pre_stop.get("events", [])
                if (
                    counters.get("rtde", {}).get("arm_acknowledgements") != 0
                    or any(
                        isinstance(event, Mapping)
                        and event.get("event") == "tp_arm_acknowledged"
                        for event in events
                    )
                ):
                    raise ManualQualificationError(
                        "Manual startup qualification observed an ARM acknowledgement"
                    )
            shell_rc = _stop_group(shell)
            if shell_rc not in {0, 130}:
                raise ManualQualificationError(
                    f"Manual canonical shell did not stop cleanly rc={shell_rc}"
                )
            closure_path = live_root / "bridge_closure.json"
            closure = strict_object(closure_path, "Manual qualification bridge closure")
            bridge_rc = closure.get("bridge_rc")
            if (
                closure.get("schema") != "step5d.manual-hold/bridge-closure-v1"
                or bridge_rc not in {0, 130}
                or closure.get("arm_authorized") is not False
                or closure.get("motion_authorized") is not False
            ):
                raise ManualQualificationError(
                    "Manual production bridge did not close cleanly"
                )
            still_alive = []
            for role, row in process_tree.items():
                if read_process_starttime(row["pid"]) == row["starttime_ticks"]:
                    still_alive.append(role)
            if still_alive:
                raise ManualQualificationError(
                    f"Manual production descendants survived shutdown: {still_alive}"
                )
            process_shutdown = {
                "all_processes_gone": True,
                "bridge_returncode": bridge_rc,
                "bridge_closure": _file_ref(closure_path),
            }
            atomic_json(endpoint_evidence_path, endpoints.evidence())
    except (OSError, ValueError, TimeoutError, QualificationBlocked, ManualQualificationError) as exc:
        blocker = f"{type(exc).__name__}:{exc}"
    finally:
        if shell_rc is None:
            shell_rc = _stop_group(shell)
        if endpoint_simulator is not None:
            try:
                final_endpoint_evidence = endpoint_simulator.evidence()
                atomic_json(endpoint_evidence_path, final_endpoint_evidence)
                if not endpoint_pre_stop:
                    endpoint_pre_stop = final_endpoint_evidence
            except (OSError, ValueError) as exc:
                if blocker is None:
                    blocker = f"{type(exc).__name__}:{exc}"
    result = {
        "schema": RESULT_SCHEMA,
        "ok": blocker is None,
        "state": QUALIFIED if blocker is None else "BLOCKED",
        "reason_code": QUALIFIED if blocker is None else FAILED,
        "blocker": blocker,
        "started_at_unix_ns": started_at,
        "completed_at_unix_ns": time.time_ns(),
        "manual_release_manifest_sha256": release["manifest_sha256"],
        "manual_host_binding_sha256": release["host_binding_sha256"],
        "source_surface_sha256": release["source_surface_sha256"],
        "runtime": {
            "bundle_id": runtime["bundle_id"],
            "attestation_sha256": runtime["attestation_sha256"],
            "control_environment_id": runtime["profiles"]["control"]["environment_id"],
            "control_python": _executable_ref(Path(control_python)),
        },
        "process_tree": process_tree,
        "bridge_launch": None
        if launch is None
        else _file_ref(live_root / "bridge_launch.json"),
        "bridge_ready": None
        if ready is None
        else _file_ref(live_root / "runtime/bridge/bridge_ready.json"),
        "preflight": _file_ref(preflight_path) if preflight_path.is_file() else None,
        "qualification_endpoints": _file_ref(endpoint_path)
        if endpoint_path.is_file()
        else None,
        "endpoint_evidence": _file_ref(endpoint_evidence_path)
        if endpoint_evidence_path.is_file()
        else None,
        "gpu_functional": gpu_reference,
        "endpoint_pre_stop_sha256": hashlib.sha256(
            json.dumps(
                endpoint_pre_stop,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        if endpoint_pre_stop
        else None,
        "canary": canary,
        "process_shutdown": process_shutdown,
        "canonical_shell_returncode": shell_rc,
        "capabilities": {
            "bridge": True,
            "play": False,
            "arm": False,
            "motion": False,
            "zero": False,
            "tare": False,
        },
        "play_prompt_ready": False,
    }
    atomic_json(result_path, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-root", type=Path, default=ROOT)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--_exec-live-from-shell-contract", type=Path)
    args = parser.parse_args(argv)
    try:
        if args._exec_live_from_shell_contract is not None:
            if args.output_root is not None:
                parser.error("internal shell execution cannot combine with --output-root")
            exec_shell_contract(args.experiment_root, args._exec_live_from_shell_contract)
            return 70
        if args.output_root is None:
            parser.error("--output-root is required")
        require_canonical_shell()
        result = run_qualification(args.experiment_root, args.output_root)
    except Exception as exc:
        print(f"Manual production qualification blocked: {type(exc).__name__}:{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
