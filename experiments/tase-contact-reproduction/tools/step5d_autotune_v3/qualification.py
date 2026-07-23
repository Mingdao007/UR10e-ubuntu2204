"""Offline production-path qualification contracts for the Step5d V3 bridge.

This module deliberately does not emulate the bridge.  A qualification may
only consume readiness written by the production bridge process and may only
become production-qualified when the observed process tree contains the real
launcher supervisor, bridge wrapper, and campaign runner.  Until those
processes support endpoint-only substitution, the canonical worker records a
content-bound blocker instead of manufacturing a passing fixture.
"""

from __future__ import annotations

import csv
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence
import uuid

from ur10e_parallel import ResourceProfile, exclusive_lane

from .runtime_environment import (
    DETERMINISTIC_VALUES,
    PASSTHROUGH_KEYS,
    production_runtime_environment,
)
from .runtime_functional_gates import (
    RuntimeFunctionalGateError,
    load_gpu_functional_attestation,
)
from .runtime_installation import (
    load_runtime_contract,
    load_runtime_pointer,
    runtime_binding,
)


CANONICAL_LAUNCH_ENV = "STEP5D_V3_CANONICAL_LAUNCHER"
CONTENT_BINDING_SCHEMA = "step5d.autotune-v3/qualification-content-binding-v1"
QUALIFICATION_RESULT_SCHEMA = "step5d.autotune-v3/qualification-result-v1"
QUALIFICATION_EVIDENCE_SCHEMA = "step5d.autotune-v3/qualification-evidence-ref-v1"
SYNTHETIC_DELIVERY_RECEIPT_SCHEMA = (
    "step5d.autotune-v3/synthetic-delivery-receipt-v1"
)
INTERNAL_SHELL_CONTRACT_SCHEMA = (
    "step5d.autotune-v3/internal-qualification-shell-contract-v1"
)
INTERNAL_SHELL_CONTRACT_ENV = "STEP5D_V3_INTERNAL_QUALIFICATION_SHELL_CONTRACT"
INTERNAL_SHELL_CONTRACT_SHA_ENV = (
    "STEP5D_V3_INTERNAL_QUALIFICATION_SHELL_CONTRACT_SHA256"
)
INTERNAL_SHELL_PID_ENV = "STEP5D_V3_INTERNAL_QUALIFICATION_SHELL_PID"

ENDPOINT_INJECTION_UNAVAILABLE = "ENDPOINT_INJECTION_UNAVAILABLE"
BRIDGE_EXITED_IMMEDIATELY = "BRIDGE_EXITED_IMMEDIATELY"
BRIDGE_READY_MISSING = "BRIDGE_READY_MISSING"
BRIDGE_EXITED_BEFORE_FIRST_ARM_ACK = "BRIDGE_EXITED_BEFORE_FIRST_ARM_ACK"
FIRST_ARM_ACK_MISSING = "FIRST_ARM_ACK_MISSING"
BRIDGE_EXITED_DURING_FIRST_TRIAL = "BRIDGE_EXITED_DURING_FIRST_TRIAL"
TRIAL_COMPLETION_MISSING = "TRIAL_COMPLETION_MISSING"
BRIDGE_EXITED_AFTER_FIRST_TRIAL = "BRIDGE_EXITED_AFTER_FIRST_TRIAL"
NEXT_ARM_ACK_MISSING = "NEXT_ARM_ACK_MISSING"
BRIDGE_NOT_ALIVE_AFTER_NEXT_ACK = "BRIDGE_NOT_ALIVE_AFTER_NEXT_ACK"
PROCESS_TREE_BINDING_INCOMPLETE = "PROCESS_TREE_BINDING_INCOMPLETE"
QUALIFIED = "QUALIFIED"
PRODUCTION_LIFECYCLE_FAILED = "PRODUCTION_LIFECYCLE_FAILED"
QUALIFICATION_ENDPOINT_LEASE_BUSY = "QUALIFICATION_ENDPOINT_LEASE_BUSY"

QUALIFICATION_ENDPOINT_PORTS = {
    "dashboard": 29999,
    "secondary": 30002,
    "rtde": 30004,
    "kunwei": 5152,
}

_SHA256_LENGTH = 64
_PROCESS_ROLE_PATHS = {
    "canonical_launcher": "scripts/step5d-autotune-v3.sh",
    "launcher_supervisor": "tools/run_step5d_autotune_v3_live.py",
    "bridge_wrapper": "tools/run_step5d_autotune_v3_bridge.py",
    "campaign_runner": "tools/run_step5d_autotune_campaign.py",
}
_PROCESS_ROLE_PROFILES = {
    "canonical_launcher": None,
    "launcher_supervisor": "control",
    "bridge_wrapper": "control",
    "campaign_runner": "optimizer",
}
_ENVIRONMENT_KEYS = tuple(
    sorted({*PASSTHROUGH_KEYS, *DETERMINISTIC_VALUES})
)
_WAITING_MARKERS = (
    "V3_QUALIFICATION_SIMULATED_PLAY_BARRIER",
)
_SUCCESS_PHASES = (
    "STARTED",
    "BRIDGE_READY",
    "WAITING_FOR_PLAY",
    "PLAY_OBSERVED",
    "FIRST_ARM_ACK",
    "TRIAL_COMPLETE",
    "NEXT_ARM_ACK",
    "QUALIFIED",
)


class QualificationError(ValueError):
    """Qualification input or observed production state is invalid."""


class QualificationBlocked(QualificationError):
    """A required production seam is absent; this is not a passing result."""


class QualificationPhase(str, Enum):
    STARTED = "STARTED"
    BRIDGE_READY = "BRIDGE_READY"
    WAITING_FOR_PLAY = "WAITING_FOR_PLAY"
    PLAY_OBSERVED = "PLAY_OBSERVED"
    FIRST_ARM_ACK = "FIRST_ARM_ACK"
    TRIAL_COMPLETE = "TRIAL_COMPLETE"
    NEXT_ARM_ACK = "NEXT_ARM_ACK"
    QUALIFIED = "QUALIFIED"


@contextmanager
def qualification_endpoint_lease(
    run_root: Path,
    *,
    environment: Mapping[str, str] | None = None,
    task: str = "step5d-v3-production-qualification",
):
    """Own production-shaped localhost ports with retained process evidence."""

    from step5d_autotune_v3.state import atomic_json

    evidence_path = Path(run_root) / "qualification_endpoint_lease.json"
    values = os.environ if environment is None else environment
    profile = ResourceProfile.from_env(values)
    lease = exclusive_lane(
        profile,
        "step5d-qualification-fixed-endpoints",
        task,
        blocking=False,
    )
    base = {
        "schema": "step5d.bridge/qualification-endpoint-lease-v1",
        "task": task,
        "lane": "step5d-qualification-fixed-endpoints",
        "lock_path": str(lease.path.resolve()),
        "owner_pid": os.getpid(),
        "owner_starttime": read_process_starttime(os.getpid()),
        "run_root": str(Path(run_root).resolve()),
        "ports": dict(QUALIFICATION_ENDPOINT_PORTS),
    }
    try:
        lease.__enter__()
    except BlockingIOError as exc:
        atomic_json(
            evidence_path,
            {
                **base,
                "status": "BLOCKED",
                "reason_code": QUALIFICATION_ENDPOINT_LEASE_BUSY,
                "observed_at_unix_ns": time.time_ns(),
            },
        )
        raise QualificationBlocked(
            f"{QUALIFICATION_ENDPOINT_LEASE_BUSY}: {lease.path} is already owned"
        ) from exc
    acquired_at_unix_ns = time.time_ns()
    atomic_json(
        evidence_path,
        {
            **base,
            "status": "ACTIVE",
            "reason_code": None,
            "acquired_at_unix_ns": acquired_at_unix_ns,
        },
    )
    try:
        yield evidence_path
    finally:
        lease.__exit__(None, None, None)
        atomic_json(
            evidence_path,
            {
                **base,
                "status": "RELEASED",
                "reason_code": None,
                "acquired_at_unix_ns": acquired_at_unix_ns,
                "released_at_unix_ns": time.time_ns(),
            },
        )


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def _sha256_bytes(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha256(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise QualificationError(f"{role} must be a lowercase SHA-256")
    return value


def _load_strict_json_bytes(encoded: bytes, role: str) -> Any:
    def unique(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise QualificationError(f"{role} repeats JSON key {key!r}")
            result[key] = value
        return result

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError(f"{role} contains a non-finite number")
        return parsed

    def reject_constant(value: str) -> None:
        raise ValueError(f"{role} contains forbidden JSON constant {value!r}")

    try:
        return json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique,
            parse_float=finite_float,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise QualificationError(f"{role} is not strict JSON: {exc}") from exc


def require_canonical_launcher(
    experiment_root: Path,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Refuse public/direct execution unless the canonical shell invoked us."""

    root = Path(experiment_root).resolve(strict=True)
    canonical = root / "scripts/step5d-autotune-v3.sh"
    if canonical.is_symlink() or not canonical.is_file() or not os.access(canonical, os.X_OK):
        raise QualificationError("canonical Step5d launcher is not a regular executable")
    values = os.environ if environment is None else environment
    observed = values.get(CANONICAL_LAUNCH_ENV)
    if observed != str(canonical):
        raise QualificationError(
            "internal qualification worker is not a public entrypoint; use "
            f"{canonical} bridge"
        )
    return canonical


def _source_binding(
    root: Path,
    manifest_sha256: str,
    *,
    release_identity: Any | None = None,
) -> dict[str, Any]:
    from .release_identity import ReleaseIdentityError, load_current_release

    current_fingerprint: str | None = None
    immutable_source_root: Path | None = None
    if release_identity is None:
        from .governance import load_current_release_snapshot

        try:
            identity = load_current_release(root)
        except ReleaseIdentityError as exc:
            raise QualificationError(
                f"qualification release source binding is invalid: {exc}"
            ) from exc
        snapshot = load_current_release_snapshot(root)
        if (
            snapshot.valid is not True
            or snapshot.manifest_sha256 != manifest_sha256
            or not isinstance(snapshot.source_fingerprint, str)
        ):
            raise QualificationError(
                "qualification release source binding is invalid: "
                f"{snapshot.error or 'current source fingerprint differs'}"
            )
        current_fingerprint = snapshot.source_fingerprint
    else:
        identity = release_identity
        manifest_path = root / identity.manifest_path
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise QualificationError(
                "qualification candidate manifest is missing or unsafe"
            )
        resolved_manifest = manifest_path.resolve(strict=True)
        try:
            resolved_manifest.relative_to(root)
        except ValueError as exc:
            raise QualificationError(
                "qualification candidate manifest escapes experiment root"
            ) from exc
        immutable_source_root = resolved_manifest.parent
    if identity.manifest_sha256 != manifest_sha256:
        raise QualificationError("qualification release manifest binding differs")
    files: dict[str, str] = {}
    for relative, expected in sorted(identity.source_fingerprints.items()):
        path = root / relative
        if immutable_source_root is not None:
            immutable_path = immutable_source_root / relative
            if immutable_path.is_symlink():
                raise QualificationError(
                    f"qualification candidate source is unsafe: {relative}"
                )
            if immutable_path.exists():
                path = immutable_path
        if path.is_symlink() or not path.is_file():
            raise QualificationError(f"qualification source is missing: {relative}")
        files[relative] = _sha256_file(path)
        if files[relative] != expected:
            raise QualificationError(f"qualification source fingerprint differs: {relative}")
    files_fingerprint = _sha256_bytes(_canonical_bytes(files))
    actual_sources = {
        f"experiment:{relative}": digest for relative, digest in files.items()
    }
    if current_fingerprint is None:
        repository_root = root
        for _ in range(identity.verification["repository_source_root_depth"]):
            repository_root = repository_root.parent
        repository_root = repository_root.resolve(strict=True)
        for relative, expected in sorted(
            identity.verification["repository_source_fingerprints"].items()
        ):
            path = repository_root / relative
            if path.is_symlink() or not path.is_file():
                raise QualificationError(
                    f"qualification repository source is missing: {relative}"
                )
            observed = _sha256_file(path)
            if observed != expected:
                raise QualificationError(
                    f"qualification repository source fingerprint differs: {relative}"
                )
            actual_sources[f"repository:{relative}"] = observed
    return {
        "files": files,
        "files_fingerprint": files_fingerprint,
        "fingerprint": current_fingerprint
        or _sha256_bytes(_canonical_bytes(actual_sources)),
    }


def _environment_binding(environment: Mapping[str, str]) -> dict[str, Any]:
    values = {key: environment.get(key, "") for key in _ENVIRONMENT_KEYS}
    pointer = load_runtime_pointer(environ=environment)
    contract = load_runtime_contract()
    control = pointer["profiles"]["control"]
    _gpu_payload, gpu_reference = load_gpu_functional_attestation(
        runtime_pointer=pointer
    )
    values.update(
        {
            "python_executable": control["python_executable"],
            "python_prefix": control["root"],
            "runtime_binding": runtime_binding(
                environ=environment,
                runtime_pointer=pointer,
            ),
            "gpu_functional_evidence": gpu_reference,
            "python_version": contract["python"]["version"],
        }
    )
    return {
        "values": values,
        "fingerprint": _sha256_bytes(_canonical_bytes(values)),
    }


def read_process_starttime(pid: int) -> int | None:
    """Return Linux /proc starttime, distinguishing dead processes and PID reuse."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        suffix = stat.rsplit(")", 1)[1].split()
        return int(suffix[19])
    except (OSError, UnicodeError, IndexError, ValueError):
        return None


def _runtime_profile_row(
    runtime_process_binding: Mapping[str, Any], role: str
) -> tuple[str | None, Mapping[str, Any] | None]:
    profile = _PROCESS_ROLE_PROFILES.get(role)
    if role not in _PROCESS_ROLE_PROFILES:
        raise QualificationError(f"unknown production process role: {role}")
    if profile is None:
        return None, None
    if (
        not isinstance(runtime_process_binding, Mapping)
        or runtime_process_binding.get("schema")
        != "step5d.autotune-v3/runtime-process-binding-v1"
        or not isinstance(runtime_process_binding.get("profiles"), Mapping)
    ):
        raise QualificationError("runtime process binding is invalid")
    row = runtime_process_binding["profiles"].get(profile)
    if not isinstance(row, Mapping):
        raise QualificationError(f"{profile} runtime profile binding is missing")
    environment_id = row.get("environment_id")
    python_executable = row.get("python_executable")
    _require_sha256(environment_id, f"{profile} runtime environment ID")
    if (
        not isinstance(python_executable, str)
        or not Path(python_executable).is_absolute()
    ):
        raise QualificationError(f"{profile} runtime interpreter path is invalid")
    return profile, row


def _read_process_runtime_environment(proc: Path, role: str) -> dict[str, str]:
    required = {
        "STEP5D_V3_CONTROL_ENVIRONMENT_ID",
        "STEP5D_V3_OPTIMIZER_ENVIRONMENT_ID",
        "STEP5D_V3_RUNTIME_PROFILE",
    }
    try:
        encoded = (proc / "environ").read_bytes()
    except OSError as exc:
        raise QualificationError(f"cannot inspect {role} process environment: {exc}") from exc
    values: dict[str, str] = {}
    for entry in encoded.split(b"\0"):
        raw_name, separator, raw_value = entry.partition(b"=")
        if not separator:
            continue
        name = raw_name.decode("utf-8", errors="surrogateescape")
        if name not in required:
            continue
        if name in values:
            raise QualificationError(
                f"{role} process environment repeats runtime binding {name}"
            )
        values[name] = raw_value.decode("utf-8", errors="surrogateescape")
    return values


def _capture_process(
    pid: int,
    role: str,
    expected_script: Path,
    runtime_process_binding: Mapping[str, Any],
) -> dict[str, Any]:
    starttime = read_process_starttime(pid)
    if starttime is None:
        raise QualificationError(f"{role} process is not alive")
    proc = Path(f"/proc/{pid}")
    try:
        executable = str((proc / "exe").resolve(strict=True))
        argv = tuple(
            value.decode("utf-8", errors="surrogateescape")
            for value in (proc / "cmdline").read_bytes().split(b"\0")
            if value
        )
        status_lines = (proc / "status").read_text(encoding="ascii").splitlines()
        ppid = int(next(line for line in status_lines if line.startswith("PPid:")).split()[1])
    except (OSError, UnicodeError, StopIteration, ValueError) as exc:
        raise QualificationError(f"cannot inspect {role} process: {exc}") from exc
    expected = str(expected_script.resolve(strict=True))
    resolved_argv = {
        str(Path(argument).resolve())
        for argument in argv
        if argument.startswith("/")
    }
    if expected not in argv and expected not in resolved_argv:
        raise QualificationError(
            f"{role} process does not execute production script {expected}"
        )
    if not argv:
        raise QualificationError(f"{role} process argv is empty")
    profile, profile_row = _runtime_profile_row(runtime_process_binding, role)
    environment_id: str | None = None
    if profile is not None:
        assert profile_row is not None
        expected_python = profile_row["python_executable"]
        if argv[0] != expected_python:
            raise QualificationError(
                f"{role} raw argv0 differs from the {profile} runtime interpreter"
            )
        runtime_environment = _read_process_runtime_environment(proc, role)
        if runtime_environment.get("STEP5D_V3_RUNTIME_PROFILE") != profile:
            raise QualificationError(
                f"{role} process runtime profile differs from {profile}"
            )
        environment_key = (
            "STEP5D_V3_CONTROL_ENVIRONMENT_ID"
            if profile == "control"
            else "STEP5D_V3_OPTIMIZER_ENVIRONMENT_ID"
        )
        environment_id = runtime_environment.get(environment_key)
        if environment_id != profile_row["environment_id"]:
            raise QualificationError(
                f"{role} process environment ID differs from the {profile} runtime"
            )
    return {
        "role": role,
        "pid": pid,
        "ppid": ppid,
        "starttime": starttime,
        "executable": executable,
        "argv": list(argv),
        "argv0": argv[0],
        "argv_sha256": _sha256_bytes(_canonical_bytes(list(argv))),
        "runtime_profile": profile,
        "environment_id": environment_id,
        "script": expected,
        "script_sha256": _sha256_file(Path(expected)),
    }


def _process_tree_shape(processes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    roles = {
        str(process["role"]): {
            "script": str(process["script"]),
            "script_sha256": str(process["script_sha256"]),
            "runtime_profile": process["runtime_profile"],
        }
        for process in processes
    }
    topology = {"launcher_supervisor": ["bridge_wrapper", "campaign_runner"]}
    if "canonical_launcher" in roles:
        topology = {
            "canonical_launcher": ["launcher_supervisor"],
            **topology,
        }
    return {
        "schema": "step5d.autotune-v3/production-process-tree-shape-v1",
        "topology": topology,
        "roles": {role: roles[role] for role in sorted(roles)},
    }


def _process_tree_shape_fingerprint(processes: Sequence[Mapping[str, Any]]) -> str:
    return _sha256_bytes(_canonical_bytes(_process_tree_shape(processes)))


def production_process_tree_fingerprint(experiment_root: Path) -> str:
    root = Path(experiment_root).resolve(strict=True)
    processes = [
        {
            "role": role,
            "script": str((root / relative).resolve(strict=True)),
            "script_sha256": _sha256_file(root / relative),
            "runtime_profile": _PROCESS_ROLE_PROFILES[role],
        }
        for role, relative in sorted(_PROCESS_ROLE_PATHS.items())
    ]
    return _process_tree_shape_fingerprint(processes)


def qualification_process_tree_fingerprint(experiment_root: Path) -> str:
    return production_process_tree_fingerprint(experiment_root)


def production_process_role_paths() -> Mapping[str, str]:
    return dict(_PROCESS_ROLE_PATHS)


def capture_content_binding(
    experiment_root: Path,
    *,
    manifest_sha256: str,
    process_pids: Mapping[str, int] | None = None,
    environment: Mapping[str, str] | None = None,
    release_identity: Any | None = None,
) -> dict[str, Any]:
    """Capture immutable source/environment/launcher and optional live process bytes."""

    root = Path(experiment_root).resolve(strict=True)
    manifest = _require_sha256(manifest_sha256, "release manifest")
    values = os.environ if environment is None else environment
    launcher = root / "scripts/step5d-autotune-v3.sh"
    if launcher.is_symlink() or not launcher.is_file():
        raise QualificationError("canonical launcher is missing")
    environment_binding = _environment_binding(values)
    runtime_process_binding = environment_binding["values"]["runtime_binding"]
    processes: list[dict[str, Any]] = []
    complete = process_pids is not None
    if process_pids is not None:
        observed_roles = set(process_pids)
        if observed_roles == set(_PROCESS_ROLE_PATHS):
            role_paths = _PROCESS_ROLE_PATHS
        else:
            raise QualificationError(
                "production process binding requires canonical_launcher, "
                "launcher_supervisor, bridge_wrapper, and campaign_runner"
            )
        for role in sorted(role_paths):
            processes.append(
                _capture_process(
                    process_pids[role],
                    role,
                    root / role_paths[role],
                    runtime_process_binding,
                )
            )
        process_by_role = {process["role"]: process for process in processes}
        supervisor_pid = process_by_role["launcher_supervisor"]["pid"]
        canonical_parent_invalid = (
            "canonical_launcher" in process_by_role
            and process_by_role["launcher_supervisor"]["ppid"]
            != process_by_role["canonical_launcher"]["pid"]
        )
        child_parent_invalid = any(
            process_by_role[role]["ppid"] != supervisor_pid
            for role in ("bridge_wrapper", "campaign_runner")
        )
        if canonical_parent_invalid or child_parent_invalid:
            raise QualificationError(
                "production process topology must be canonical shell -> live "
                "supervisor -> bridge wrapper and campaign runner"
            )
    process_tree = {
        "complete": complete,
        "processes": processes,
        "fingerprint": _process_tree_shape_fingerprint(processes),
    }
    binding = {
        "schema": CONTENT_BINDING_SCHEMA,
        "manifest_sha256": manifest,
        "source": _source_binding(
            root,
            manifest,
            release_identity=release_identity,
        ),
        "environment": environment_binding,
        "launcher": {
            "path": str(launcher),
            "sha256": _sha256_file(launcher),
        },
        "process_tree": process_tree,
        "endpoint_substitution": {
            "roles": ["dashboard", "kunwei", "rtde", "tp"],
            "endpoint_only": True,
            "motion_capable": False,
            "production_processes_retained": complete,
            "ready_writer": "production_bridge" if complete else None,
        },
    }
    validate_content_binding(binding)
    return binding


def validate_content_binding(binding: Mapping[str, Any]) -> None:
    if not isinstance(binding, Mapping) or set(binding) != {
        "schema",
        "manifest_sha256",
        "source",
        "environment",
        "launcher",
        "process_tree",
        "endpoint_substitution",
    }:
        raise QualificationError("qualification content binding fields are invalid")
    if binding["schema"] != CONTENT_BINDING_SCHEMA:
        raise QualificationError("qualification content binding schema is invalid")
    _require_sha256(binding["manifest_sha256"], "release manifest")

    source = binding["source"]
    if not isinstance(source, Mapping) or set(source) != {
        "files",
        "files_fingerprint",
        "fingerprint",
    }:
        raise QualificationError("source binding fields are invalid")
    files = source["files"]
    if not isinstance(files, Mapping) or not files:
        raise QualificationError("source binding must contain source files")
    normalized_files: dict[str, str] = {}
    for name, digest in files.items():
        if not isinstance(name, str) or not name or name.startswith("/") or ".." in Path(name).parts:
            raise QualificationError("source binding contains an unsafe path")
        normalized_files[name] = _require_sha256(digest, f"source {name}")
    if source["files_fingerprint"] != _sha256_bytes(
        _canonical_bytes(normalized_files)
    ):
        raise QualificationError("source-file fingerprint is not derived")
    _require_sha256(source["fingerprint"], "release source fingerprint")

    environment = binding["environment"]
    if not isinstance(environment, Mapping) or set(environment) != {"values", "fingerprint"}:
        raise QualificationError("environment binding fields are invalid")
    if not isinstance(environment["values"], Mapping):
        raise QualificationError("environment values are invalid")
    if environment["fingerprint"] != _sha256_bytes(
        _canonical_bytes(dict(environment["values"]))
    ):
        raise QualificationError("environment fingerprint is not derived")

    launcher = binding["launcher"]
    if not isinstance(launcher, Mapping) or set(launcher) != {"path", "sha256"}:
        raise QualificationError("launcher binding fields are invalid")
    if not isinstance(launcher["path"], str) or not Path(launcher["path"]).is_absolute():
        raise QualificationError("launcher path must be absolute")
    _require_sha256(launcher["sha256"], "launcher")

    process_tree = binding["process_tree"]
    if not isinstance(process_tree, Mapping) or set(process_tree) != {
        "complete",
        "processes",
        "fingerprint",
    }:
        raise QualificationError("process-tree binding fields are invalid")
    if not isinstance(process_tree["complete"], bool) or not isinstance(
        process_tree["processes"], list
    ):
        raise QualificationError("process-tree values are invalid")
    if process_tree["fingerprint"] != _process_tree_shape_fingerprint(
        process_tree["processes"]
    ):
        raise QualificationError("process-tree fingerprint is not derived")
    roles = {
        process.get("role")
        for process in process_tree["processes"]
        if isinstance(process, Mapping)
    }
    if process_tree["complete"] and roles != set(_PROCESS_ROLE_PATHS):
        raise QualificationError("complete process tree lacks a production role")
    if not process_tree["complete"] and process_tree["processes"]:
        raise QualificationError("incomplete process tree must not imply observations")
    process_by_role: dict[str, Mapping[str, Any]] = {}
    runtime_process_binding = environment["values"].get("runtime_binding")
    for process in process_tree["processes"]:
        if not isinstance(process, Mapping) or set(process) != {
            "role",
            "pid",
            "ppid",
            "starttime",
            "executable",
            "argv",
            "argv0",
            "argv_sha256",
            "runtime_profile",
            "environment_id",
            "script",
            "script_sha256",
        }:
            raise QualificationError("process-tree observation fields are invalid")
        role = process["role"]
        if role not in _PROCESS_ROLE_PATHS or role in process_by_role:
            raise QualificationError("process-tree role is invalid or repeated")
        for name in ("pid", "ppid", "starttime"):
            if isinstance(process[name], bool) or not isinstance(process[name], int) or process[name] < 0:
                raise QualificationError(f"process-tree {name} is invalid")
        if process["pid"] <= 0 or process["starttime"] <= 0:
            raise QualificationError("process-tree PID/starttime must be positive")
        if not isinstance(process["argv"], list) or not all(
            isinstance(argument, str) for argument in process["argv"]
        ):
            raise QualificationError("process-tree argv is invalid")
        if (
            not process["argv"]
            or not isinstance(process["argv0"], str)
            or process["argv0"] != process["argv"][0]
        ):
            raise QualificationError("process-tree raw argv0 is invalid")
        if process["argv_sha256"] != _sha256_bytes(_canonical_bytes(process["argv"])):
            raise QualificationError("process-tree argv fingerprint is not derived")
        for name in ("executable", "script"):
            if not isinstance(process[name], str) or not Path(process[name]).is_absolute():
                raise QualificationError(f"process-tree {name} path must be absolute")
        _require_sha256(process["script_sha256"], "process script")
        expected_profile, profile_row = _runtime_profile_row(
            runtime_process_binding, role
        )
        if process["runtime_profile"] != expected_profile:
            raise QualificationError(
                f"process-tree {role} runtime profile binding differs"
            )
        if expected_profile is None:
            if process["environment_id"] is not None:
                raise QualificationError(
                    "canonical launcher must not claim a Python runtime environment"
                )
        else:
            assert profile_row is not None
            if process["argv0"] != profile_row["python_executable"]:
                raise QualificationError(
                    f"process-tree {role} raw argv0 differs from the "
                    f"{expected_profile} runtime interpreter"
                )
            if process["environment_id"] != profile_row["environment_id"]:
                raise QualificationError(
                    f"process-tree {role} environment ID differs from the "
                    f"{expected_profile} runtime"
                )
        process_by_role[role] = process
    if process_tree["complete"]:
        supervisor_pid = process_by_role["launcher_supervisor"]["pid"]
        canonical_parent_invalid = (
            "canonical_launcher" in process_by_role
            and process_by_role["launcher_supervisor"]["ppid"]
            != process_by_role["canonical_launcher"]["pid"]
        )
        child_parent_invalid = any(
            process_by_role[role]["ppid"] != supervisor_pid
            for role in ("bridge_wrapper", "campaign_runner")
        )
        if canonical_parent_invalid or child_parent_invalid:
            raise QualificationError("process-tree parentage is not the production topology")

    endpoints = binding["endpoint_substitution"]
    if not isinstance(endpoints, Mapping) or set(endpoints) != {
        "roles",
        "endpoint_only",
        "motion_capable",
        "production_processes_retained",
        "ready_writer",
    }:
        raise QualificationError("endpoint substitution binding fields are invalid")
    if endpoints["roles"] != ["dashboard", "kunwei", "rtde", "tp"]:
        raise QualificationError("qualification must substitute exactly four endpoints")
    if endpoints["endpoint_only"] is not True or endpoints["motion_capable"] is not False:
        raise QualificationError("qualification endpoint substitution is unsafe")
    if endpoints["production_processes_retained"] is not process_tree["complete"]:
        raise QualificationError("endpoint/process-tree binding is inconsistent")
    expected_writer = "production_bridge" if process_tree["complete"] else None
    if endpoints["ready_writer"] != expected_writer:
        raise QualificationError("only the production bridge may write readiness")


def _positive_integer(value: Any, role: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise QualificationError(f"{role} must be a positive integer")
    return value


def _read_reference_file(
    reference: Any,
    role: str,
) -> tuple[Path, bytes]:
    if not isinstance(reference, Mapping) or set(reference) != {"path", "sha256"}:
        raise QualificationError(f"{role} reference fields are invalid")
    path_value = reference["path"]
    if not isinstance(path_value, str):
        raise QualificationError(f"{role} reference path is invalid")
    path = Path(path_value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise QualificationError(f"{role} reference is not an absolute regular file")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise QualificationError(f"{role} reference path is not canonical")
    encoded = path.read_bytes()
    expected_sha256 = _require_sha256(reference["sha256"], f"{role} reference")
    if _sha256_bytes(encoded) != expected_sha256:
        raise QualificationError(f"{role} reference SHA-256 differs")
    return path, encoded


def _read_json_reference(reference: Any, role: str) -> tuple[Path, Mapping[str, Any]]:
    path, encoded = _read_reference_file(reference, role)
    payload = _load_strict_json_bytes(encoded, role)
    if not isinstance(payload, Mapping):
        raise QualificationError(f"{role} must contain a JSON object")
    return path, payload


def _validate_runner_ready(
    payload: Any,
    *,
    expected_pid: int | None,
    expected_bridge_run: Path,
) -> None:
    required_fields = {
        "schema_version",
        "ok",
        "pid",
        "bridge_run",
        "campaign_root",
        "campaign_epoch",
        "campaign_fingerprint",
        "selection_policy",
        "state",
        "durable_state_ready",
    }
    if not isinstance(payload, Mapping) or set(payload) != required_fields:
        raise QualificationError("campaign-runner waiting sentinel fields differ")
    if (
        payload["schema_version"] != "step5d_autotune_runner_ready_v1"
        or payload["ok"] is not True
        or payload["state"] != "ready_home"
        or payload["durable_state_ready"] is not True
        or payload["selection_policy"] != "codex_batches"
    ):
        raise QualificationError("campaign runner did not reach the production waiting state")
    runner_pid = _positive_integer(payload["pid"], "campaign-runner PID")
    if expected_pid is not None and runner_pid != expected_pid:
        raise QualificationError("campaign-runner waiting sentinel PID differs")
    if payload["bridge_run"] != str(expected_bridge_run):
        raise QualificationError("campaign-runner waiting sentinel bridge path differs")
    campaign_root = payload["campaign_root"]
    if not isinstance(campaign_root, str) or not Path(campaign_root).is_absolute():
        raise QualificationError("campaign-runner waiting sentinel campaign path is invalid")
    _positive_integer(payload["campaign_epoch"], "campaign epoch")
    _require_sha256(payload["campaign_fingerprint"], "campaign fingerprint")


def _validate_endpoint_event(
    event: Any,
    *,
    expected_event: str,
    expected_arm_count: int | None = None,
) -> Mapping[str, Any]:
    fields = {"event", "monotonic_s", "command_seq", "trial_id"}
    if expected_arm_count is not None:
        fields.add("arm_count")
    if not isinstance(event, Mapping) or set(event) != fields:
        raise QualificationError(f"endpoint {expected_event} event fields differ")
    if event["event"] != expected_event:
        raise QualificationError(f"endpoint {expected_event} event name differs")
    monotonic_s = event["monotonic_s"]
    if (
        isinstance(monotonic_s, bool)
        or not isinstance(monotonic_s, (int, float))
        or not math.isfinite(float(monotonic_s))
        or float(monotonic_s) <= 0.0
    ):
        raise QualificationError(f"endpoint {expected_event} event time is invalid")
    _positive_integer(event["command_seq"], f"endpoint {expected_event} command sequence")
    _positive_integer(event["trial_id"], f"endpoint {expected_event} trial ID")
    if expected_arm_count is not None and event["arm_count"] != expected_arm_count:
        raise QualificationError(f"endpoint {expected_event} ARM count differs")
    return event


def _validate_trial_bundle(
    path: Path,
    *,
    expected_trial_id: int,
    expected_command_seq: int,
) -> Mapping[str, Any]:
    encoded = path.read_bytes()
    bundle = _load_strict_json_bytes(encoded, "immutable trial bundle")
    required = {
        "schema_version",
        "trial",
        "capture",
        "evaluation",
        "physical_capture_uid",
        "history_identity",
        "artifact_provenance",
    }
    if (
        not isinstance(bundle, Mapping)
        or set(bundle) != required
        or bundle["schema_version"] != "step5d.autotune.immutable-bundle/v1"
    ):
        raise QualificationError("immutable trial bundle fields or schema differ")
    trial = bundle["trial"]
    if (
        not isinstance(trial, Mapping)
        or trial.get("trial_id") != expected_trial_id
        or trial.get("command_seq") != expected_command_seq
    ):
        raise QualificationError("immutable trial bundle ARM identity differs")
    _require_sha256(trial.get("trial_uid"), "immutable trial UID")
    _require_sha256(bundle["physical_capture_uid"], "physical capture UID")
    _require_sha256(bundle["history_identity"], "history identity")
    capture = bundle["capture"]
    if (
        not isinstance(capture, Mapping)
        or capture.get("trial_uid") != trial["trial_uid"]
        or capture.get("completion_marker") is not True
        or capture.get("returned_safe") is not True
    ):
        raise QualificationError("immutable trial bundle lacks safe completion evidence")
    if not isinstance(bundle["evaluation"], Mapping):
        raise QualificationError("immutable trial bundle evaluation is invalid")
    provenance = bundle["artifact_provenance"]
    if not isinstance(provenance, Mapping) or set(provenance) != {
        "csv",
        "metadata",
        "terminal_manifest",
    }:
        raise QualificationError("immutable trial bundle artifact provenance differs")
    seen_files: set[tuple[int, int]] = set()
    for artifact_role, reference in provenance.items():
        if not isinstance(reference, Mapping) or set(reference) != {
            "path",
            "sha256",
            "size_bytes",
        }:
            raise QualificationError(
                f"immutable trial bundle {artifact_role} reference fields differ"
            )
        artifact_path = Path(str(reference["path"]))
        if (
            not artifact_path.is_absolute()
            or artifact_path.is_symlink()
            or not artifact_path.is_file()
            or artifact_path.resolve(strict=True) != artifact_path
        ):
            raise QualificationError(
                f"immutable trial bundle {artifact_role} artifact is unsafe"
            )
        stat = artifact_path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if identity in seen_files:
            raise QualificationError("immutable trial bundle artifact roles share a file")
        seen_files.add(identity)
        if (
            isinstance(reference["size_bytes"], bool)
            or not isinstance(reference["size_bytes"], int)
            or stat.st_size != reference["size_bytes"]
            or _sha256_file(artifact_path)
            != _require_sha256(reference["sha256"], f"{artifact_role} artifact")
        ):
            raise QualificationError(
                f"immutable trial bundle {artifact_role} artifact bytes differ"
            )
        capture_digest_field = {
            "csv": "csv_sha256",
            "metadata": "metadata_sha256",
            "terminal_manifest": "terminal_manifest_sha256",
        }[artifact_role]
        if capture.get(capture_digest_field) != reference["sha256"]:
            raise QualificationError(
                f"immutable trial bundle {artifact_role} capture digest differs"
            )
    return bundle


def validate_qualification_binding(
    payload: Mapping[str, Any],
    *,
    experiment_root: Path,
    manifest_sha256: str,
    source_fingerprint: str,
    launcher_sha256: str,
    release_identity: Any | None = None,
) -> Mapping[str, Any]:
    required_fields = {
        "schema",
        "ok",
        "lifecycle_complete",
        "state",
        "reason_code",
        "binding",
        "canonical_shell_result",
        "bridge",
        "waiting_barrier",
        "play_row",
        "play_row_sha256",
        "first_arm_seq",
        "trial_evidence_ref",
        "next_arm_seq",
        "events",
        "remaining_integration_seam",
        "started_at_unix_ns",
        "completed_at_unix_ns",
        "endpoint_evidence",
        "preflight_evidence",
        "process_log",
        "bridge_csv",
        "live_result",
    }
    if (
        not isinstance(payload, Mapping)
        or set(payload) != required_fields
        or payload.get("schema") != QUALIFICATION_RESULT_SCHEMA
        or payload.get("ok") is not True
        or payload.get("lifecycle_complete") is not True
        or payload.get("state") != QualificationPhase.QUALIFIED.value
        or payload.get("reason_code") != QUALIFIED
        or payload.get("remaining_integration_seam") is not None
    ):
        raise QualificationError("qualification did not complete the production path")
    started_at = _positive_integer(
        payload["started_at_unix_ns"], "qualification start timestamp"
    )
    completed_at = _positive_integer(
        payload["completed_at_unix_ns"], "qualification completion timestamp"
    )
    if completed_at < started_at:
        raise QualificationError("qualification completion precedes its start")
    binding = payload.get("binding")
    validate_content_binding(binding)
    process_tree = binding["process_tree"]
    if (
        process_tree["complete"] is not True
        or process_tree["fingerprint"]
        != qualification_process_tree_fingerprint(experiment_root)
    ):
        raise QualificationError("qualification process topology differs")
    expected = {
        "manifest_sha256": _require_sha256(manifest_sha256, "release manifest"),
        "source_fingerprint": _require_sha256(
            source_fingerprint, "release source fingerprint"
        ),
        "launcher_sha256": _require_sha256(
            launcher_sha256, "canonical launcher"
        ),
    }
    observed = {
        "manifest_sha256": binding["manifest_sha256"],
        "source_fingerprint": binding["source"]["fingerprint"],
        "launcher_sha256": binding["launcher"]["sha256"],
    }
    for name, expected_value in expected.items():
        if observed[name] != expected_value:
            raise QualificationError(f"qualification {name} differs")
    return binding


def validate_qualification_result(
    payload: Mapping[str, Any],
    *,
    experiment_root: Path,
    manifest_sha256: str,
    source_fingerprint: str,
    launcher_sha256: str,
    release_identity: Any | None = None,
) -> Mapping[str, Any]:
    binding = validate_qualification_binding(
        payload,
        experiment_root=experiment_root,
        manifest_sha256=manifest_sha256,
        source_fingerprint=source_fingerprint,
        launcher_sha256=launcher_sha256,
        release_identity=release_identity,
    )
    started_at = _positive_integer(
        payload["started_at_unix_ns"], "qualification start timestamp"
    )
    completed_at = _positive_integer(
        payload["completed_at_unix_ns"], "qualification completion timestamp"
    )
    process_tree = binding["process_tree"]

    process_by_role = {
        process["role"]: process for process in process_tree["processes"]
    }
    shell_result = payload["canonical_shell_result"]
    shell_process = process_by_role["canonical_launcher"]
    if not isinstance(shell_result, Mapping) or set(shell_result) != {
        "pid",
        "starttime",
        "argv_sha256",
        "returncode",
        "contract_ref",
    }:
        raise QualificationError("canonical qualification shell result fields differ")
    if (
        shell_result["pid"] != shell_process["pid"]
        or shell_result["starttime"] != shell_process["starttime"]
        or shell_result["argv_sha256"] != shell_process["argv_sha256"]
        or shell_result["returncode"] != 0
    ):
        raise QualificationError("canonical qualification shell did not exit successfully")
    _, contract_payload = _read_json_reference(
        shell_result["contract_ref"], "internal qualification shell contract"
    )
    contract = _validate_internal_shell_contract(experiment_root, contract_payload)
    requested_argv = contract["requested_argv"]
    if shell_process["argv"][-len(requested_argv) :] != requested_argv:
        raise QualificationError("canonical qualification shell argv differs")
    if (
        contract["canonical_launcher"]["sha256"]
        != binding["launcher"]["sha256"]
        or contract["release_manifest"]["sha256"] != binding["manifest_sha256"]
    ):
        raise QualificationError("canonical qualification shell content binding differs")
    bridge = payload["bridge"]
    if not isinstance(bridge, Mapping) or set(bridge) != {
        "pid",
        "starttime",
        "launch_nonce",
        "alive",
        "alive_at_campaign_outcome",
        "ready_ref",
    }:
        raise QualificationError("qualification bridge observation fields differ")
    bridge_pid = _positive_integer(bridge["pid"], "qualification bridge PID")
    bridge_starttime = _positive_integer(
        bridge["starttime"], "qualification bridge starttime"
    )
    if (
        bridge_pid != process_by_role["bridge_wrapper"]["pid"]
        or bridge_starttime != process_by_role["bridge_wrapper"]["starttime"]
        or not isinstance(bridge["launch_nonce"], str)
        or not bridge["launch_nonce"]
        or not isinstance(bridge["alive"], bool)
        or bridge["alive_at_campaign_outcome"] is not True
    ):
        raise QualificationError("qualification bridge process identity differs")
    ready_path, ready = _read_json_reference(bridge["ready_ref"], "bridge readiness")
    if (
        ready.get("ready_schema") != "step5d_bridge_ready_v2"
        or ready.get("ok") is not True
        or ready.get("pid") != bridge_pid
        or ready.get("launch_nonce") != bridge["launch_nonce"]
        or ready.get("prewarm_status") != "ok"
        or ready.get("v30_runtime_complete") is not True
        or ready.get("rtde_connected") is not True
        or ready.get("rtde_send_succeeded") is not True
        or ready.get("sensor_stream_ready") is not True
    ):
        raise QualificationError("qualification bridge readiness predicates differ")

    waiting = payload["waiting_barrier"]
    if not isinstance(waiting, Mapping) or set(waiting) != {
        "runner_ready_ref",
        "marker_sequence",
        "marker_sequence_sha256",
    }:
        raise QualificationError("qualification waiting barrier fields differ")
    if waiting["marker_sequence"] != list(_WAITING_MARKERS) or waiting[
        "marker_sequence_sha256"
    ] != _sha256_bytes(_canonical_bytes(list(_WAITING_MARKERS))):
        raise QualificationError("qualification waiting marker sequence differs")
    _, runner_ready = _read_json_reference(
        waiting["runner_ready_ref"], "campaign-runner readiness"
    )
    _validate_runner_ready(
        runner_ready,
        expected_pid=process_by_role["campaign_runner"]["pid"],
        expected_bridge_run=ready_path.parent,
    )
    _, process_log_bytes = _read_reference_file(payload["process_log"], "process log")
    marker_positions = [
        process_log_bytes.find(marker.encode("ascii")) for marker in _WAITING_MARKERS
    ]
    if marker_positions[0] < 0 or marker_positions != sorted(marker_positions):
        raise QualificationError("process log lacks the ordered waiting barrier")

    play_row = payload["play_row"]
    if not isinstance(play_row, Mapping):
        raise QualificationError("qualification Play observation is not a CSV row")
    try:
        play_is_valid = (
            int(float(play_row["ur_runtime_state"])) == 2
            and int(float(play_row["ur_safety_mode"])) == 1
            and int(float(play_row["rtde_connected"])) == 1
        )
    except (KeyError, TypeError, ValueError):
        play_is_valid = False
    if not play_is_valid:
        raise QualificationError("qualification Play observation predicates differ")
    play_sha256 = _require_sha256(payload["play_row_sha256"], "Play observation")
    if play_sha256 != _sha256_bytes(_canonical_bytes(dict(play_row))):
        raise QualificationError("qualification Play observation SHA-256 is not derived")
    _, bridge_csv_bytes = _read_reference_file(payload["bridge_csv"], "bridge CSV")
    try:
        csv_rows = list(
            csv.DictReader(bridge_csv_bytes.decode("utf-8").splitlines())
        )
    except UnicodeDecodeError as exc:
        raise QualificationError("bridge CSV is not UTF-8") from exc
    if not any(
        _sha256_bytes(_canonical_bytes(dict(row))) == play_sha256 for row in csv_rows
    ):
        raise QualificationError("bridge CSV does not contain the bound Play observation")

    events = payload["events"]
    if not isinstance(events, list) or len(events) != len(_SUCCESS_PHASES):
        raise QualificationError("qualification lifecycle event count differs")
    observed_times: list[int] = []
    details: list[Mapping[str, Any]] = []
    for event, expected_phase in zip(events, _SUCCESS_PHASES, strict=True):
        if not isinstance(event, Mapping) or set(event) != {
            "phase",
            "observed_at_ns",
            "details",
        }:
            raise QualificationError("qualification lifecycle event fields differ")
        if event["phase"] != expected_phase or not isinstance(event["details"], Mapping):
            raise QualificationError("qualification lifecycle event order differs")
        observed_times.append(
            _positive_integer(event["observed_at_ns"], f"{expected_phase} timestamp")
        )
        details.append(event["details"])
    if (
        observed_times != sorted(observed_times)
        or observed_times[0] < started_at
        or observed_times[-1] > completed_at
    ):
        raise QualificationError("qualification lifecycle timestamps are not ordered")
    if details[0] != {"bridge_pid": bridge_pid} or details[1] != bridge["ready_ref"]:
        raise QualificationError("qualification startup lifecycle evidence differs")
    if details[2] != waiting or details[3] != {"row_sha256": play_sha256}:
        raise QualificationError("qualification waiting/Play lifecycle evidence differs")

    first_arm_seq = _positive_integer(payload["first_arm_seq"], "first ARM sequence")
    next_arm_seq = _positive_integer(payload["next_arm_seq"], "next ARM sequence")
    if next_arm_seq <= first_arm_seq:
        raise QualificationError("qualification next ARM sequence did not increase")
    for arm_details, sequence, count in (
        (details[4], first_arm_seq, 1),
        (details[6], next_arm_seq, 2),
    ):
        if not isinstance(arm_details, Mapping) or set(arm_details) != {
            "command_seq",
            "trial_id",
            "arm_count",
            "endpoint_event_sha256",
        }:
            raise QualificationError("qualification ARM event details differ")
        if arm_details["command_seq"] != sequence or arm_details["arm_count"] != count:
            raise QualificationError("qualification ARM event identity differs")
        _positive_integer(arm_details["trial_id"], "qualification ARM trial ID")
        _require_sha256(arm_details["endpoint_event_sha256"], "endpoint ARM event")

    trial_ref = payload["trial_evidence_ref"]
    if not isinstance(trial_ref, Mapping) or set(trial_ref) != {
        "path",
        "sha256",
        "trial_id",
        "command_seq",
        "endpoint_event_sha256",
    }:
        raise QualificationError("qualification trial evidence reference fields differ")
    if (
        trial_ref["trial_id"] != details[4]["trial_id"]
        or trial_ref["command_seq"] != first_arm_seq
        or details[5] != trial_ref
    ):
        raise QualificationError("qualification trial lifecycle identity differs")
    trial_path, _ = _read_reference_file(
        {"path": trial_ref["path"], "sha256": trial_ref["sha256"]},
        "immutable trial bundle",
    )
    _require_sha256(trial_ref["endpoint_event_sha256"], "endpoint trial event")
    _validate_trial_bundle(
        trial_path,
        expected_trial_id=trial_ref["trial_id"],
        expected_command_seq=trial_ref["command_seq"],
    )

    _, endpoint = _read_json_reference(
        payload["endpoint_evidence"], "qualification endpoint evidence"
    )
    if (
        endpoint.get("schema")
        != "step5d.autotune-v3/qualification-endpoint-evidence-v1"
        or endpoint.get("alive") is not True
        or endpoint.get("errors") != []
    ):
        raise QualificationError("qualification endpoint evidence is not successful")
    _, endpoint_config = _read_json_reference(
        contract["endpoint_config"], "qualification endpoint config"
    )
    if (
        endpoint_config.get("schema")
        != "step5d.autotune-v3/qualification-endpoint-config-v1"
        or endpoint_config.get("motion_capable") is not False
        or endpoint_config.get("content_sha256") != endpoint.get("content_sha256")
        or endpoint_config.get("addresses") != endpoint.get("addresses")
    ):
        raise QualificationError("canonical shell endpoint binding differs")
    counters = endpoint.get("counters")
    rtde = counters.get("rtde") if isinstance(counters, Mapping) else None
    dashboard = counters.get("dashboard") if isinstance(counters, Mapping) else None
    if (
        not isinstance(rtde, Mapping)
        or not isinstance(dashboard, Mapping)
    ):
        raise QualificationError("qualification endpoint counters are incomplete")
    trials_completed = rtde.get("trials_completed")
    arm_acknowledgements = rtde.get("arm_acknowledgements")
    play_transitions = dashboard.get("play_transitions")
    if (
        isinstance(trials_completed, bool)
        or not isinstance(trials_completed, int)
        or trials_completed < 1
        or isinstance(arm_acknowledgements, bool)
        or not isinstance(arm_acknowledgements, int)
        or arm_acknowledgements < 2
        or isinstance(play_transitions, bool)
        or play_transitions != 1
    ):
        raise QualificationError("qualification endpoint counters are incomplete")
    endpoint_events = endpoint.get("events")
    if not isinstance(endpoint_events, list):
        raise QualificationError("qualification endpoint events are invalid")
    endpoint_hashes = [
        _sha256_bytes(_canonical_bytes(dict(event)))
        for event in endpoint_events
        if isinstance(event, Mapping)
    ]
    expected_endpoint_hashes = [
        details[4]["endpoint_event_sha256"],
        trial_ref["endpoint_event_sha256"],
        details[6]["endpoint_event_sha256"],
    ]
    try:
        endpoint_positions = [endpoint_hashes.index(value) for value in expected_endpoint_hashes]
    except ValueError as exc:
        raise QualificationError("qualification endpoint event reference is absent") from exc
    if endpoint_positions != sorted(endpoint_positions) or len(set(endpoint_positions)) != 3:
        raise QualificationError("qualification endpoint lifecycle order differs")

    _, preflight = _read_json_reference(
        payload["preflight_evidence"], "qualification preflight"
    )
    if contract["preflight"] != payload["preflight_evidence"]:
        raise QualificationError("canonical shell preflight reference differs")
    if (
        preflight.get("schema") != "step5d.autotune-v3/live-preflight-snapshot-v3"
        or preflight.get("ok") is not True
        or preflight.get("fresh") is not True
        or preflight.get("release_manifest_sha256") != binding["manifest_sha256"]
    ):
        raise QualificationError("qualification preflight evidence differs")
    predicates = preflight.get("predicates")
    controller_delivery = (
        predicates.get("controller_delivery")
        if isinstance(predicates, Mapping)
        else None
    )
    if (
        not isinstance(controller_delivery, Mapping)
        or set(controller_delivery) != {"ok", "observation"}
        or controller_delivery.get("ok") is not True
        or not isinstance(controller_delivery.get("observation"), Mapping)
    ):
        raise QualificationError(
            "qualification preflight synthetic delivery evidence is incomplete"
        )
    from .release_identity import ReleaseIdentityError, load_current_release

    if release_identity is None:
        try:
            release = load_current_release(Path(experiment_root).resolve(strict=True))
        except ReleaseIdentityError as exc:
            raise QualificationError(
                f"qualification delivery release binding is invalid: {exc}"
            ) from exc
    else:
        release = release_identity
    if release.manifest_sha256 != binding["manifest_sha256"]:
        raise QualificationError("qualification delivery release manifest differs")
    _validate_synthetic_delivery_observation(
        Path(experiment_root),
        controller_delivery["observation"],
        release=release,
        endpoint_content_sha256=_require_sha256(
            endpoint.get("content_sha256"), "qualification endpoint content"
        ),
    )
    _, live_result = _read_json_reference(payload["live_result"], "live campaign result")
    if Path(payload["live_result"]["path"]).parent != Path(contract["output_root"]):
        raise QualificationError("canonical shell live-result path differs")
    if (
        live_result.get("schema")
        != "step5d.autotune-v3/live-campaign-launch-result-v1"
        or live_result.get("ok") is not True
        or live_result.get("bridge_alive_at_campaign_outcome") is not True
        or live_result.get("release_manifest_sha256") != binding["manifest_sha256"]
        or live_result.get("qualification_endpoint_content_sha256")
        != endpoint.get("content_sha256")
    ):
        raise QualificationError("qualification live campaign outcome differs")
    if details[7] != {
        "live_result_sha256": payload["live_result"]["sha256"],
        "endpoint_evidence_sha256": payload["endpoint_evidence"]["sha256"],
    }:
        raise QualificationError("qualification terminal lifecycle evidence differs")
    return binding


def qualification_integration_blocker() -> str:
    return (
        "the production bridge wrapper has no qualification-only injection seam for "
        "Dashboard, RTDE, Kunwei, and TP endpoints while retaining the real bridge "
        "wrapper and campaign/runtime process tree"
    )


@dataclass
class QualificationLifecycle:
    """Observe, but never synthesize, the production qualification lifecycle."""

    binding: Mapping[str, Any]
    bridge_pid: int
    bridge_starttime: int
    launch_nonce: str
    process_starttime_reader: Callable[[int], int | None] = read_process_starttime

    def __post_init__(self) -> None:
        validate_content_binding(self.binding)
        if isinstance(self.bridge_pid, bool) or self.bridge_pid <= 0:
            raise QualificationError("bridge PID must be positive")
        if isinstance(self.bridge_starttime, bool) or self.bridge_starttime <= 0:
            raise QualificationError("bridge process starttime must be positive")
        if not isinstance(self.launch_nonce, str) or not self.launch_nonce:
            raise QualificationError("bridge launch nonce must be non-empty")
        self.phase = QualificationPhase.STARTED
        self.events: list[dict[str, Any]] = []
        self.bridge_ready_ref: dict[str, Any] | None = None
        self.waiting_barrier: dict[str, Any] | None = None
        self.play_row: dict[str, Any] | None = None
        self.play_row_sha256: str | None = None
        self.first_arm_seq: int | None = None
        self.next_arm_seq: int | None = None
        self.trial_evidence_ref: dict[str, Any] | None = None
        self.endpoint_evidence_ref: dict[str, str] | None = None
        self.preflight_evidence_ref: dict[str, str] | None = None
        self.process_log_ref: dict[str, str] | None = None
        self.bridge_csv_ref: dict[str, str] | None = None
        self.live_result_ref: dict[str, str] | None = None
        self.canonical_shell_result: dict[str, Any] | None = None
        self.alive_at_campaign_outcome = False
        self._first_arm_monotonic_s: float | None = None
        self._trial_monotonic_s: float | None = None
        self._event(QualificationPhase.STARTED, {"bridge_pid": self.bridge_pid})

    def observe_canonical_shell_exit(
        self,
        returncode: int,
        contract_path: Path,
    ) -> None:
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            raise QualificationError("canonical qualification shell return code is invalid")
        process_by_role = {
            process["role"]: process
            for process in self.binding["process_tree"]["processes"]
        }
        shell = process_by_role.get("canonical_launcher")
        if shell is None:
            raise QualificationError("canonical qualification shell is not process-bound")
        self.canonical_shell_result = {
            "pid": shell["pid"],
            "starttime": shell["starttime"],
            "argv_sha256": shell["argv_sha256"],
            "returncode": returncode,
            "contract_ref": _reference_file(Path(contract_path)),
        }

    def _event(self, phase: QualificationPhase, details: Mapping[str, Any]) -> None:
        self.events.append(
            {
                "phase": phase.value,
                "observed_at_ns": time.time_ns(),
                "details": dict(details),
            }
        )

    def _bridge_alive(self) -> bool:
        return self.process_starttime_reader(self.bridge_pid) == self.bridge_starttime

    def observe_bridge_ready_file(
        self,
        path: Path,
        *,
        evidence_path: Path | None = None,
    ) -> None:
        if self.phase is not QualificationPhase.STARTED:
            raise QualificationError("bridge readiness was observed out of order")
        ready = Path(path)
        if not ready.is_absolute() or ready.name != "bridge_ready.json":
            raise QualificationError("bridge readiness must be the absolute production sentinel")
        if ready.is_symlink() or not ready.is_file():
            raise QualificationError("production bridge readiness sentinel is unavailable")
        encoded = ready.read_bytes()
        payload = _load_strict_json_bytes(encoded, "bridge readiness")
        required = {
            "ready_schema": "step5d_bridge_ready_v2",
            "ok": True,
            "pid": self.bridge_pid,
            "launch_nonce": self.launch_nonce,
            "prewarm_status": "ok",
            "v30_runtime_complete": True,
            "rtde_connected": True,
            "rtde_send_succeeded": True,
            "sensor_stream_ready": True,
        }
        if not isinstance(payload, Mapping) or any(
            payload.get(name) != value for name, value in required.items()
        ):
            raise QualificationError("bridge readiness lacks production predicates")
        production_fields = {
            "bridge_profile",
            "rtde_hz",
            "runtime_scheduler",
            "runtime_scheduler_lifecycle",
            "sensor_samples",
            "baseline_ready",
            "sensor_age_s",
            "sensor_stale_s",
            "parse_errors",
            "output_dir",
        }
        if not production_fields.issubset(payload):
            raise QualificationError("bridge readiness lacks production metadata")
        try:
            metadata_valid = (
                isinstance(payload["bridge_profile"], str)
                and bool(payload["bridge_profile"])
                and float(payload["rtde_hz"]) > 0.0
                and int(payload["sensor_samples"]) > 0
                and float(payload["sensor_age_s"]) >= 0.0
                and float(payload["sensor_age_s"]) <= float(payload["sensor_stale_s"])
                and int(payload["parse_errors"]) == 0
                and isinstance(payload["output_dir"], str)
                and Path(payload["output_dir"]).is_absolute()
            )
        except (TypeError, ValueError):
            metadata_valid = False
        if not metadata_valid:
            raise QualificationError("bridge readiness production metadata is invalid")
        if any(name in payload for name in ("transport", "motion_capable", "controller_connected")):
            raise QualificationError("a qualification harness may not manufacture bridge readiness")
        if not self._bridge_alive():
            raise QualificationError("bridge process exited before readiness observation")
        observed = ready
        if evidence_path is not None:
            snapshot = Path(evidence_path)
            if (
                not snapshot.is_absolute()
                or snapshot.parent != ready.parent
                or snapshot == ready
            ):
                raise QualificationError(
                    "bridge readiness evidence must be a distinct same-directory path"
                )
            _write_exact_snapshot(snapshot, encoded)
            observed = snapshot
        self.bridge_ready_ref = _reference_file(observed)
        self.phase = QualificationPhase.BRIDGE_READY
        self._event(self.phase, self.bridge_ready_ref)

    def observe_waiting_barrier(
        self,
        runner_ready_path: Path,
        supervisor_log_path: Path,
    ) -> None:
        if self.phase is not QualificationPhase.BRIDGE_READY:
            raise QualificationError("waiting barrier was observed out of order")
        if not self._bridge_alive():
            raise QualificationError("bridge exited before the waiting barrier")
        runner_ref = _reference_file(runner_ready_path)
        _, runner_ready = _read_json_reference(
            runner_ref, "campaign-runner readiness"
        )
        process_by_role = {
            process["role"]: process
            for process in self.binding["process_tree"]["processes"]
        }
        runner = process_by_role.get("campaign_runner")
        expected_runner_pid = None if runner is None else runner["pid"]
        if self.bridge_ready_ref is None:
            raise QualificationError("bridge readiness reference is unavailable")
        _validate_runner_ready(
            runner_ready,
            expected_pid=expected_runner_pid,
            expected_bridge_run=Path(self.bridge_ready_ref["path"]).parent,
        )
        log_path = Path(supervisor_log_path)
        if not log_path.is_absolute() or log_path.is_symlink() or not log_path.is_file():
            raise QualificationError("supervisor log is unavailable at the waiting barrier")
        encoded = log_path.read_bytes()
        positions = [encoded.find(marker.encode("ascii")) for marker in _WAITING_MARKERS]
        if positions[0] < 0 or positions != sorted(positions):
            raise QualificationError("supervisor did not publish the ordered waiting barrier")
        self.waiting_barrier = {
            "runner_ready_ref": runner_ref,
            "marker_sequence": list(_WAITING_MARKERS),
            "marker_sequence_sha256": _sha256_bytes(
                _canonical_bytes(list(_WAITING_MARKERS))
            ),
        }
        self.phase = QualificationPhase.WAITING_FOR_PLAY
        self._event(self.phase, self.waiting_barrier)

    def observe_play(self, production_csv_row: Mapping[str, Any]) -> None:
        if self.phase is not QualificationPhase.WAITING_FOR_PLAY:
            raise QualificationError("Play was observed out of order")
        try:
            playing = int(float(production_csv_row["ur_runtime_state"])) == 2
            normal = int(float(production_csv_row["ur_safety_mode"])) == 1
            connected = int(float(production_csv_row["rtde_connected"])) == 1
        except (KeyError, TypeError, ValueError) as exc:
            raise QualificationError("Play observation is not a production CSV row") from exc
        if not (playing and normal and connected):
            raise QualificationError("production CSV does not observe Play in NORMAL mode")
        self.play_row = dict(production_csv_row)
        self.play_row_sha256 = _sha256_bytes(_canonical_bytes(self.play_row))
        self.phase = QualificationPhase.PLAY_OBSERVED
        self._event(self.phase, {"row_sha256": self.play_row_sha256})

    def observe_arm_ack(self, runtime: Any, rtde_output: Mapping[str, Any]) -> None:
        from step5d_autotune_live_driver import BridgeMailboxRuntime, tp_packet_from_rtde
        from step5d_autotune_state_machine import HostCommand

        expected_phase = (
            QualificationPhase.PLAY_OBSERVED
            if self.first_arm_seq is None
            else QualificationPhase.TRIAL_COMPLETE
        )
        if self.phase is not expected_phase:
            raise QualificationError("ARM acknowledgement was observed out of order")
        if not isinstance(runtime, BridgeMailboxRuntime):
            raise QualificationError("ARM acknowledgement must come from BridgeMailboxRuntime")
        active = runtime.active
        if active is None or active.packet.command is not HostCommand.ARM:
            raise QualificationError("production runtime has no active ARM")
        if runtime.identity_commit_pending:
            raise QualificationError("ARM register-30 identity commit is still pending")
        snapshot = tp_packet_from_rtde(rtde_output)
        packet = active.packet
        matches = (
            snapshot.campaign_epoch_echo == packet.campaign_epoch
            and snapshot.trial_id_echo == packet.trial_id
            and snapshot.candidate_token_echo == packet.candidate_token
            and snapshot.execution_profile_id_echo == packet.execution_profile_id
            and snapshot.consumed_command_seq == packet.command_seq
            and (
                packet.logical_batch_sequence == 0
                or snapshot.logical_batch_sequence_echo == packet.logical_batch_sequence
            )
        )
        if not matches:
            raise QualificationError("TP did not acknowledge the active ARM identity")
        sequence = packet.command_seq
        if self.first_arm_seq is None:
            self.first_arm_seq = sequence
            self.phase = QualificationPhase.FIRST_ARM_ACK
        else:
            if sequence <= self.first_arm_seq:
                raise QualificationError("next ARM sequence did not increase")
            self.next_arm_seq = sequence
            self.phase = QualificationPhase.NEXT_ARM_ACK
        self._event(
            self.phase,
            {
                "command_seq": sequence,
                "trial_id": packet.trial_id,
                "tp_state": snapshot.state.name,
            },
        )

    def observe_endpoint_arm_ack(self, event: Mapping[str, Any]) -> None:
        expected_phase = (
            QualificationPhase.PLAY_OBSERVED
            if self.first_arm_seq is None
            else QualificationPhase.TRIAL_COMPLETE
        )
        expected_count = 1 if self.first_arm_seq is None else 2
        if self.phase is not expected_phase:
            raise QualificationError("endpoint ARM acknowledgement was observed out of order")
        observed = _validate_endpoint_event(
            event,
            expected_event="tp_arm_acknowledged",
            expected_arm_count=expected_count,
        )
        sequence = int(observed["command_seq"])
        monotonic_s = float(observed["monotonic_s"])
        details = {
            "command_seq": sequence,
            "trial_id": int(observed["trial_id"]),
            "arm_count": expected_count,
            "endpoint_event_sha256": _sha256_bytes(
                _canonical_bytes(dict(observed))
            ),
        }
        if self.first_arm_seq is None:
            self.first_arm_seq = sequence
            self._first_arm_monotonic_s = monotonic_s
            self.phase = QualificationPhase.FIRST_ARM_ACK
        else:
            if sequence <= self.first_arm_seq:
                raise QualificationError("next endpoint ARM sequence did not increase")
            if self._trial_monotonic_s is None or monotonic_s <= self._trial_monotonic_s:
                raise QualificationError("next endpoint ARM preceded trial completion")
            self.next_arm_seq = sequence
            self.phase = QualificationPhase.NEXT_ARM_ACK
        self._event(self.phase, details)

    def observe_trial_bundle(
        self,
        evidence_path: Path,
        endpoint_event: Mapping[str, Any],
    ) -> None:
        if self.phase is not QualificationPhase.FIRST_ARM_ACK:
            raise QualificationError("endpoint trial completion was observed out of order")
        observed = _validate_endpoint_event(
            endpoint_event,
            expected_event="tp_trial_completed",
        )
        if (
            observed["command_seq"] != self.first_arm_seq
            or self._first_arm_monotonic_s is None
            or float(observed["monotonic_s"]) <= self._first_arm_monotonic_s
        ):
            raise QualificationError("endpoint trial completion identity/order differs")
        reference = _reference_file(evidence_path)
        trial_path = Path(reference["path"])
        _validate_trial_bundle(
            trial_path,
            expected_trial_id=int(observed["trial_id"]),
            expected_command_seq=int(observed["command_seq"]),
        )
        self._trial_monotonic_s = float(observed["monotonic_s"])
        self.trial_evidence_ref = {
            **reference,
            "trial_id": int(observed["trial_id"]),
            "command_seq": int(observed["command_seq"]),
            "endpoint_event_sha256": _sha256_bytes(
                _canonical_bytes(dict(observed))
            ),
        }
        self.phase = QualificationPhase.TRIAL_COMPLETE
        self._event(self.phase, self.trial_evidence_ref)

    def observe_trial_complete(self, closure: Any, evidence_path: Path) -> None:
        from step5d_autotune_contract import SafeClosureEvidence

        if self.phase is not QualificationPhase.FIRST_ARM_ACK:
            raise QualificationError("trial completion was observed out of order")
        if not isinstance(closure, SafeClosureEvidence) or not closure.returned_safe:
            raise QualificationError("trial completion lacks real safe-closure evidence")
        path = Path(evidence_path)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise QualificationError("trial evidence must be an absolute regular file")
        self.trial_evidence_ref = {
            "path": str(path),
            "sha256": _sha256_file(path),
            "safe_closure_sha256": _sha256_bytes(_canonical_bytes(closure.payload())),
        }
        self.phase = QualificationPhase.TRIAL_COMPLETE
        self._event(self.phase, self.trial_evidence_ref)

    def observe_campaign_outcome(
        self,
        *,
        live_result_path: Path,
        endpoint_evidence_path: Path,
        preflight_path: Path,
        process_log_path: Path,
        bridge_csv_path: Path,
    ) -> None:
        if self.phase is not QualificationPhase.NEXT_ARM_ACK:
            raise QualificationError("campaign outcome was observed out of order")
        live_ref = _reference_file(live_result_path)
        endpoint_ref = _reference_file(endpoint_evidence_path)
        preflight_ref = _reference_file(preflight_path)
        process_log_ref = _reference_file(process_log_path)
        bridge_csv_ref = _reference_file(bridge_csv_path)
        _, live_result = _read_json_reference(live_ref, "live campaign result")
        _, endpoint = _read_json_reference(
            endpoint_ref, "qualification endpoint evidence"
        )
        _, preflight = _read_json_reference(preflight_ref, "qualification preflight")
        if (
            live_result.get("schema")
            != "step5d.autotune-v3/live-campaign-launch-result-v1"
            or live_result.get("ok") is not True
            or live_result.get("bridge_alive_at_campaign_outcome") is not True
            or live_result.get("release_manifest_sha256")
            != self.binding["manifest_sha256"]
        ):
            raise QualificationError("live campaign did not record a successful outcome")
        if (
            endpoint.get("schema")
            != "step5d.autotune-v3/qualification-endpoint-evidence-v1"
            or endpoint.get("alive") is not True
            or endpoint.get("errors") != []
            or live_result.get("qualification_endpoint_content_sha256")
            != endpoint.get("content_sha256")
        ):
            raise QualificationError("endpoint evidence does not bind the live outcome")
        if (
            preflight.get("schema")
            != "step5d.autotune-v3/live-preflight-snapshot-v3"
            or preflight.get("ok") is not True
            or preflight.get("fresh") is not True
            or preflight.get("release_manifest_sha256")
            != self.binding["manifest_sha256"]
        ):
            raise QualificationError("preflight evidence does not bind the live outcome")
        log_bytes = Path(process_log_ref["path"]).read_bytes()
        marker_positions = [
            log_bytes.find(marker.encode("ascii")) for marker in _WAITING_MARKERS
        ]
        if marker_positions[0] < 0 or marker_positions != sorted(marker_positions):
            raise QualificationError("final process log lost the waiting barrier")
        if self.play_row_sha256 is None:
            raise QualificationError("Play observation reference is unavailable")
        with Path(bridge_csv_ref["path"]).open(
            "r", encoding="utf-8", newline=""
        ) as handle:
            play_retained = any(
                _sha256_bytes(_canonical_bytes(dict(row))) == self.play_row_sha256
                for row in csv.DictReader(handle)
            )
        if not play_retained:
            raise QualificationError("final bridge CSV lost the Play observation")
        endpoint_hashes = {
            _sha256_bytes(_canonical_bytes(dict(event)))
            for event in endpoint.get("events", [])
            if isinstance(event, Mapping)
        }
        expected_hashes = {
            event["details"]["endpoint_event_sha256"]
            for event in self.events
            if event["phase"]
            in {
                QualificationPhase.FIRST_ARM_ACK.value,
                QualificationPhase.NEXT_ARM_ACK.value,
            }
        }
        if self.trial_evidence_ref is None:
            raise QualificationError("trial evidence reference is unavailable")
        expected_hashes.add(self.trial_evidence_ref["endpoint_event_sha256"])
        if not expected_hashes.issubset(endpoint_hashes):
            raise QualificationError("endpoint evidence lost a lifecycle event")
        self.live_result_ref = live_ref
        self.endpoint_evidence_ref = endpoint_ref
        self.preflight_evidence_ref = preflight_ref
        self.process_log_ref = process_log_ref
        self.bridge_csv_ref = bridge_csv_ref
        self.alive_at_campaign_outcome = True
        self.phase = QualificationPhase.QUALIFIED
        self._event(
            self.phase,
            {
                "live_result_sha256": live_ref["sha256"],
                "endpoint_evidence_sha256": endpoint_ref["sha256"],
            },
        )

    def result(self) -> dict[str, Any]:
        alive = self._bridge_alive()
        lifecycle_complete = (
            self.phase is QualificationPhase.QUALIFIED
            and self.alive_at_campaign_outcome
        ) or (self.phase is QualificationPhase.NEXT_ARM_ACK and alive)
        process_bound = bool(self.binding["process_tree"]["complete"])
        production_qualified = (
            self.phase is QualificationPhase.QUALIFIED
            and lifecycle_complete
            and process_bound
            and self.canonical_shell_result is not None
            and self.canonical_shell_result["returncode"] == 0
        )
        if production_qualified:
            reason = QUALIFIED
            state = QualificationPhase.QUALIFIED.value
        elif self.phase is QualificationPhase.STARTED:
            reason = BRIDGE_READY_MISSING if alive else BRIDGE_EXITED_IMMEDIATELY
            state = self.phase.value
        elif self.phase in {
            QualificationPhase.BRIDGE_READY,
            QualificationPhase.WAITING_FOR_PLAY,
            QualificationPhase.PLAY_OBSERVED,
        }:
            reason = FIRST_ARM_ACK_MISSING if alive else BRIDGE_EXITED_BEFORE_FIRST_ARM_ACK
            state = self.phase.value
        elif self.phase is QualificationPhase.FIRST_ARM_ACK:
            reason = TRIAL_COMPLETION_MISSING if alive else BRIDGE_EXITED_DURING_FIRST_TRIAL
            state = self.phase.value
        elif self.phase is QualificationPhase.TRIAL_COMPLETE:
            reason = NEXT_ARM_ACK_MISSING if alive else BRIDGE_EXITED_AFTER_FIRST_TRIAL
            state = self.phase.value
        elif self.phase is QualificationPhase.NEXT_ARM_ACK:
            if not alive:
                reason = BRIDGE_NOT_ALIVE_AFTER_NEXT_ACK
            else:
                reason = PROCESS_TREE_BINDING_INCOMPLETE
            state = self.phase.value
        elif self.phase is QualificationPhase.QUALIFIED:
            reason = PROCESS_TREE_BINDING_INCOMPLETE
            state = self.phase.value
        else:
            raise AssertionError(f"unhandled qualification phase: {self.phase}")
        return {
            "schema": QUALIFICATION_RESULT_SCHEMA,
            "ok": production_qualified,
            "lifecycle_complete": lifecycle_complete,
            "state": state,
            "reason_code": reason,
            "binding": dict(self.binding),
            "canonical_shell_result": self.canonical_shell_result,
            "bridge": {
                "pid": self.bridge_pid,
                "starttime": self.bridge_starttime,
                "launch_nonce": self.launch_nonce,
                "alive": alive,
                "alive_at_campaign_outcome": self.alive_at_campaign_outcome,
                "ready_ref": self.bridge_ready_ref,
            },
            "waiting_barrier": self.waiting_barrier,
            "play_row": self.play_row,
            "play_row_sha256": self.play_row_sha256,
            "first_arm_seq": self.first_arm_seq,
            "trial_evidence_ref": self.trial_evidence_ref,
            "next_arm_seq": self.next_arm_seq,
            "events": list(self.events),
            "endpoint_evidence": self.endpoint_evidence_ref,
            "preflight_evidence": self.preflight_evidence_ref,
            "process_log": self.process_log_ref,
            "bridge_csv": self.bridge_csv_ref,
            "live_result": self.live_result_ref,
            "remaining_integration_seam": (
                None if production_qualified else qualification_integration_blocker()
            ),
        }


def write_qualification_evidence(output_root: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Write immutable, content-addressed evidence; never write readiness state."""

    root = Path(output_root)
    if not root.is_absolute():
        raise QualificationError("qualification evidence root must be absolute")
    encoded = _canonical_bytes(dict(payload))
    digest = _sha256_bytes(encoded)
    directory = root / "qualification" / "evidence" / digest
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise QualificationError("qualification evidence directory must not be a symlink")
    path = directory / "qualification.json"
    if path.exists():
        if path.is_symlink() or path.read_bytes() != encoded:
            raise QualificationError("content-addressed qualification evidence conflicts")
    else:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        directory_descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    return {
        "schema": QUALIFICATION_EVIDENCE_SCHEMA,
        "path": str(path),
        "sha256": digest,
    }


def _qualification_environment(values: Mapping[str, str]) -> dict[str, str]:
    return production_runtime_environment(values)


def _process_cmdline(pid: int) -> tuple[str, ...]:
    try:
        return tuple(
            value.decode("utf-8", errors="surrogateescape")
            for value in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if value
        )
    except OSError:
        return ()


def _direct_children(pid: int) -> tuple[int, ...]:
    try:
        text = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii")
        return tuple(int(value) for value in text.split())
    except (OSError, UnicodeError, ValueError):
        return ()


def _descendants(pid: int) -> tuple[int, ...]:
    pending = list(_direct_children(pid))
    observed: list[int] = []
    seen: set[int] = set()
    while pending:
        child = pending.pop(0)
        if child in seen:
            continue
        seen.add(child)
        observed.append(child)
        pending.extend(_direct_children(child))
    return tuple(observed)


def _wait_for_production_processes(
    supervisor: subprocess.Popen[Any], root: Path, timeout_s: float
) -> dict[str, int]:
    expected = {
        role: str((root / relative).resolve(strict=True))
        for role, relative in _PROCESS_ROLE_PATHS.items()
        if role != "canonical_launcher"
    }
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if supervisor.poll() is not None:
            break
        roles = {"canonical_launcher": supervisor.pid}
        for child in _descendants(supervisor.pid):
            argv = _process_cmdline(child)
            resolved = {
                str(Path(value).resolve()) for value in argv if value.startswith("/")
            }
            for role, script in expected.items():
                if script in argv or script in resolved:
                    roles[role] = child
        if set(roles) == set(_PROCESS_ROLE_PATHS):
            return roles
        time.sleep(0.05)
    raise QualificationBlocked("production qualification process tree is incomplete")


def _wait_for_waiting_barrier(
    supervisor: subprocess.Popen[Any],
    *,
    bridge_ready_path: Path,
    runner_ready_path: Path,
    supervisor_log_path: Path,
    timeout_s: float,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if supervisor.poll() is not None:
            raise QualificationBlocked(
                "production supervisor exited before the waiting barrier"
            )
        if (
            bridge_ready_path.is_file()
            and not bridge_ready_path.is_symlink()
            and runner_ready_path.is_file()
            and not runner_ready_path.is_symlink()
            and supervisor_log_path.is_file()
            and not supervisor_log_path.is_symlink()
        ):
            encoded = supervisor_log_path.read_bytes()
            positions = [
                encoded.find(marker.encode("ascii")) for marker in _WAITING_MARKERS
            ]
            if positions[0] >= 0 and positions == sorted(positions):
                return
        time.sleep(0.025)
    raise QualificationBlocked("production supervisor did not reach the waiting barrier")


def _latest_complete_csv_row(path: Path) -> dict[str, str] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error):
        return None
    for row in reversed(rows):
        if None not in row and row and all(value is not None for value in row.values()):
            return dict(row)
    return None


def _wait_for_play_row(
    supervisor: subprocess.Popen[Any],
    bridge_csv_path: Path,
    timeout_s: float,
) -> dict[str, str]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = _latest_complete_csv_row(bridge_csv_path)
        if row is not None:
            try:
                playing = (
                    int(float(row["ur_runtime_state"])) == 2
                    and int(float(row["ur_safety_mode"])) == 1
                    and int(float(row["rtde_connected"])) == 1
                )
            except (KeyError, TypeError, ValueError):
                playing = False
            if playing:
                return row
        if supervisor.poll() is not None:
            break
        time.sleep(0.01)
    raise QualificationBlocked("production bridge did not retain the simulated Play row")


def _reference_file(path: Path) -> dict[str, str]:
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file():
        raise QualificationError(f"qualification evidence file is unsafe: {candidate}")
    resolved = candidate.resolve(strict=True)
    if resolved != candidate:
        raise QualificationError(f"qualification evidence path is not canonical: {candidate}")
    return {"path": str(resolved), "sha256": _sha256_file(resolved)}


def _write_exact_snapshot(path: Path, encoded: bytes) -> None:
    candidate = Path(path)
    if not candidate.is_absolute() or candidate.is_symlink():
        raise QualificationError(f"qualification snapshot path is unsafe: {candidate}")
    candidate.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if candidate.parent.is_symlink() or not candidate.parent.is_dir():
        raise QualificationError(
            f"qualification snapshot directory is unsafe: {candidate.parent}"
        )
    if candidate.exists():
        if not candidate.is_file() or candidate.read_bytes() != encoded:
            raise QualificationError(
                f"qualification snapshot conflicts: {candidate}"
            )
        return
    descriptor = os.open(
        candidate,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        candidate.unlink(missing_ok=True)
        raise
    directory_descriptor = os.open(
        candidate.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _absolute_contract_path(value: Any, role: str, *, must_exist: bool) -> Path:
    if not isinstance(value, str):
        raise QualificationError(f"{role} path is invalid")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise QualificationError(f"{role} path is unsafe")
    resolved = path.resolve(strict=must_exist)
    if resolved != path:
        raise QualificationError(f"{role} path is not canonical")
    return path


def _validate_internal_shell_contract(
    experiment_root: Path,
    payload: Any,
) -> Mapping[str, Any]:
    root = Path(experiment_root).resolve(strict=True)
    required = {
        "schema",
        "caller",
        "canonical_launcher",
        "live_worker",
        "python_executable",
        "run_root",
        "output_root",
        "campaign_root",
        "release_manifest",
        "endpoint_config",
        "launch_profile",
        "preflight",
        "delivery_observation",
        "ready_timeout_s",
        "play_timeout_s",
        "requested_argv",
    }
    if (
        not isinstance(payload, Mapping)
        or set(payload) != required
        or payload.get("schema") != INTERNAL_SHELL_CONTRACT_SCHEMA
    ):
        raise QualificationError("internal qualification shell contract fields differ")
    caller = payload["caller"]
    if not isinstance(caller, Mapping) or set(caller) != {"pid", "starttime"}:
        raise QualificationError("internal qualification caller binding differs")
    _positive_integer(caller["pid"], "internal qualification caller PID")
    _positive_integer(caller["starttime"], "internal qualification caller starttime")

    launcher = payload["canonical_launcher"]
    worker = payload["live_worker"]
    for role, reference, expected in (
        ("canonical launcher", launcher, root / "scripts/step5d-autotune-v3.sh"),
        ("live worker", worker, root / "tools/run_step5d_autotune_v3_live.py"),
    ):
        path, _encoded = _read_reference_file(reference, role)
        if path != expected.resolve(strict=True):
            raise QualificationError(f"internal qualification {role} path differs")

    python_value = payload["python_executable"]
    if not isinstance(python_value, str) or not Path(python_value).is_absolute():
        raise QualificationError("internal qualification Python path is invalid")
    python_path = Path(python_value)
    runtime_pointer = load_runtime_pointer()
    if (
        python_value
        != runtime_pointer["profiles"]["control"]["python_executable"]
        or not python_path.is_file()
        or not os.access(python_path, os.X_OK)
    ):
        raise QualificationError("internal qualification Python binding differs")
    run_root = _absolute_contract_path(
        payload["run_root"], "internal qualification run root", must_exist=True
    )
    if not run_root.is_dir():
        raise QualificationError("internal qualification run root is not a directory")
    output_root = _absolute_contract_path(
        payload["output_root"], "internal qualification output root", must_exist=False
    )
    campaign_root = _absolute_contract_path(
        payload["campaign_root"], "internal qualification campaign root", must_exist=False
    )
    if output_root.parent != run_root or campaign_root.parent != run_root:
        raise QualificationError("internal qualification output roots escape the run")

    references: dict[str, Path] = {}
    for role in (
        "release_manifest",
        "endpoint_config",
        "launch_profile",
        "preflight",
        "delivery_observation",
    ):
        path, _encoded = _read_reference_file(payload[role], role.replace("_", " "))
        references[role] = path
    for role in ("endpoint_config", "preflight", "delivery_observation"):
        try:
            references[role].relative_to(run_root)
        except ValueError as exc:
            raise QualificationError(
                f"internal qualification {role.replace('_', ' ')} escapes the run"
            ) from exc

    for role in ("ready_timeout_s", "play_timeout_s"):
        value = payload[role]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0.0
        ):
            raise QualificationError(f"internal qualification {role} is invalid")
    requested_argv = payload["requested_argv"]
    expected_argv = [
        str((root / "scripts/step5d-autotune-v3.sh").resolve(strict=True)),
        "bridge",
        "--output-root",
        str(output_root),
        "--campaign-root",
        str(campaign_root),
        "--ready-timeout-s",
        str(payload["ready_timeout_s"]),
        "--play-timeout-s",
        str(payload["play_timeout_s"]),
    ]
    if requested_argv != expected_argv:
        raise QualificationError("internal qualification canonical argv differs")
    return payload


def _write_internal_shell_contract(
    experiment_root: Path,
    run_root: Path,
    *,
    output_root: Path,
    campaign_root: Path,
    release_manifest_path: Path,
    endpoint_config_path: Path,
    launch_profile_path: Path,
    preflight_path: Path,
    delivery_observation_path: Path,
    ready_timeout_s: float,
    play_timeout_s: float,
    python_executable: str,
) -> tuple[Path, Mapping[str, Any]]:
    root = Path(experiment_root).resolve(strict=True)
    run = Path(run_root).resolve(strict=True)
    launcher = (root / "scripts/step5d-autotune-v3.sh").resolve(strict=True)
    caller_starttime = read_process_starttime(os.getpid())
    if caller_starttime is None:
        raise QualificationError("qualification caller process is unavailable")
    requested_argv = [
        str(launcher),
        "bridge",
        "--output-root",
        str(Path(output_root).resolve()),
        "--campaign-root",
        str(Path(campaign_root).resolve()),
        "--ready-timeout-s",
        str(ready_timeout_s),
        "--play-timeout-s",
        str(play_timeout_s),
    ]
    payload = {
        "schema": INTERNAL_SHELL_CONTRACT_SCHEMA,
        "caller": {"pid": os.getpid(), "starttime": caller_starttime},
        "canonical_launcher": _reference_file(launcher),
        "live_worker": _reference_file(
            (root / "tools/run_step5d_autotune_v3_live.py").resolve(strict=True)
        ),
        "python_executable": python_executable,
        "run_root": str(run),
        "output_root": str(Path(output_root).resolve()),
        "campaign_root": str(Path(campaign_root).resolve()),
        "release_manifest": _reference_file(Path(release_manifest_path).resolve(strict=True)),
        "endpoint_config": _reference_file(Path(endpoint_config_path).resolve(strict=True)),
        "launch_profile": _reference_file(Path(launch_profile_path).resolve(strict=True)),
        "preflight": _reference_file(Path(preflight_path).resolve(strict=True)),
        "delivery_observation": _reference_file(
            Path(delivery_observation_path).resolve(strict=True)
        ),
        "ready_timeout_s": ready_timeout_s,
        "play_timeout_s": play_timeout_s,
        "requested_argv": requested_argv,
    }
    _validate_internal_shell_contract(root, payload)
    path = run / "internal-qualification-shell-contract.json"
    _write_exact_snapshot(path, _canonical_bytes(payload))
    return path, payload


def exec_internal_shell_contract(
    experiment_root: Path,
    contract_path: Path,
    *,
    environment: Mapping[str, str] | None = None,
) -> None:
    """Validate a one-run contract and replace only its shell child with live.py."""

    values = dict(os.environ if environment is None else environment)
    root = Path(experiment_root).resolve(strict=True)
    path = Path(contract_path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise QualificationError("internal qualification contract path is unsafe")
    if path.resolve(strict=True) != path:
        raise QualificationError("internal qualification contract path is not canonical")
    if (
        values.get(INTERNAL_SHELL_CONTRACT_ENV) != str(path)
        or values.get(INTERNAL_SHELL_CONTRACT_SHA_ENV) != _sha256_file(path)
    ):
        raise QualificationError("internal qualification contract environment differs")
    payload = _load_strict_json_bytes(
        path.read_bytes(), "internal qualification shell contract"
    )
    contract = _validate_internal_shell_contract(root, payload)
    try:
        shell_pid = int(values[INTERNAL_SHELL_PID_ENV])
    except (KeyError, TypeError, ValueError) as exc:
        raise QualificationError("internal qualification shell PID is missing") from exc
    if shell_pid != os.getppid():
        raise QualificationError("internal qualification worker is not a shell child")
    shell = _capture_process(
        shell_pid,
        "canonical_launcher",
        root / _PROCESS_ROLE_PATHS["canonical_launcher"],
        {},
    )
    caller = contract["caller"]
    if (
        shell["ppid"] != caller["pid"]
        or read_process_starttime(caller["pid"]) != caller["starttime"]
    ):
        raise QualificationError("internal qualification caller identity differs")
    requested_argv = contract["requested_argv"]
    if shell["argv"][-len(requested_argv) :] != requested_argv:
        raise QualificationError("canonical shell runtime argv differs from contract")

    from .release_identity import (
        QUALIFICATION_ENDPOINT_CONFIG_ENV,
        QUALIFICATION_MODE_ENV,
        QUALIFICATION_MODE_VALUE,
        QUALIFICATION_RELEASE_MANIFEST_ENV,
    )

    required_environment = {
        CANONICAL_LAUNCH_ENV: contract["canonical_launcher"]["path"],
        QUALIFICATION_RELEASE_MANIFEST_ENV: contract["release_manifest"]["path"],
        QUALIFICATION_ENDPOINT_CONFIG_ENV: contract["endpoint_config"]["path"],
        QUALIFICATION_MODE_ENV: QUALIFICATION_MODE_VALUE,
    }
    if any(values.get(name) != expected for name, expected in required_environment.items()):
        raise QualificationError("internal qualification endpoint/release environment differs")
    live_argv = [
        contract["python_executable"],
        contract["live_worker"]["path"],
        "--output-root",
        contract["output_root"],
        "--campaign-root",
        contract["campaign_root"],
        "--launch-profile",
        contract["launch_profile"]["path"],
        "--preflight",
        contract["preflight"]["path"],
        "--delivery-observation",
        contract["delivery_observation"]["path"],
        "--qualification-endpoints",
        contract["endpoint_config"]["path"],
        "--canonical-owner-pid",
        str(shell_pid),
        "--canonical-owner-starttime",
        str(read_process_starttime(shell_pid)),
        "--ready-timeout-s",
        str(contract["ready_timeout_s"]),
        "--play-timeout-s",
        str(contract["play_timeout_s"]),
    ]
    os.execve(live_argv[0], live_argv, values)


def _validate_synthetic_delivery_observation(
    experiment_root: Path,
    value: Mapping[str, Any],
    *,
    release: Any,
    endpoint_content_sha256: str,
) -> Mapping[str, Any]:
    """Validate the endpoint-only receipt without claiming real controller I/O."""

    from step5d_autotune_v3.delivery_observation import (
        DeliveryObservationError,
        validate_delivery_observation,
    )

    root = Path(experiment_root).resolve(strict=True)
    endpoint_sha256 = _require_sha256(
        endpoint_content_sha256, "qualification endpoint content"
    )
    try:
        checked_at = datetime.fromisoformat(
            str(value.get("fresh_controller_checked_at"))
        )
        observed = validate_delivery_observation(
            root,
            value,
            release=release,
            now=checked_at,
        )
    except (DeliveryObservationError, TypeError, ValueError) as exc:
        raise QualificationError(
            f"qualification synthetic delivery observation is invalid: {exc}"
        ) from exc

    receipt_reference = observed["receipt"]
    receipt_path = root / Path(str(receipt_reference["path"]))
    receipt = _load_strict_json_bytes(
        receipt_path.read_bytes(), "qualification synthetic delivery receipt"
    )
    required_receipt_fields = {
        "delivery_mode",
        "fresh_controller_checked_at",
        "fresh_controller_sha_verified",
        "qualification_endpoint_substitution",
        "readback_source",
        "sha256",
        "status",
        "target_dir",
        "upload_transaction_id",
        "validation",
    }
    if not isinstance(receipt, Mapping) or set(receipt) != required_receipt_fields:
        raise QualificationError(
            "qualification synthetic delivery receipt fields differ"
        )
    expected_hashes = dict(release.artifact_sha256)
    hashes = receipt.get("sha256")
    validation = receipt.get("validation")
    target = Path(str(release.controller_target))
    expected_validation = {
        "program": release.program_id,
        "target_dir": target.parent.as_posix(),
        "script_node_path": target.with_suffix(".script").as_posix(),
        "script_sha256": expected_hashes[".script"],
        "txt_sha256": expected_hashes[".txt"],
        "urp_sha256": expected_hashes[".urp"],
    }
    if (
        not isinstance(hashes, Mapping)
        or not all(
            isinstance(hashes.get(role), Mapping)
            and dict(hashes[role]) == expected_hashes
            for role in ("local", "controller", "readback")
        )
        or not isinstance(validation, Mapping)
        or validation != expected_validation
        or receipt.get("upload_transaction_id") != observed["transaction_id"]
        or receipt.get("status") != "controller read-back verified"
        or receipt.get("delivery_mode") != "full_upload_readback"
        or receipt.get("readback_source") != "fresh_controller_get"
        or receipt.get("fresh_controller_sha_verified") is not True
        or receipt.get("target_dir") != target.parent.as_posix()
        or receipt.get("fresh_controller_checked_at")
        != observed["fresh_controller_checked_at"]
    ):
        raise QualificationError(
            "qualification synthetic delivery receipt identity closure differs"
        )
    marker = receipt.get("qualification_endpoint_substitution")
    expected_marker = {
        "schema": SYNTHETIC_DELIVERY_RECEIPT_SCHEMA,
        "controller_contacted": False,
        "endpoint_only": True,
        "motion_capable": False,
        "endpoint_content_sha256": endpoint_sha256,
    }
    if marker != expected_marker:
        raise QualificationError(
            "qualification synthetic delivery endpoint binding differs"
        )
    return observed


def _write_synthetic_delivery_observation(
    experiment_root: Path,
    run_root: Path,
    *,
    release: Any,
    endpoint_content_sha256: str,
) -> Path:
    """Write a valid delivery input bound only to the localhost qualification seam."""

    from step5d_autotune_v3.delivery_observation import build_delivery_observation
    from step5d_autotune_v3.state import atomic_json

    root = Path(experiment_root).resolve(strict=True)
    run = Path(run_root).resolve(strict=True)
    endpoint_sha256 = _require_sha256(
        endpoint_content_sha256, "qualification endpoint content"
    )
    transaction_id = uuid.uuid4().hex
    try:
        run.relative_to(root)
    except ValueError:
        receipt_root = (
            root
            / "runs/step5d_autotune_v3/qualification-synthetic-delivery"
            / transaction_id
        )
    else:
        receipt_root = run / "synthetic-delivery"
    receipt_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    receipt_path = receipt_root / "receipt.json"
    checked_at = datetime.now(timezone.utc).isoformat()
    artifact_sha256 = dict(release.artifact_sha256)
    controller_target = Path(str(release.controller_target))
    atomic_json(
        receipt_path,
        {
            "delivery_mode": "full_upload_readback",
            "fresh_controller_checked_at": checked_at,
            "fresh_controller_sha_verified": True,
            "qualification_endpoint_substitution": {
                "schema": SYNTHETIC_DELIVERY_RECEIPT_SCHEMA,
                "controller_contacted": False,
                "endpoint_only": True,
                "motion_capable": False,
                "endpoint_content_sha256": endpoint_sha256,
            },
            "readback_source": "fresh_controller_get",
            "sha256": {
                "local": artifact_sha256,
                "controller": artifact_sha256,
                "readback": artifact_sha256,
            },
            "upload_transaction_id": transaction_id,
            "status": "controller read-back verified",
            "target_dir": controller_target.parent.as_posix(),
            "validation": {
                "program": release.program_id,
                "target_dir": controller_target.parent.as_posix(),
                "script_node_path": controller_target.with_suffix(
                    ".script"
                ).as_posix(),
                "script_sha256": artifact_sha256[".script"],
                "txt_sha256": artifact_sha256[".txt"],
                "urp_sha256": artifact_sha256[".urp"],
            },
        },
    )
    observation = build_delivery_observation(
        root,
        receipt_path=receipt_path,
        receipt_sha256=_sha256_file(receipt_path),
        transaction_id=transaction_id,
        release=release,
    )
    observation_path = run / "delivery-observation.json"
    atomic_json(observation_path, observation)
    _validate_synthetic_delivery_observation(
        root,
        observation,
        release=release,
        endpoint_content_sha256=endpoint_sha256,
    )
    return observation_path


def _terminate_supervisor(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=8.0)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)


def run_endpoint_qualification(
    experiment_root: Path,
    output_root: Path,
    *,
    environment: Mapping[str, str] | None = None,
    release_identity: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the production bridge/runtime tree against no-motion localhost endpoints."""

    values = os.environ if environment is None else environment
    root = Path(experiment_root).resolve(strict=True)
    launcher = require_canonical_launcher(root, values)
    from step5d_autotune_v3.release_identity import (
        LAUNCH_PROFILE_PATH,
        QUALIFICATION_ENDPOINT_CONFIG_ENV,
        QUALIFICATION_MODE_ENV,
        QUALIFICATION_MODE_VALUE,
        QUALIFICATION_RELEASE_MANIFEST_ENV,
        ReleaseIdentityError,
        load_current_release,
        load_release_manifest,
        release_payload_path,
    )

    if release_identity is None:
        try:
            release = load_current_release(root)
        except ReleaseIdentityError as exc:
            raise QualificationError(f"CURRENT_RELEASE_INVALID: {exc}") from exc
    else:
        try:
            release = load_release_manifest(
                root,
                root / release_identity.manifest_path,
                expected_manifest_sha256=release_identity.manifest_sha256,
            )
        except ReleaseIdentityError as exc:
            raise QualificationError(f"LOCAL_RELEASE_INVALID: {exc}") from exc
    binding_release = release if release_identity is not None else None
    from step5d_autotune_v3.qualification_endpoints import (
        QualificationEndpointSimulator,
    )
    from step5d_autotune_v3.runtime_gate import release_runtime_contract
    from step5d_autotune_v3.state import atomic_json, read_strict_json

    output = Path(output_root).resolve()
    if output.exists() and (output.is_symlink() or not output.is_dir()):
        raise QualificationError("qualification output root is unsafe")
    output.mkdir(parents=True, exist_ok=True)
    # Reject a stale or incomplete release source closure before consulting
    # any host-local runtime state.
    _source_binding(
        root,
        release.manifest_sha256,
        release_identity=binding_release,
    )
    clean_environment = _qualification_environment(values)
    runtime_pointer = load_runtime_pointer(environ=clean_environment)
    try:
        load_gpu_functional_attestation(runtime_pointer=runtime_pointer)
    except RuntimeFunctionalGateError as exc:
        raise QualificationError(f"GPU_FUNCTIONAL_GATE_MISSING: {exc}") from exc
    runtime_environment = clean_environment
    prebinding = capture_content_binding(
        root,
        manifest_sha256=release.manifest_sha256,
        environment=clean_environment,
        release_identity=binding_release,
    )
    process_fingerprint = qualification_process_tree_fingerprint(root)
    cache_key = _sha256_bytes(
        _canonical_bytes(
            {
                "schema": "step5d.autotune-v3/qualification-cache-key-v1",
                "manifest_sha256": release.manifest_sha256,
                "source_fingerprint": prebinding["source"]["fingerprint"],
                "source_files_fingerprint": prebinding["source"][
                    "files_fingerprint"
                ],
                "launcher_sha256": prebinding["launcher"]["sha256"],
                "environment_sha256": prebinding["environment"]["fingerprint"],
                "process_tree_fingerprint": process_fingerprint,
            }
        )
    )
    current_path = output / "qualification/current.json"
    if current_path.is_file() and not current_path.is_symlink():
        from step5d_autotune_v3.state import read_strict_json

        current = read_strict_json(current_path, role="qualification current pointer")
        if (
            isinstance(current, Mapping)
            and set(current)
            == {"schema", "cache_key", "path", "sha256"}
            and current["schema"]
            == "step5d.autotune-v3/qualification-current-pointer-v1"
            and current["cache_key"] == cache_key
            and isinstance(current["path"], str)
        ):
            cached_path = (output / current["path"]).resolve()
            try:
                cached_path.relative_to(output)
            except ValueError:
                cached_path = output / ".invalid-qualification-cache"
            if (
                cached_path.is_file()
                and not cached_path.is_symlink()
                and _sha256_file(cached_path) == current["sha256"]
            ):
                cached = read_strict_json(cached_path, role="cached qualification")
                try:
                    validate_qualification_result(
                        cached,
                        experiment_root=root,
                        manifest_sha256=release.manifest_sha256,
                        source_fingerprint=prebinding["source"]["fingerprint"],
                        launcher_sha256=prebinding["launcher"]["sha256"],
                        release_identity=binding_release,
                    )
                except QualificationError:
                    pass
                else:
                    return dict(cached), {
                        "schema": QUALIFICATION_EVIDENCE_SCHEMA,
                        "path": str(cached_path),
                        "sha256": current["sha256"],
                    }
    run_root = output / "qualification" / "runs" / uuid.uuid4().hex
    run_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    launch_profile = release_payload_path(root, release, LAUNCH_PROFILE_PATH)
    preflight_path = run_root / "preflight.json"
    preflight_log = run_root / "preflight.log"
    endpoint_config_path = run_root / "qualification_endpoints.json"
    supervisor_log = run_root / "launcher_supervisor.log"
    live_root = run_root / "live"
    campaign_root = run_root / "campaign"
    bridge_ready_path = live_root / "runtime/bridge/bridge_ready.json"
    bridge_ready_evidence_path = (
        live_root / "runtime/bridge/qualification_bridge_ready.json"
    )
    runner_ready_path = live_root / "runtime/bridge/runtime/campaign_runner_ready.json"
    bridge_csv_path = live_root / "runtime/bridge/bridge_rtde_500hz.csv"
    live_result_path = live_root / "live_campaign_result.json"
    runtime_contract = release_runtime_contract(root, release)
    binding: Mapping[str, Any] | None = None
    lifecycle: QualificationLifecycle | None = None
    supervisor: subprocess.Popen[Any] | None = None
    live_result: Mapping[str, Any] = {}
    endpoint_evidence: Mapping[str, Any] = {}
    endpoint_simulator: Any | None = None
    blocker: str | None = None
    started_at = time.time_ns()

    try:
        with (
            qualification_endpoint_lease(
                run_root,
                environment=clean_environment,
            ),
            QualificationEndpointSimulator(
                dashboard_port=QUALIFICATION_ENDPOINT_PORTS["dashboard"],
                secondary_port=QUALIFICATION_ENDPOINT_PORTS["secondary"],
                rtde_port=QUALIFICATION_ENDPOINT_PORTS["rtde"],
                kunwei_port=QUALIFICATION_ENDPOINT_PORTS["kunwei"],
                runtime_identity=runtime_contract["tp_runtime_identity"],
                loaded_program=runtime_contract["expected_loaded_program"],
            ) as endpoints,
        ):
            endpoint_simulator = endpoints
            atomic_json(
                endpoint_config_path,
                {
                    "schema": "step5d.autotune-v3/qualification-endpoint-config-v1",
                    "content_sha256": endpoints.content_sha256,
                    "addresses": endpoints.addresses,
                    "motion_capable": False,
                },
            )
            runtime_environment = production_runtime_environment(
                clean_environment,
                runtime_pointer=runtime_pointer,
                additions={
                    QUALIFICATION_RELEASE_MANIFEST_ENV: str(
                        (root / release.manifest_path).resolve(strict=True)
                    ),
                    QUALIFICATION_ENDPOINT_CONFIG_ENV: str(endpoint_config_path),
                    QUALIFICATION_MODE_ENV: QUALIFICATION_MODE_VALUE,
                },
            )
            delivery_observation_path = _write_synthetic_delivery_observation(
                root,
                run_root,
                release=release,
                endpoint_content_sha256=endpoints.content_sha256,
            )
            preflight_command = [
                runtime_pointer["profiles"]["control"]["python_executable"],
                str(root / "tools/preflight_step5d_autotune_v3.py"),
                "--robot-host",
                "127.0.0.1",
                "--sensor-ip",
                "127.0.0.1",
                "--sensor-port",
                str(endpoints.kunwei_port),
                "--mailbox",
                str(live_root / "runtime/command.json"),
                "--launch-profile",
                str(launch_profile),
                "--delivery-observation",
                str(delivery_observation_path),
                "--output",
                str(preflight_path),
                "--json",
            ]
            with preflight_log.open("wb") as log:
                preflight = subprocess.run(
                    preflight_command,
                    cwd=root,
                    env=runtime_environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    timeout=60.0,
                    check=False,
                )
            if preflight.returncode != 0:
                raise QualificationBlocked(
                    f"production preflight failed rc={preflight.returncode}"
                )
            preflight_payload = read_strict_json(
                preflight_path, role="qualification preflight"
            )
            if preflight_payload.get("ok") is not True:
                raise QualificationBlocked("production preflight did not pass")

            contract_path, contract = _write_internal_shell_contract(
                root,
                run_root,
                output_root=live_root,
                campaign_root=campaign_root,
                release_manifest_path=root / release.manifest_path,
                endpoint_config_path=endpoint_config_path,
                launch_profile_path=launch_profile,
                preflight_path=preflight_path,
                delivery_observation_path=delivery_observation_path,
                ready_timeout_s=60.0,
                play_timeout_s=30.0,
                python_executable=runtime_pointer["profiles"]["control"][
                    "python_executable"
                ],
            )
            runtime_environment = {
                **runtime_environment,
                INTERNAL_SHELL_CONTRACT_ENV: str(contract_path),
                INTERNAL_SHELL_CONTRACT_SHA_ENV: _sha256_file(contract_path),
            }
            live_command = list(contract["requested_argv"])
            with supervisor_log.open("wb") as log:
                supervisor = subprocess.Popen(
                    live_command,
                    cwd=root,
                    env=runtime_environment,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                )
                process_pids = _wait_for_production_processes(
                    supervisor, root, timeout_s=180.0
                )
                binding = capture_content_binding(
                    root,
                    manifest_sha256=release.manifest_sha256,
                    process_pids=process_pids,
                    environment=runtime_environment,
                    release_identity=binding_release,
                )
                bridge_ready = read_strict_json(
                    bridge_ready_path, role="qualification bridge readiness"
                )
                launch_nonce = bridge_ready.get("launch_nonce")
                if not isinstance(launch_nonce, str) or not launch_nonce:
                    raise QualificationBlocked(
                        "production bridge readiness lacks a launch nonce"
                    )
                bridge_starttime = read_process_starttime(
                    process_pids["bridge_wrapper"]
                )
                if bridge_starttime is None:
                    raise QualificationBlocked(
                        "production bridge exited before lifecycle observation"
                    )
                lifecycle = QualificationLifecycle(
                    binding=binding,
                    bridge_pid=process_pids["bridge_wrapper"],
                    bridge_starttime=bridge_starttime,
                    launch_nonce=launch_nonce,
                )
                lifecycle.observe_bridge_ready_file(
                    bridge_ready_path,
                    evidence_path=bridge_ready_evidence_path,
                )
                _wait_for_waiting_barrier(
                    supervisor,
                    bridge_ready_path=bridge_ready_path,
                    runner_ready_path=runner_ready_path,
                    supervisor_log_path=supervisor_log,
                    timeout_s=60.0,
                )
                lifecycle.observe_waiting_barrier(
                    runner_ready_path,
                    supervisor_log,
                )
                endpoints.simulate_play()
                lifecycle.observe_play(
                    _wait_for_play_row(supervisor, bridge_csv_path, timeout_s=30.0)
                )
                try:
                    supervisor.wait(timeout=150.0)
                except subprocess.TimeoutExpired as exc:
                    raise QualificationBlocked(
                        "production qualification exceeded the lifecycle timeout"
                    ) from exc
            if supervisor.returncode != 0:
                raise QualificationBlocked(
                    f"canonical qualification shell failed rc={supervisor.returncode}"
                )
            if lifecycle is None:
                raise QualificationBlocked(
                    "production lifecycle observer was not created"
                )
            lifecycle.observe_canonical_shell_exit(
                supervisor.returncode,
                contract_path,
            )
            live_result = read_strict_json(
                live_result_path,
                role="qualification live result",
            )
            endpoint_evidence = endpoints.evidence()
    except QualificationError as exc:
        blocker = str(exc)
    finally:
        _terminate_supervisor(supervisor)
        if endpoint_simulator is not None and not endpoint_evidence:
            endpoint_evidence = endpoint_simulator.evidence()

    if binding is None:
        binding = capture_content_binding(
            root,
            manifest_sha256=release.manifest_sha256,
            environment=runtime_environment,
            release_identity=binding_release,
        )
    endpoint_evidence_path = run_root / "endpoint_evidence.json"
    atomic_json(endpoint_evidence_path, dict(endpoint_evidence))
    trials = sorted(campaign_root.rglob("immutable_trial_bundle.json"))
    endpoint_events = endpoint_evidence.get("events", [])
    arm_events = [
        event
        for event in endpoint_events
        if isinstance(event, Mapping) and event.get("event") == "tp_arm_acknowledged"
    ]
    trial_events = [
        event
        for event in endpoint_events
        if isinstance(event, Mapping) and event.get("event") == "tp_trial_completed"
    ]
    if blocker is None:
        try:
            if lifecycle is None:
                raise QualificationError("production lifecycle observer was not created")
            if len(arm_events) < 2 or not trial_events or not trials:
                raise QualificationError("production lifecycle evidence is incomplete")
            lifecycle.observe_endpoint_arm_ack(arm_events[0])
            lifecycle.observe_trial_bundle(trials[0], trial_events[0])
            lifecycle.observe_endpoint_arm_ack(arm_events[1])
            lifecycle.observe_campaign_outcome(
                live_result_path=live_result_path,
                endpoint_evidence_path=endpoint_evidence_path,
                preflight_path=preflight_path,
                process_log_path=supervisor_log,
                bridge_csv_path=bridge_csv_path,
            )
        except QualificationError as exc:
            blocker = str(exc)

    if lifecycle is not None:
        payload = lifecycle.result()
    else:
        payload = {
            "schema": QUALIFICATION_RESULT_SCHEMA,
            "ok": False,
            "lifecycle_complete": False,
            "state": "BLOCKED",
            "reason_code": PRODUCTION_LIFECYCLE_FAILED,
            "binding": dict(binding),
            "canonical_shell_result": None,
            "bridge": {
                "pid": None,
                "starttime": None,
                "launch_nonce": None,
                "alive": False,
                "alive_at_campaign_outcome": False,
                "ready_ref": None,
            },
            "waiting_barrier": None,
            "play_row": None,
            "play_row_sha256": None,
            "first_arm_seq": None,
            "trial_evidence_ref": None,
            "next_arm_seq": None,
            "events": [],
            "endpoint_evidence": None,
            "preflight_evidence": None,
            "process_log": None,
            "bridge_csv": None,
            "live_result": None,
            "remaining_integration_seam": blocker,
        }
    diagnostic_paths = {
        "endpoint_evidence": endpoint_evidence_path,
        "preflight_evidence": preflight_path,
        "process_log": supervisor_log,
        "bridge_csv": bridge_csv_path,
        "live_result": live_result_path,
    }
    for role, path in diagnostic_paths.items():
        if payload.get(role) is None and path.is_file() and not path.is_symlink():
            payload[role] = _reference_file(path)
    payload.update(
        {
            "started_at_unix_ns": started_at,
            "completed_at_unix_ns": time.time_ns(),
        }
    )
    if blocker is not None:
        payload.update(
            {
                "ok": False,
                "lifecycle_complete": False,
                "state": "BLOCKED",
                "reason_code": PRODUCTION_LIFECYCLE_FAILED,
                "remaining_integration_seam": blocker,
            }
        )
    else:
        try:
            validate_qualification_result(
                payload,
                experiment_root=root,
                manifest_sha256=release.manifest_sha256,
                source_fingerprint=prebinding["source"]["fingerprint"],
                launcher_sha256=prebinding["launcher"]["sha256"],
                release_identity=binding_release,
            )
        except QualificationError as exc:
            blocker = f"qualification result validation failed: {exc}"
            payload.update(
                {
                    "ok": False,
                    "lifecycle_complete": False,
                    "state": "BLOCKED",
                    "reason_code": PRODUCTION_LIFECYCLE_FAILED,
                    "remaining_integration_seam": blocker,
                }
            )
    evidence_ref = write_qualification_evidence(output, payload)
    if payload["ok"] is True:
        evidence_path = Path(evidence_ref["path"])
        atomic_json(
            current_path,
            {
                "schema": "step5d.autotune-v3/qualification-current-pointer-v1",
                "cache_key": cache_key,
                "path": str(evidence_path.relative_to(output)),
                "sha256": evidence_ref["sha256"],
            },
        )
    return payload, evidence_ref


__all__ = [
    "BRIDGE_EXITED_AFTER_FIRST_TRIAL",
    "BRIDGE_EXITED_BEFORE_FIRST_ARM_ACK",
    "BRIDGE_EXITED_DURING_FIRST_TRIAL",
    "BRIDGE_EXITED_IMMEDIATELY",
    "BRIDGE_NOT_ALIVE_AFTER_NEXT_ACK",
    "CANONICAL_LAUNCH_ENV",
    "ENDPOINT_INJECTION_UNAVAILABLE",
    "FIRST_ARM_ACK_MISSING",
    "NEXT_ARM_ACK_MISSING",
    "PROCESS_TREE_BINDING_INCOMPLETE",
    "PRODUCTION_LIFECYCLE_FAILED",
    "QUALIFIED",
    "QualificationBlocked",
    "QualificationError",
    "QualificationLifecycle",
    "QualificationPhase",
    "capture_content_binding",
    "exec_internal_shell_contract",
    "qualification_integration_blocker",
    "production_process_tree_fingerprint",
    "production_process_role_paths",
    "read_process_starttime",
    "require_canonical_launcher",
    "run_endpoint_qualification",
    "validate_content_binding",
    "validate_qualification_binding",
    "validate_qualification_result",
    "write_qualification_evidence",
]
