"""Content-addressed Step5d V3 Python runtime installation and attestation."""

from __future__ import annotations

import csv
import fcntl
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


EXPERIMENT_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = EXPERIMENT_ROOT / "config/step5/step5d_v3_runtime_contract.json"
LOCK_PATH = EXPERIMENT_ROOT / "uv.lock"
PYPROJECT_PATH = EXPERIMENT_ROOT / "pyproject.toml"
CONTRACT_SCHEMA = "step5d.autotune-v3/runtime-contract-v1"
POINTER_SCHEMA = "step5d.autotune-v3/runtime-pointer-v2"
ATTESTATION_SCHEMA = "step5d.autotune-v3/runtime-attestation-v2"
INSTALLATION_SCHEMA = "step5d.autotune-v3/runtime-installation-v1"
PROFILES = ("control", "optimizer")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NORMALIZE_RE = re.compile(r"[-_.]+")


class RuntimeInstallationError(RuntimeError):
    """A managed runtime is absent, mutable, or inconsistent with its contract."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail


@dataclass(frozen=True)
class RuntimeInputSnapshot:
    contract: Mapping[str, Any]
    contract_sha256: str
    lock_bytes: bytes
    lock_sha256: str
    pyproject_bytes: bytes
    pyproject_sha256: str


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


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha256(value: Any, role: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", f"{role} must be a lowercase SHA-256"
        )
    return value


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeInstallationError(
                "HOST_CONTRACT_MISMATCH", f"runtime JSON repeats key {key!r}"
            )
        result[key] = value
    return result


def _load_json(
    path: Path,
    *,
    role: str,
    reason_code: str = "HOST_CONTRACT_MISMATCH",
) -> Any:
    if path.is_symlink() or not path.is_file():
        raise RuntimeInstallationError(
            reason_code, f"{role} is missing or unsafe: {path}"
        )
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"forbidden JSON constant {value!r}")
            ),
        )
    except RuntimeInstallationError as exc:
        if exc.reason_code == reason_code:
            raise
        raise RuntimeInstallationError(reason_code, exc.detail) from exc
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeInstallationError(
            reason_code, f"{role} is not strict JSON: {exc}"
        ) from exc


def load_runtime_contract(path: Path = CONTRACT_PATH) -> dict[str, Any]:
    payload = _load_json(path, role="runtime contract")
    expected = {
        "schema",
        "python",
        "uv",
        "profiles",
        "host",
        "ros",
        "gpu",
        "calibration",
        "owner_dependencies",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", "runtime contract fields differ"
        )
    if payload["schema"] != CONTRACT_SCHEMA:
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", "runtime contract schema differs"
        )
    profiles = payload["profiles"]
    if not isinstance(profiles, dict) or tuple(sorted(profiles)) != PROFILES:
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", "runtime profile set differs"
        )
    for profile in PROFILES:
        row = profiles[profile]
        if not isinstance(row, dict) or set(row) != {
            "dependency_group",
            "required_distributions",
            "required_imports",
            "forbidden_imports",
        }:
            raise RuntimeInstallationError(
                "HOST_CONTRACT_MISMATCH", f"{profile} runtime contract fields differ"
            )
        if row["dependency_group"] != profile:
            raise RuntimeInstallationError(
                "HOST_CONTRACT_MISMATCH", f"{profile} dependency group differs"
            )
        distributions = row["required_distributions"]
        if not isinstance(distributions, dict) or not distributions or any(
            not isinstance(name, str)
            or not name
            or not isinstance(version, str)
            or not version
            for name, version in distributions.items()
        ):
            raise RuntimeInstallationError(
                "HOST_CONTRACT_MISMATCH", f"{profile} distributions are invalid"
            )
        normalized = [_normalize_distribution(name) for name in distributions]
        if len(normalized) != len(set(normalized)):
            raise RuntimeInstallationError(
                "HOST_CONTRACT_MISMATCH",
                f"{profile} distributions normalize to duplicate names",
            )
        for field in ("required_imports", "forbidden_imports"):
            values = row[field]
            if (
                not isinstance(values, list)
                or not values
                or any(not isinstance(value, str) or not value for value in values)
                or len(values) != len(set(values))
            ):
                raise RuntimeInstallationError(
                    "HOST_CONTRACT_MISMATCH", f"{profile} {field} is invalid"
                )
        if set(row["required_imports"]) & set(row["forbidden_imports"]):
            raise RuntimeInstallationError(
                "HOST_CONTRACT_MISMATCH",
                f"{profile} required and forbidden imports overlap",
            )

    def exact_object(value: Any, fields: set[str], role: str) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != fields:
            raise RuntimeInstallationError(
                "HOST_CONTRACT_MISMATCH", f"{role} fields differ"
            )
        return value

    def nonempty_string(value: Any, role: str) -> str:
        if not isinstance(value, str) or not value:
            raise RuntimeInstallationError(
                "HOST_CONTRACT_MISMATCH", f"{role} must be a non-empty string"
            )
        return value

    python = exact_object(
        payload["python"],
        {
            "bootstrap_executable",
            "bootstrap_sha256",
            "cache_tag",
            "soabi",
            "version",
        },
        "Python contract",
    )
    bootstrap = Path(nonempty_string(python["bootstrap_executable"], "Python executable"))
    if not bootstrap.is_absolute():
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", "Python executable must be absolute"
        )
    for field in ("cache_tag", "soabi", "version"):
        nonempty_string(python[field], f"Python {field}")
    _require_sha256(python["bootstrap_sha256"], "Python digest")

    uv = exact_object(
        payload["uv"],
        {"dependency_manifest_sha256", "executable_name", "sha256", "version"},
        "uv contract",
    )
    for field in ("executable_name", "version"):
        nonempty_string(uv[field], f"uv {field}")
    _require_sha256(uv["sha256"], "uv digest")
    _require_sha256(
        uv["dependency_manifest_sha256"],
        "dependency manifest digest",
    )

    host = exact_object(
        payload["host"],
        {
            "os_id",
            "os_version_id",
            "minimum_memlock_bytes",
            "minimum_rtprio",
            "required_available_cpus",
            "sched_rt_runtime_us",
            "kernel_release",
        },
        "host contract",
    )
    for field in ("os_id", "os_version_id", "kernel_release"):
        nonempty_string(host[field], f"host {field}")
    for field in (
        "minimum_memlock_bytes",
        "minimum_rtprio",
        "sched_rt_runtime_us",
    ):
        if isinstance(host[field], bool) or not isinstance(host[field], int) or host[field] < 0:
            raise RuntimeInstallationError(
                "HOST_CONTRACT_MISMATCH", f"host {field} must be non-negative"
            )
    cpus = host["required_available_cpus"]
    if (
        not isinstance(cpus, list)
        or not cpus
        or any(isinstance(cpu, bool) or not isinstance(cpu, int) or cpu < 0 for cpu in cpus)
        or len(cpus) != len(set(cpus))
    ):
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", "host CPU set is invalid"
        )

    ros = exact_object(
        payload["ros"],
        {"distro", "prefix", "packages", "xacro_path", "xacro_sha256"},
        "ROS contract",
    )
    for field in ("distro", "prefix", "xacro_path"):
        nonempty_string(ros[field], f"ROS {field}")
    if not Path(ros["prefix"]).is_absolute() or not Path(ros["xacro_path"]).is_absolute():
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", "ROS paths must be absolute"
        )
    packages = ros["packages"]
    if not isinstance(packages, dict) or not packages or any(
        not isinstance(name, str)
        or not name
        or not isinstance(version, str)
        or not version
        for name, version in packages.items()
    ):
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", "ROS package contract is invalid"
        )
    _require_sha256(ros["xacro_sha256"], "xacro digest")

    gpu = exact_object(
        payload["gpu"],
        {"compute_capability", "driver_version", "name", "uuid"},
        "GPU contract",
    )
    for field in ("compute_capability", "driver_version", "name", "uuid"):
        nonempty_string(gpu[field], f"GPU {field}")
    if not gpu["uuid"].startswith("GPU-"):
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", "GPU UUID is invalid"
        )

    calibration = exact_object(
        payload["calibration"],
        {"artifact_path", "artifact_sha256", "yaml_sha256"},
        "calibration contract",
    )
    artifact_path = Path(
        nonempty_string(calibration["artifact_path"], "calibration artifact path")
    )
    if artifact_path.is_absolute() or ".." in artifact_path.parts:
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", "calibration artifact path is unsafe"
        )
    _require_sha256(calibration["artifact_sha256"], "calibration artifact digest")
    _require_sha256(calibration["yaml_sha256"], "calibration YAML digest")

    owners = exact_object(
        payload["owner_dependencies"],
        {"controller_helper"},
        "owner dependency contract",
    )
    helper = exact_object(
        owners["controller_helper"],
        {"owner_id", "sha256"},
        "controller helper contract",
    )
    nonempty_string(helper["owner_id"], "controller helper owner ID")
    _require_sha256(
        helper["sha256"],
        "controller helper digest",
    )
    return payload


def runtime_contract_sha256(path: Path = CONTRACT_PATH) -> str:
    load_runtime_contract(path)
    return _sha256_file(path)


def lock_sha256(path: Path = LOCK_PATH) -> str:
    if path.is_symlink() or not path.is_file():
        raise RuntimeInstallationError(
            "RUNTIME_LOCK_MISMATCH", f"uv lock is missing or unsafe: {path}"
        )
    return _sha256_file(path)


def dependency_manifest_sha256(path: Path = PYPROJECT_PATH) -> str:
    if path.is_symlink() or not path.is_file():
        raise RuntimeInstallationError(
            "RUNTIME_LOCK_MISMATCH",
            f"dependency manifest is missing or unsafe: {path}",
        )
    return _sha256_file(path)


def _verify_dependency_manifest(contract: Mapping[str, Any]) -> None:
    if (
        dependency_manifest_sha256()
        != contract["uv"]["dependency_manifest_sha256"]
    ):
        raise RuntimeInstallationError(
            "RUNTIME_LOCK_MISMATCH", "dependency manifest digest differs"
        )


def _capture_runtime_inputs() -> RuntimeInputSnapshot:
    for path, role in (
        (CONTRACT_PATH, "runtime contract"),
        (LOCK_PATH, "uv lock"),
        (PYPROJECT_PATH, "dependency manifest"),
    ):
        if path.is_symlink() or not path.is_file():
            raise RuntimeInstallationError(
                "RUNTIME_LOCK_MISMATCH", f"{role} is missing or unsafe: {path}"
            )
    try:
        contract_before = CONTRACT_PATH.read_bytes()
        lock_bytes = LOCK_PATH.read_bytes()
        pyproject_bytes = PYPROJECT_PATH.read_bytes()
    except OSError as exc:
        raise RuntimeInstallationError(
            "RUNTIME_LOCK_MISMATCH", f"runtime input is unreadable: {exc}"
        ) from exc
    contract = load_runtime_contract()
    try:
        contract_after = CONTRACT_PATH.read_bytes()
    except OSError as exc:
        raise RuntimeInstallationError(
            "RUNTIME_LOCK_MISMATCH", f"runtime contract changed while reading: {exc}"
        ) from exc
    if contract_before != contract_after:
        raise RuntimeInstallationError(
            "RUNTIME_LOCK_MISMATCH", "runtime contract changed while reading"
        )
    pyproject_digest = _sha256_bytes(pyproject_bytes)
    if contract["uv"]["dependency_manifest_sha256"] != pyproject_digest:
        raise RuntimeInstallationError(
            "RUNTIME_LOCK_MISMATCH", "dependency manifest digest differs"
        )
    return RuntimeInputSnapshot(
        contract=contract,
        contract_sha256=_sha256_bytes(contract_before),
        lock_bytes=lock_bytes,
        lock_sha256=_sha256_bytes(lock_bytes),
        pyproject_bytes=pyproject_bytes,
        pyproject_sha256=pyproject_digest,
    )


def _assert_runtime_inputs_unchanged(snapshot: RuntimeInputSnapshot) -> None:
    try:
        current_contract = _sha256_file(CONTRACT_PATH)
        current_lock = _sha256_file(LOCK_PATH)
        current_pyproject = _sha256_file(PYPROJECT_PATH)
    except OSError as exc:
        raise RuntimeInstallationError(
            "RUNTIME_LOCK_MISMATCH", f"runtime input became unreadable: {exc}"
        ) from exc
    if (
        current_contract != snapshot.contract_sha256
        or current_lock != snapshot.lock_sha256
        or current_pyproject != snapshot.pyproject_sha256
    ):
        raise RuntimeInstallationError(
            "RUNTIME_LOCK_MISMATCH", "runtime inputs changed during provisioning"
        )


def _normalize_distribution(name: str) -> str:
    return _NORMALIZE_RE.sub("-", name).lower()


def _data_home(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get("XDG_DATA_HOME")
    path = Path(configured) if configured else Path.home() / ".local/share"
    if not path.is_absolute():
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", "XDG_DATA_HOME must be absolute"
        )
    return path.resolve()


def _state_home(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get("XDG_STATE_HOME")
    path = Path(configured) if configured else Path.home() / ".local/state"
    if not path.is_absolute():
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", "XDG_STATE_HOME must be absolute"
        )
    return path.resolve()


def _cache_home(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get("XDG_CACHE_HOME")
    path = Path(configured) if configured else Path.home() / ".cache"
    if not path.is_absolute():
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", "XDG_CACHE_HOME must be absolute"
        )
    return path.resolve()


def runtime_store(environ: Mapping[str, str] | None = None) -> Path:
    return _data_home(environ) / "step5d-autotune-v3/runtimes"


def current_pointer_path(environ: Mapping[str, str] | None = None) -> Path:
    return _data_home(environ) / "step5d-autotune-v3/current-runtime.json"


def runtime_cache_root(environ: Mapping[str, str] | None = None) -> Path:
    return _state_home(environ) / "step5d-autotune-v3"


def runtime_profile_cache_root(environ: Mapping[str, str] | None = None) -> Path:
    return _cache_home(environ) / "step5d-autotune-v3/profiles"


def runtime_download_cache(environ: Mapping[str, str] | None = None) -> Path:
    return _cache_home(environ) / "step5d-autotune-v3/uv"


def runtime_bundle_id(
    contract: Mapping[str, Any] | None = None,
    *,
    contract_sha256_value: str | None = None,
    lock_sha256_value: str | None = None,
) -> str:
    values = load_runtime_contract() if contract is None else contract
    _verify_dependency_manifest(values)
    material = {
        "schema": "step5d.autotune-v3/runtime-bundle-identity-v1",
        "contract_sha256": contract_sha256_value or runtime_contract_sha256(),
        "lock_sha256": lock_sha256_value or lock_sha256(),
        "python_abi": values["python"]["soabi"],
        "profiles": list(PROFILES),
    }
    return _sha256_bytes(_canonical_bytes(material))


def _snapshot_bundle_id(snapshot: RuntimeInputSnapshot) -> str:
    return runtime_bundle_id(
        snapshot.contract,
        contract_sha256_value=snapshot.contract_sha256,
        lock_sha256_value=snapshot.lock_sha256,
    )


def profile_environment_id(
    profile: str,
    contract: Mapping[str, Any] | None = None,
    *,
    contract_sha256_value: str | None = None,
    lock_sha256_value: str | None = None,
) -> str:
    if profile not in PROFILES:
        raise RuntimeInstallationError(
            "RUNTIME_NOT_PROVISIONED", f"unknown runtime profile {profile!r}"
        )
    values = load_runtime_contract() if contract is None else contract
    _verify_dependency_manifest(values)
    material = {
        "schema": "step5d.autotune-v3/runtime-environment-identity-v1",
        "contract_sha256": contract_sha256_value or runtime_contract_sha256(),
        "lock_sha256": lock_sha256_value or lock_sha256(),
        "profile": profile,
        "python_abi": values["python"]["soabi"],
    }
    return _sha256_bytes(_canonical_bytes(material))


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
    timeout: float = 120.0,
    reason_code: str = "HOST_CONTRACT_MISMATCH",
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=None if environment is None else dict(environment),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeInstallationError(
            reason_code, f"command could not run: {command[0]}: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeInstallationError(
            reason_code,
            f"command failed ({completed.returncode}): {command[0]}: {detail}",
        )
    return completed


def _read_os_release() -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        rows[key] = value.strip().strip('"')
    return rows


def _limit_value(value: int) -> int:
    return sys.maxsize if value == resource.RLIM_INFINITY else int(value)


def _observe_host(
    contract: Mapping[str, Any],
    *,
    uv_executable: Path,
    controller_helper: Path,
) -> dict[str, Any]:
    python = Path(contract["python"]["bootstrap_executable"])
    host_environment = {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
    }
    if uv_executable.expanduser().is_symlink():
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", f"uv must not be a symlink: {uv_executable}"
        )
    if controller_helper.expanduser().is_symlink():
        raise RuntimeInstallationError(
            "OWNER_DEPENDENCY_MISMATCH",
            f"controller helper must not be a symlink: {controller_helper}",
        )
    try:
        uv_path = uv_executable.expanduser().resolve(strict=True)
    except OSError as exc:
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", f"uv is unavailable: {uv_executable}"
        ) from exc
    try:
        helper = controller_helper.expanduser().resolve(strict=True)
    except OSError as exc:
        raise RuntimeInstallationError(
            "OWNER_DEPENDENCY_MISMATCH",
            f"controller helper is unavailable: {controller_helper}",
        ) from exc
    for path, expected, role in (
        (python, contract["python"]["bootstrap_sha256"], "bootstrap Python"),
        (uv_path, contract["uv"]["sha256"], "uv"),
        (
            helper,
            contract["owner_dependencies"]["controller_helper"]["sha256"],
            "controller helper",
        ),
    ):
        if path.is_symlink() or not path.is_file() or _sha256_file(path) != expected:
            reason = (
                "OWNER_DEPENDENCY_MISMATCH"
                if role == "controller helper"
                else "HOST_CONTRACT_MISMATCH"
            )
            raise RuntimeInstallationError(reason, f"{role} identity differs: {path}")

    python_probe = _run(
        [
            str(python),
            "-B",
            "-I",
            "-S",
            "-c",
            (
                "import json,sys,sysconfig;"
                "print(json.dumps({'version':'.'.join(map(str,sys.version_info[:3])),"
                "'cache_tag':sys.implementation.cache_tag,'soabi':sysconfig.get_config_var('SOABI')}))"
            ),
        ],
        environment=host_environment,
    )
    python_observation = json.loads(python_probe.stdout)
    for field in ("version", "cache_tag", "soabi"):
        if python_observation[field] != contract["python"][field]:
            raise RuntimeInstallationError(
                "HOST_CONTRACT_MISMATCH", f"Python {field} differs"
            )

    uv_version = _run(
        [str(uv_path), "--version"], environment=host_environment
    ).stdout.strip()
    if uv_version != f"uv {contract['uv']['version']}":
        raise RuntimeInstallationError("HOST_CONTRACT_MISMATCH", "uv version differs")

    os_release = _read_os_release()
    if (
        os_release.get("ID") != contract["host"]["os_id"]
        or os_release.get("VERSION_ID") != contract["host"]["os_version_id"]
        or platform.release() != contract["host"]["kernel_release"]
    ):
        raise RuntimeInstallationError("HOST_CONTRACT_MISMATCH", "OS or kernel differs")

    rtprio = _limit_value(resource.getrlimit(resource.RLIMIT_RTPRIO)[1])
    memlock = _limit_value(resource.getrlimit(resource.RLIMIT_MEMLOCK)[1])
    affinity = sorted(os.sched_getaffinity(0))
    required_cpus = set(contract["host"]["required_available_cpus"])
    sched_runtime = int(
        Path("/proc/sys/kernel/sched_rt_runtime_us").read_text(encoding="ascii").strip()
    )
    if (
        rtprio < contract["host"]["minimum_rtprio"]
        or memlock < contract["host"]["minimum_memlock_bytes"]
        or not required_cpus.issubset(affinity)
        or sched_runtime != contract["host"]["sched_rt_runtime_us"]
    ):
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", "realtime resource contract differs"
        )

    packages: dict[str, str] = {}
    for name, expected in sorted(contract["ros"]["packages"].items()):
        observed = _run(
            ["/usr/bin/dpkg-query", "-W", "-f=${Version}", name],
            environment=host_environment,
        ).stdout
        if observed != expected:
            raise RuntimeInstallationError(
                "HOST_CONTRACT_MISMATCH", f"ROS package differs: {name}"
            )
        packages[name] = observed
    xacro = Path(contract["ros"]["xacro_path"])
    if xacro.is_symlink() or not xacro.is_file() or _sha256_file(xacro) != contract["ros"]["xacro_sha256"]:
        raise RuntimeInstallationError("HOST_CONTRACT_MISMATCH", "UR xacro differs")

    calibration_path = EXPERIMENT_ROOT / contract["calibration"]["artifact_path"]
    if _sha256_file(calibration_path) != contract["calibration"]["artifact_sha256"]:
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", "runtime calibration artifact differs"
        )
    calibration = _load_json(calibration_path, role="runtime calibration")
    yaml_relative = Path(calibration["calibrated_model"]["calibration_yaml"])
    calibration_yaml = (EXPERIMENT_ROOT / yaml_relative).resolve(strict=True)
    if _sha256_file(calibration_yaml) != contract["calibration"]["yaml_sha256"]:
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", "installed calibration YAML differs"
        )

    gpu_output = _run(
        [
            "/usr/bin/nvidia-smi",
            "--query-gpu=uuid,name,compute_cap,driver_version",
            "--format=csv,noheader,nounits",
        ],
        environment=host_environment,
        reason_code="GPU_IDENTITY_MISMATCH",
    ).stdout
    rows = list(csv.reader(gpu_output.splitlines(), skipinitialspace=True))
    expected_gpu = contract["gpu"]
    matched = [
        row
        for row in rows
        if len(row) == 4 and row[0].strip() == expected_gpu["uuid"]
    ]
    if len(matched) != 1:
        raise RuntimeInstallationError("GPU_IDENTITY_MISMATCH", "governed GPU is absent")
    gpu = {
        "uuid": matched[0][0].strip(),
        "name": matched[0][1].strip(),
        "compute_capability": matched[0][2].strip(),
        "driver_version": matched[0][3].strip(),
    }
    if gpu != expected_gpu:
        raise RuntimeInstallationError("GPU_IDENTITY_MISMATCH", "governed GPU differs")

    return {
        "schema": "step5d.autotune-v3/host-observation-v1",
        "observed_at_unix_ns": time.time_ns(),
        "python": {**python_observation, "executable": str(python), "sha256": _sha256_file(python)},
        "uv": {"executable": str(uv_path), "version": contract["uv"]["version"], "sha256": _sha256_file(uv_path)},
        "os": {"id": os_release["ID"], "version_id": os_release["VERSION_ID"], "kernel_release": platform.release()},
        "realtime": {"rtprio_hard": rtprio, "memlock_hard_bytes": memlock, "affinity": affinity, "sched_rt_runtime_us": sched_runtime},
        "ros": {"distro": contract["ros"]["distro"], "packages": packages, "xacro_path": str(xacro), "xacro_sha256": _sha256_file(xacro)},
        "calibration": {"artifact_path": str(calibration_path), "artifact_sha256": _sha256_file(calibration_path), "yaml_path": str(calibration_yaml), "yaml_sha256": _sha256_file(calibration_yaml)},
        "gpu": gpu,
        "owner_dependencies": {
            "controller_helper": {
                "owner_id": contract["owner_dependencies"]["controller_helper"]["owner_id"],
                "path": str(helper),
                "sha256": _sha256_file(helper),
            }
        },
    }


def observe_host(
    contract: Mapping[str, Any],
    *,
    uv_executable: Path,
    controller_helper: Path,
) -> dict[str, Any]:
    try:
        return _observe_host(
            contract,
            uv_executable=uv_executable,
            controller_helper=controller_helper,
        )
    except RuntimeInstallationError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", f"host observation failed: {exc}"
        ) from exc


_PROFILE_PROBE = r"""
import importlib
import importlib.metadata
import importlib.util
import json
import os
import pathlib
import site
import sys
import sysconfig

contract = json.loads(sys.argv[1])
required = {}
for name in contract["required_imports"]:
    module = importlib.import_module(name)
    required[name] = str(pathlib.Path(module.__file__).resolve()) if getattr(module, "__file__", None) else None
for name in contract["forbidden_imports"]:
    if importlib.util.find_spec(name) is not None:
        raise SystemExit(f"forbidden import is visible: {name}")
versions = {name: importlib.metadata.version(name) for name in contract["required_distributions"]}
print(json.dumps({
    "python_executable": os.path.abspath(sys.executable),
    "python_binary": str(pathlib.Path(sys.executable).resolve()),
    "python_version": ".".join(map(str, sys.version_info[:3])),
    "prefix": str(pathlib.Path(sys.prefix).resolve()),
    "purelib": str(pathlib.Path(sysconfig.get_paths()["purelib"]).resolve()),
    "enable_user_site": bool(site.ENABLE_USER_SITE),
    "required_imports": required,
    "required_distributions": versions,
}, sort_keys=True))
"""


def _profile_environment(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache = home / "cache"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "XDG_CACHE_HOME": str(cache),
    }


def _record_tree(environment_root: Path, purelib: Path) -> tuple[str, int, dict[str, str]]:
    distributions: dict[str, tuple[str, Path]] = {}
    duplicates: set[str] = set()
    for distribution in importlib.metadata.distributions(path=[str(purelib)]):
        raw_name = distribution.metadata.get("Name")
        if not isinstance(raw_name, str) or not raw_name:
            raise RuntimeInstallationError(
                "RUNTIME_PACKAGE_INTEGRITY_MISMATCH", "distribution lacks a name"
            )
        name = _normalize_distribution(raw_name)
        if name in distributions:
            duplicates.add(name)
        distributions[name] = (distribution.version, Path(distribution._path))
    if duplicates:
        raise RuntimeInstallationError(
            "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
            f"duplicate distributions: {sorted(duplicates)}",
        )

    material: list[dict[str, str]] = []
    for name, (version, metadata_path) in sorted(distributions.items()):
        distribution = importlib.metadata.PathDistribution(metadata_path)
        files = distribution.files
        if files is None:
            raise RuntimeInstallationError(
                "RUNTIME_PACKAGE_INTEGRITY_MISMATCH", f"{name} lacks RECORD files"
            )
        for item in sorted(files, key=str):
            logical_path = Path(distribution.locate_file(item)).absolute()
            if logical_path.suffix == ".pyc" or "__pycache__" in logical_path.parts:
                raise RuntimeInstallationError(
                    "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
                    f"{name} RECORD contains unauthenticated bytecode: {item}",
                )
            path = logical_path.resolve()
            try:
                logical_relative = logical_path.relative_to(environment_root.resolve())
                resolved_relative = path.relative_to(environment_root.resolve())
            except ValueError as exc:
                raise RuntimeInstallationError(
                    "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
                    f"{name} RECORD escapes the environment: {item}",
                ) from exc
            if not path.is_file():
                raise RuntimeInstallationError(
                    "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
                    f"{name} RECORD entry is missing or unsafe: {item}",
                )
            material.append(
                {
                    "distribution": name,
                    "path": logical_relative.as_posix(),
                    "resolved_path": resolved_relative.as_posix(),
                    "sha256": _sha256_file(path),
                }
            )
    return (
        _sha256_bytes(_canonical_bytes(material)),
        len(material),
        {name: version for name, (version, _) in sorted(distributions.items())},
    )


def _reject_untracked_bytecode(root: Path) -> None:
    for path in root.rglob("*"):
        if path.suffix == ".pyc" or "__pycache__" in path.parts:
            raise RuntimeInstallationError(
                "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
                f"runtime contains unauthenticated bytecode: {path}",
            )


def _filesystem_tree(root: Path) -> tuple[str, int]:
    material: list[dict[str, Any]] = []
    for path in sorted([root, *root.rglob("*")], key=lambda item: item.as_posix()):
        relative = "." if path == root else path.relative_to(root).as_posix()
        metadata = path.lstat()
        row: dict[str, Any] = {
            "path": relative,
            "mode": stat.S_IMODE(metadata.st_mode),
        }
        if stat.S_ISLNK(metadata.st_mode):
            row.update({"kind": "symlink", "target": os.readlink(path)})
        elif stat.S_ISDIR(metadata.st_mode):
            row["kind"] = "directory"
        elif stat.S_ISREG(metadata.st_mode):
            row.update({"kind": "file", "sha256": _sha256_file(path)})
        else:
            raise RuntimeInstallationError(
                "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
                f"runtime contains unsupported filesystem entry: {path}",
            )
        material.append(row)
    return _sha256_bytes(_canonical_bytes(material)), len(material)


def observe_profile(
    environment_root: Path,
    profile: str,
    contract: Mapping[str, Any],
    *,
    cache_home: Path,
    contract_sha256_value: str | None = None,
    lock_sha256_value: str | None = None,
) -> dict[str, Any]:
    reason = "CONTROL_RUNTIME_INVALID" if profile == "control" else "OPTIMIZER_RUNTIME_INVALID"
    if profile not in PROFILES:
        raise RuntimeInstallationError(reason, f"unknown runtime profile {profile!r}")
    try:
        root = environment_root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeInstallationError(
            reason, f"{profile} runtime root is unavailable: {environment_root}"
        ) from exc
    python = root / "bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeInstallationError(reason, f"{profile} interpreter is missing or unsafe")
    profile_contract = contract["profiles"][profile]
    try:
        completed = _run(
            [
                str(python),
                "-B",
                "-I",
                "-c",
                _PROFILE_PROBE,
                json.dumps(profile_contract, sort_keys=True),
            ],
            environment=_profile_environment(cache_home),
            timeout=180.0,
            reason_code=reason,
        )
        observed = json.loads(completed.stdout)
    except (json.JSONDecodeError, RuntimeInstallationError) as exc:
        if isinstance(exc, RuntimeInstallationError) and exc.reason_code == reason:
            raise
        detail = exc.detail if isinstance(exc, RuntimeInstallationError) else str(exc)
        raise RuntimeInstallationError(reason, f"{profile} import probe failed: {detail}") from exc
    if observed.get("required_distributions") != profile_contract["required_distributions"]:
        raise RuntimeInstallationError(reason, f"{profile} distribution versions differ")
    if observed.get("enable_user_site") is not False:
        raise RuntimeInstallationError(reason, f"{profile} user-site is enabled")
    purelib = Path(observed["purelib"])
    if Path(observed.get("prefix", "")) != root:
        raise RuntimeInstallationError(reason, f"{profile} interpreter prefix differs")
    try:
        purelib.relative_to(root)
    except ValueError as exc:
        raise RuntimeInstallationError(reason, f"{profile} purelib escapes the runtime") from exc
    for name, raw_path in observed.get("required_imports", {}).items():
        if raw_path is None:
            continue
        try:
            Path(raw_path).relative_to(root)
        except ValueError as exc:
            raise RuntimeInstallationError(
                reason, f"{profile} import {name!r} escapes the runtime"
            ) from exc
    _reject_untracked_bytecode(root)
    record_sha256, record_count, all_distributions = _record_tree(root, purelib)
    tree_sha256, tree_entry_count = _filesystem_tree(root)
    return {
        "schema": "step5d.autotune-v3/runtime-profile-observation-v1",
        "profile": profile,
        "environment_id": profile_environment_id(
            profile,
            contract,
            contract_sha256_value=contract_sha256_value,
            lock_sha256_value=lock_sha256_value,
        ),
        "root": str(root),
        "python_executable": observed["python_executable"],
        "python_binary": observed["python_binary"],
        "python_sha256": _sha256_file(python),
        "python_version": observed["python_version"],
        "prefix": observed["prefix"],
        "purelib": observed["purelib"],
        "required_imports": observed["required_imports"],
        "required_distributions": observed["required_distributions"],
        "all_distributions": all_distributions,
        "record_tree_sha256": record_sha256,
        "record_file_count": record_count,
        "profile_tree_sha256": tree_sha256,
        "profile_tree_entry_count": tree_entry_count,
    }


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return _sha256_bytes(encoded.encode("utf-8"))


def _make_read_only(root: Path, *, bootstrap_python: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            target = path.resolve(strict=True)
            if target != bootstrap_python.resolve(strict=True) and not target.is_relative_to(root):
                raise RuntimeInstallationError(
                    "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
                    f"runtime symlink escapes its bundle: {path}",
                )
            continue
        mode = path.stat().st_mode
        if path.is_dir():
            path.chmod(0o555)
        elif path.is_file():
            executable = bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
            path.chmod(0o555 if executable else 0o444)
    root.chmod(0o555)


def _verify_read_only(root: Path, *, bootstrap_python: Path) -> None:
    paths = [root, *root.rglob("*")]
    for path in paths:
        if path.lstat().st_uid != os.geteuid():
            raise RuntimeInstallationError(
                "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
                f"runtime path owner differs: {path}",
            )
        if path.is_symlink():
            try:
                target = path.resolve(strict=True)
            except OSError as exc:
                raise RuntimeInstallationError(
                    "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
                    f"runtime symlink is broken: {path}",
                ) from exc
            if target != bootstrap_python.resolve(strict=True) and not target.is_relative_to(root):
                raise RuntimeInstallationError(
                    "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
                    f"runtime symlink escapes its bundle: {path}",
                )
            continue
        if path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeInstallationError(
                "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
                f"runtime path is writable: {path}",
            )


def _provision_environment(
    destination: Path,
    profile: str,
    *,
    contract: Mapping[str, Any],
    uv_executable: Path,
    cache_root: Path,
    uv_cache: Path,
    project_root: Path,
) -> None:
    bootstrap = contract["python"]["bootstrap_executable"]
    base_environment = _profile_environment(cache_root / "provision-home")
    Path(base_environment["HOME"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    uv_cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    base_environment["UV_CACHE_DIR"] = str(uv_cache)
    _run(
        [
            str(uv_executable),
            "--no-config",
            "venv",
            "--relocatable",
            "--python",
            bootstrap,
            "--no-managed-python",
            "--no-python-downloads",
            str(destination),
        ],
        cwd=project_root,
        environment=base_environment,
        timeout=300.0,
        reason_code=(
            "CONTROL_RUNTIME_INVALID"
            if profile == "control"
            else "OPTIMIZER_RUNTIME_INVALID"
        ),
    )
    sync_environment = {
        **base_environment,
        "VIRTUAL_ENV": str(destination),
        "UV_PROJECT_ENVIRONMENT": str(destination),
    }
    _run(
        [
            str(uv_executable),
            "--no-config",
            "sync",
            "--active",
            "--frozen",
            "--only-group",
            profile,
            "--link-mode",
            "copy",
            "--no-install-project",
            "--no-build",
            "--no-managed-python",
            "--no-python-downloads",
        ],
        cwd=project_root,
        environment=sync_environment,
        timeout=1800.0,
        reason_code=(
            "CONTROL_RUNTIME_INVALID"
            if profile == "control"
            else "OPTIMIZER_RUNTIME_INVALID"
        ),
    )


def _installation_profile(observation: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(observation["root"])
    purelib = Path(observation["purelib"])
    imports: dict[str, str | None] = {}
    for name, raw_path in observation["required_imports"].items():
        imports[name] = (
            None if raw_path is None else Path(raw_path).relative_to(root).as_posix()
        )
    return {
        "schema": "step5d.autotune-v3/runtime-installed-profile-v1",
        "profile": observation["profile"],
        "environment_id": observation["environment_id"],
        "python_sha256": observation["python_sha256"],
        "python_version": observation["python_version"],
        "purelib": purelib.relative_to(root).as_posix(),
        "required_imports": imports,
        "required_distributions": observation["required_distributions"],
        "all_distributions": observation["all_distributions"],
        "record_tree_sha256": observation["record_tree_sha256"],
        "record_file_count": observation["record_file_count"],
        "profile_tree_sha256": observation["profile_tree_sha256"],
        "profile_tree_entry_count": observation["profile_tree_entry_count"],
    }


def _installation_manifest(
    bundle_id: str,
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    snapshot: RuntimeInputSnapshot,
) -> dict[str, Any]:
    return {
        "schema": INSTALLATION_SCHEMA,
        "bundle_id": bundle_id,
        "contract_sha256": snapshot.contract_sha256,
        "lock_sha256": snapshot.lock_sha256,
        "profiles": {
            profile: _installation_profile(profiles[profile])
            for profile in PROFILES
        },
    }


def _static_profile_integrity(
    root: Path,
    profile: str,
    *,
    snapshot: RuntimeInputSnapshot,
    expected: Mapping[str, Any],
) -> None:
    contract = snapshot.contract
    reason = "CONTROL_RUNTIME_INVALID" if profile == "control" else "OPTIMIZER_RUNTIME_INVALID"
    if root.is_symlink() or not root.is_dir():
        raise RuntimeInstallationError(reason, f"{profile} runtime root is unsafe")
    python = root / "bin/python"
    bootstrap = Path(contract["python"]["bootstrap_executable"])
    if (
        not python.is_symlink()
        or python.resolve(strict=True) != bootstrap.resolve(strict=True)
        or _sha256_file(python) != contract["python"]["bootstrap_sha256"]
    ):
        raise RuntimeInstallationError(reason, f"{profile} interpreter topology differs")
    pyvenv = root / "pyvenv.cfg"
    if pyvenv.is_symlink() or not pyvenv.is_file():
        raise RuntimeInstallationError(reason, f"{profile} pyvenv.cfg is missing")
    values: dict[str, str] = {}
    try:
        for line in pyvenv.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            key = key.strip()
            if separator != "=" or not key or key in values:
                raise ValueError("invalid or duplicate pyvenv.cfg entry")
            values[key] = value.strip()
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeInstallationError(reason, f"{profile} pyvenv.cfg is invalid") from exc
    expected_pyvenv = {
        "home": str(bootstrap.parent),
        "implementation": "CPython",
        "uv": contract["uv"]["version"],
        "version_info": contract["python"]["version"],
        "include-system-site-packages": "false",
        "relocatable": "true",
    }
    if values != expected_pyvenv:
        raise RuntimeInstallationError(reason, f"{profile} pyvenv.cfg differs")
    expected_fields = {
        "schema",
        "profile",
        "environment_id",
        "python_sha256",
        "python_version",
        "purelib",
        "required_imports",
        "required_distributions",
        "all_distributions",
        "record_tree_sha256",
        "record_file_count",
        "profile_tree_sha256",
        "profile_tree_entry_count",
    }
    if (
        not isinstance(expected, Mapping)
        or set(expected) != expected_fields
        or expected.get("schema")
        != "step5d.autotune-v3/runtime-installed-profile-v1"
        or expected.get("profile") != profile
        or expected.get("environment_id")
        != profile_environment_id(
            profile,
            contract,
            contract_sha256_value=snapshot.contract_sha256,
            lock_sha256_value=snapshot.lock_sha256,
        )
        or expected.get("python_sha256") != contract["python"]["bootstrap_sha256"]
        or expected.get("python_version") != contract["python"]["version"]
        or not isinstance(expected.get("required_imports"), Mapping)
    ):
        raise RuntimeInstallationError(reason, f"{profile} installation binding differs")
    abi = ".".join(contract["python"]["version"].split(".")[:2])
    purelib = root / f"lib/python{abi}/site-packages"
    if expected.get("purelib") != purelib.relative_to(root).as_posix():
        raise RuntimeInstallationError(reason, f"{profile} purelib binding differs")
    record_sha256, record_count, distributions = _record_tree(root, purelib)
    tree_sha256, tree_entry_count = _filesystem_tree(root)
    if (
        expected.get("record_tree_sha256") != record_sha256
        or expected.get("record_file_count") != record_count
        or expected.get("all_distributions") != distributions
        or expected.get("required_distributions")
        != contract["profiles"][profile]["required_distributions"]
        or expected.get("profile_tree_sha256") != tree_sha256
        or expected.get("profile_tree_entry_count") != tree_entry_count
    ):
        raise RuntimeInstallationError(
            "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
            f"{profile} static package integrity differs",
        )


def _verified_installation_manifest(
    final: Path,
    *,
    snapshot: RuntimeInputSnapshot,
    installation_manifest_sha256: str,
) -> dict[str, Any]:
    contract = snapshot.contract
    manifest_path = final / "installation-manifest.json"
    if _sha256_file(manifest_path) != installation_manifest_sha256:
        raise RuntimeInstallationError(
            "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
            "runtime installation manifest digest differs",
        )
    manifest = _load_json(
        manifest_path,
        role="runtime installation manifest",
        reason_code="RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
    )
    expected_fields = {
        "schema",
        "bundle_id",
        "contract_sha256",
        "lock_sha256",
        "profiles",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != expected_fields
        or manifest.get("schema") != INSTALLATION_SCHEMA
        or manifest.get("bundle_id") != _snapshot_bundle_id(snapshot)
        or manifest.get("contract_sha256") != snapshot.contract_sha256
        or manifest.get("lock_sha256") != snapshot.lock_sha256
        or not isinstance(manifest.get("profiles"), dict)
        or tuple(sorted(manifest["profiles"])) != PROFILES
    ):
        raise RuntimeInstallationError(
            "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
            "runtime installation manifest fields or bindings differ",
        )
    _verify_read_only(
        final,
        bootstrap_python=Path(contract["python"]["bootstrap_executable"]),
    )
    _reject_untracked_bytecode(final)
    return manifest


def _installed_profiles(
    final: Path,
    *,
    snapshot: RuntimeInputSnapshot,
    environ: Mapping[str, str] | None,
    installation_manifest_sha256: str,
) -> dict[str, dict[str, Any]]:
    contract = snapshot.contract
    manifest = _verified_installation_manifest(
        final,
        snapshot=snapshot,
        installation_manifest_sha256=installation_manifest_sha256,
    )
    for profile in PROFILES:
        _static_profile_integrity(
            final / profile,
            profile,
            snapshot=snapshot,
            expected=manifest["profiles"][profile],
        )
    cache_root = runtime_profile_cache_root(environ) / manifest["bundle_id"]
    observations: dict[str, dict[str, Any]] = {}
    for profile in PROFILES:
        observed = observe_profile(
            final / profile,
            profile,
            contract,
            cache_home=cache_root / profile,
            contract_sha256_value=snapshot.contract_sha256,
            lock_sha256_value=snapshot.lock_sha256,
        )
        if _installation_profile(observed) != manifest["profiles"][profile]:
            raise RuntimeInstallationError(
                "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
                f"{profile} runtime observation differs",
            )
        observations[profile] = observed
    return observations


def _host_identity(observation: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(observation)
    result.pop("observed_at_unix_ns", None)
    return result


def _write_runtime_attestation(
    *,
    bundle_id: str,
    snapshot: RuntimeInputSnapshot,
    host: Mapping[str, Any],
    profiles: Mapping[str, Mapping[str, Any]],
    installation_manifest_sha256: str,
    environ: Mapping[str, str] | None,
) -> tuple[Path, str]:
    observed_at = time.time_ns()
    attestation = {
        "schema": ATTESTATION_SCHEMA,
        "bundle_id": bundle_id,
        "contract_sha256": snapshot.contract_sha256,
        "lock_sha256": snapshot.lock_sha256,
        "installation_manifest_sha256": installation_manifest_sha256,
        "observed_at_unix_ns": observed_at,
        "host": host,
        "profiles": {profile: profiles[profile] for profile in PROFILES},
    }
    evidence_root = runtime_cache_root(environ) / bundle_id / "attestations"
    path = evidence_root / f"runtime-attestation-{observed_at}.json"
    digest = _atomic_json(path, attestation)
    path.chmod(0o400)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path, digest


def _pointer_payload(
    *,
    bundle_id: str,
    snapshot: RuntimeInputSnapshot,
    attestation_path: Path,
    attestation_sha256: str,
    installation_manifest_sha256: str,
    profiles: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": POINTER_SCHEMA,
        "bundle_id": bundle_id,
        "contract_sha256": snapshot.contract_sha256,
        "lock_sha256": snapshot.lock_sha256,
        "attestation_path": str(attestation_path),
        "attestation_sha256": attestation_sha256,
        "installation_manifest_sha256": installation_manifest_sha256,
        "profiles": {
            profile: {
                "environment_id": profiles[profile]["environment_id"],
                "root": profiles[profile]["root"],
                "python_executable": profiles[profile]["python_executable"],
                "record_tree_sha256": profiles[profile]["record_tree_sha256"],
                "profile_tree_sha256": profiles[profile]["profile_tree_sha256"],
            }
            for profile in PROFILES
        },
    }


def _verify_runtime_attestation(
    attestation_path: Path,
    *,
    snapshot: RuntimeInputSnapshot,
    environ: Mapping[str, str] | None,
) -> dict[str, Any]:
    contract = snapshot.contract
    attestation = _load_json(
        attestation_path,
        role="runtime attestation",
        reason_code="RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
    )
    expected_fields = {
        "schema",
        "bundle_id",
        "contract_sha256",
        "lock_sha256",
        "installation_manifest_sha256",
        "observed_at_unix_ns",
        "host",
        "profiles",
    }
    if (
        not isinstance(attestation, dict)
        or set(attestation) != expected_fields
        or attestation.get("schema") != ATTESTATION_SCHEMA
        or attestation.get("bundle_id") != _snapshot_bundle_id(snapshot)
        or attestation.get("contract_sha256") != snapshot.contract_sha256
        or attestation.get("lock_sha256") != snapshot.lock_sha256
        or not isinstance(attestation.get("observed_at_unix_ns"), int)
        or isinstance(attestation.get("observed_at_unix_ns"), bool)
        or not isinstance(attestation.get("profiles"), dict)
        or tuple(sorted(attestation["profiles"])) != PROFILES
    ):
        raise RuntimeInstallationError(
            "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
            "runtime attestation fields or bindings differ",
        )
    final = runtime_store(environ) / attestation["bundle_id"]
    profiles = _installed_profiles(
        final,
        snapshot=snapshot,
        environ=environ,
        installation_manifest_sha256=attestation[
            "installation_manifest_sha256"
        ],
    )
    for profile in PROFILES:
        if profiles[profile] != attestation["profiles"][profile]:
            raise RuntimeInstallationError(
                "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
                f"{profile} attested runtime observation differs",
            )
    host = attestation.get("host")
    try:
        uv_path = Path(host["uv"]["executable"])
        helper_path = Path(host["owner_dependencies"]["controller_helper"]["path"])
    except (KeyError, TypeError) as exc:
        raise RuntimeInstallationError(
            "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
            "runtime attestation host binding differs",
        ) from exc
    current_host = observe_host(
        contract,
        uv_executable=uv_path,
        controller_helper=helper_path,
    )
    if _host_identity(current_host) != _host_identity(host):
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", "runtime host observation differs"
        )
    return _pointer_payload(
        bundle_id=attestation["bundle_id"],
        snapshot=snapshot,
        attestation_path=attestation_path,
        attestation_sha256=_sha256_file(attestation_path),
        installation_manifest_sha256=attestation[
            "installation_manifest_sha256"
        ],
        profiles=profiles,
    )


def _restore_writable(root: Path) -> None:
    root.chmod(0o700)
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        try:
            path.chmod(0o700 if path.is_dir() else 0o600)
        except OSError:
            pass


def _fsync_tree(root: Path) -> None:
    directories: list[Path] = [root]
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            directories.append(path)
            continue
        if path.is_file():
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in sorted(
        directories, key=lambda item: len(item.parts), reverse=True
    ):
        descriptor = os.open(
            directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_pointer_document(
    path: Path,
    *,
    environ: Mapping[str, str] | None,
) -> dict[str, Any]:
    payload = _load_json(
        path,
        role="runtime pointer",
        reason_code="RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
    )
    fields = {
        "schema",
        "bundle_id",
        "contract_sha256",
        "lock_sha256",
        "attestation_path",
        "attestation_sha256",
        "installation_manifest_sha256",
        "profiles",
    }
    digest_fields = fields - {"schema", "attestation_path", "profiles"}
    if (
        not isinstance(payload, dict)
        or set(payload) != fields
        or payload.get("schema") != POINTER_SCHEMA
        or any(
            not isinstance(payload.get(field), str)
            or _SHA256_RE.fullmatch(payload[field]) is None
            for field in digest_fields
        )
        or not isinstance(payload.get("attestation_path"), str)
        or not Path(payload["attestation_path"]).is_absolute()
        or not isinstance(payload.get("profiles"), dict)
        or tuple(sorted(payload["profiles"])) != PROFILES
    ):
        raise RuntimeInstallationError(
            "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
            "runtime pointer fields or digest bindings differ",
        )
    profile_fields = {
        "environment_id",
        "root",
        "python_executable",
        "record_tree_sha256",
        "profile_tree_sha256",
    }
    attestation_path = Path(payload["attestation_path"])
    expected_attestation_root = (
        runtime_cache_root(environ) / payload["bundle_id"] / "attestations"
    )
    if attestation_path.parent != expected_attestation_root:
        raise RuntimeInstallationError(
            "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
            "runtime pointer attestation path crosses its bundle boundary",
        )
    for profile in PROFILES:
        row = payload["profiles"][profile]
        expected_root = runtime_store(environ) / payload["bundle_id"] / profile
        if (
            not isinstance(row, dict)
            or set(row) != profile_fields
            or any(
                not isinstance(row.get(field), str)
                or _SHA256_RE.fullmatch(row[field]) is None
                for field in (
                    "environment_id",
                    "record_tree_sha256",
                    "profile_tree_sha256",
                )
            )
            or not isinstance(row.get("root"), str)
            or not Path(row["root"]).is_absolute()
            or not isinstance(row.get("python_executable"), str)
            or not Path(row["python_executable"]).is_absolute()
            or row["root"] != str(expected_root)
            or row["python_executable"] != str(expected_root / "bin/python")
        ):
            raise RuntimeInstallationError(
                "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
                f"{profile} runtime pointer fields differ",
            )
    return payload


def _current_pointer_targets_bundle(
    bundle_id: str,
    *,
    environ: Mapping[str, str] | None,
) -> bool | None:
    path = current_pointer_path(environ)
    if not os.path.lexists(path):
        return None
    return _load_pointer_document(path, environ=environ)["bundle_id"] == bundle_id


def _quarantine_bundle(final: Path, *, store: Path) -> Path:
    quarantine = store / f".quarantine-{final.name}-{time.time_ns()}"
    try:
        os.replace(final, quarantine)
        _fsync_directory(store)
    except OSError as exc:
        raise RuntimeInstallationError(
            "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
            f"invalid runtime could not be quarantined: {exc}",
        ) from exc
    return quarantine


def _provision_runtime_locked(
    *,
    uv_executable: Path,
    controller_helper: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    snapshot = _capture_runtime_inputs()
    contract = snapshot.contract
    host = observe_host(
        contract,
        uv_executable=uv_executable,
        controller_helper=controller_helper,
    )
    bundle_id = _snapshot_bundle_id(snapshot)
    store = runtime_store(environ)
    cache_root = runtime_profile_cache_root(environ) / bundle_id
    uv_cache = runtime_download_cache(environ)
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    uv_cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = store / bundle_id
    if os.path.lexists(final):
        try:
            pointer = load_runtime_pointer(environ=environ)
        except RuntimeInstallationError as exc:
            try:
                current_targets_candidate = _current_pointer_targets_bundle(
                    bundle_id,
                    environ=environ,
                )
            except RuntimeInstallationError as pointer_exc:
                raise RuntimeInstallationError(
                    pointer_exc.reason_code,
                    "current runtime pointer is unsafe; existing runtime was "
                    f"preserved: {pointer_exc.detail}",
                ) from pointer_exc
            if current_targets_candidate is True:
                raise RuntimeInstallationError(
                    exc.reason_code,
                    "current runtime pointer is invalid; existing runtime was "
                    f"preserved: {exc.detail}",
                ) from exc
            _quarantine_bundle(final, store=store)
        else:
            if pointer["bundle_id"] == bundle_id:
                _assert_runtime_inputs_unchanged(snapshot)
                return pointer
            _quarantine_bundle(final, store=store)

    staging = Path(tempfile.mkdtemp(prefix=f".{bundle_id}.", dir=store))
    project_root = Path(tempfile.mkdtemp(prefix=".project.", dir=cache_root))
    try:
        (project_root / "pyproject.toml").write_bytes(snapshot.pyproject_bytes)
        (project_root / "uv.lock").write_bytes(snapshot.lock_bytes)
        check_environment = _profile_environment(cache_root / "provision-home")
        check_environment["UV_CACHE_DIR"] = str(uv_cache)
        _run(
            [
                str(uv_executable),
                "--no-config",
                "lock",
                "--check",
                "--offline",
            ],
            cwd=project_root,
            environment=check_environment,
            reason_code="RUNTIME_LOCK_MISMATCH",
        )
        staging_profiles: dict[str, dict[str, Any]] = {}
        for profile in PROFILES:
            _provision_environment(
                staging / profile,
                profile,
                contract=contract,
                uv_executable=uv_executable,
                cache_root=cache_root,
                uv_cache=uv_cache,
                project_root=project_root,
            )
        for profile in PROFILES:
            _make_read_only(
                staging / profile,
                bootstrap_python=Path(contract["python"]["bootstrap_executable"]),
            )
            staging_profiles[profile] = observe_profile(
                staging / profile,
                profile,
                contract,
                cache_home=cache_root / profile,
                contract_sha256_value=snapshot.contract_sha256,
                lock_sha256_value=snapshot.lock_sha256,
            )
        _assert_runtime_inputs_unchanged(snapshot)
        current_host = observe_host(
            contract,
            uv_executable=uv_executable,
            controller_helper=controller_helper,
        )
        if _host_identity(current_host) != _host_identity(host):
            raise RuntimeInstallationError(
                "HOST_CONTRACT_MISMATCH", "host changed during runtime provisioning"
            )
        installation_manifest_sha256 = _atomic_json(
            staging / "installation-manifest.json",
            _installation_manifest(
                bundle_id,
                staging_profiles,
                snapshot=snapshot,
            ),
        )
        _make_read_only(
            staging,
            bootstrap_python=Path(contract["python"]["bootstrap_executable"]),
        )
        _fsync_tree(staging)
        os.replace(staging, final)
        _fsync_directory(store)
        _assert_runtime_inputs_unchanged(snapshot)
        profiles = _installed_profiles(
            final,
            snapshot=snapshot,
            environ=environ,
            installation_manifest_sha256=installation_manifest_sha256,
        )
        final_host = observe_host(
            contract,
            uv_executable=uv_executable,
            controller_helper=controller_helper,
        )
        if _host_identity(final_host) != _host_identity(host):
            raise RuntimeInstallationError(
                "HOST_CONTRACT_MISMATCH", "host changed before runtime promotion"
            )
        _assert_runtime_inputs_unchanged(snapshot)
        attestation_path, attestation_sha256 = _write_runtime_attestation(
            bundle_id=bundle_id,
            snapshot=snapshot,
            host=final_host,
            profiles=profiles,
            installation_manifest_sha256=installation_manifest_sha256,
            environ=environ,
        )
        pointer = _pointer_payload(
            bundle_id=bundle_id,
            snapshot=snapshot,
            attestation_path=attestation_path,
            attestation_sha256=attestation_sha256,
            installation_manifest_sha256=installation_manifest_sha256,
            profiles=profiles,
        )
        _assert_runtime_inputs_unchanged(snapshot)
        _atomic_json(current_pointer_path(environ), pointer)
        return pointer
    except BaseException:
        if staging.exists():
            _restore_writable(staging)
            shutil.rmtree(staging)
        raise
    finally:
        if project_root.exists():
            shutil.rmtree(project_root)


def provision_runtime(
    *,
    uv_executable: Path,
    controller_helper: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    lock_root = runtime_cache_root(environ)
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (
        lock_root.is_symlink()
        or not lock_root.is_dir()
        or lock_root.stat().st_uid != os.geteuid()
        or stat.S_IMODE(lock_root.stat().st_mode) != 0o700
    ):
        raise RuntimeInstallationError(
            "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
            f"runtime lock root is unsafe: {lock_root}",
        )
    lock_path = lock_root / "provision.lock"
    try:
        descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except OSError as exc:
        raise RuntimeInstallationError(
            "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
            f"runtime provision lock is unsafe: {exc}",
        ) from exc
    try:
        lock_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != os.geteuid()
            or stat.S_IMODE(lock_stat.st_mode) != 0o600
            or lock_stat.st_nlink != 1
        ):
            raise RuntimeInstallationError(
                "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
                f"runtime provision lock identity differs: {lock_path}",
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            return _provision_runtime_locked(
                uv_executable=uv_executable,
                controller_helper=controller_helper,
                environ=environ,
            )
        except RuntimeInstallationError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            raise RuntimeInstallationError(
                "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
                f"runtime provisioning failed closed: {exc}",
            ) from exc
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def load_runtime_pointer(
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    payload = load_runtime_pointer_identity(environ=environ)
    snapshot = _capture_runtime_inputs()
    attestation_path = Path(payload["attestation_path"])
    try:
        observed_pointer = _verify_runtime_attestation(
            attestation_path,
            snapshot=snapshot,
            environ=environ,
        )
    except RuntimeInstallationError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeInstallationError(
            "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
            f"runtime integrity verification failed closed: {exc}",
        ) from exc
    if observed_pointer != payload:
        raise RuntimeInstallationError(
            "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
            "runtime pointer differs from installed runtime",
        )
    return payload


def load_runtime_pointer_identity(
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate immutable runtime identity without rehashing package trees.

    This is only for children of a process that already passed
    ``load_runtime_pointer``.  It deliberately retains the contract, lock,
    pointer and attestation byte bindings, but it does not issue production
    authority on its own.
    """

    path = current_pointer_path(environ)
    if not path.exists():
        raise RuntimeInstallationError(
            "RUNTIME_NOT_PROVISIONED", f"runtime pointer is missing: {path}"
        )
    payload = _load_pointer_document(path, environ=environ)
    snapshot = _capture_runtime_inputs()
    if payload["contract_sha256"] != snapshot.contract_sha256:
        raise RuntimeInstallationError(
            "RUNTIME_LOCK_MISMATCH", "runtime contract digest differs"
        )
    if payload["lock_sha256"] != snapshot.lock_sha256:
        raise RuntimeInstallationError("RUNTIME_LOCK_MISMATCH", "uv lock digest differs")
    if payload["bundle_id"] != _snapshot_bundle_id(snapshot):
        raise RuntimeInstallationError(
            "RUNTIME_LOCK_MISMATCH", "runtime bundle identity differs"
        )
    attestation_path = Path(payload["attestation_path"])
    expected_attestation_root = (
        runtime_cache_root(environ) / payload["bundle_id"] / "attestations"
    )
    if (
        not attestation_path.is_absolute()
        or attestation_path.is_symlink()
        or not attestation_path.is_file()
        or attestation_path.parent != expected_attestation_root
        or attestation_path.stat().st_uid != os.geteuid()
        or attestation_path.stat().st_mode
        & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        or _sha256_file(attestation_path) != payload["attestation_sha256"]
    ):
        raise RuntimeInstallationError(
            "RUNTIME_PACKAGE_INTEGRITY_MISMATCH", "runtime attestation differs"
        )
    attestation = _load_json(
        attestation_path,
        role="runtime attestation",
        reason_code="RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
    )
    attestation_fields = {
        "schema",
        "bundle_id",
        "contract_sha256",
        "lock_sha256",
        "installation_manifest_sha256",
        "observed_at_unix_ns",
        "host",
        "profiles",
    }
    if (
        not isinstance(attestation, dict)
        or set(attestation) != attestation_fields
        or attestation.get("schema") != ATTESTATION_SCHEMA
        or attestation.get("bundle_id") != payload["bundle_id"]
        or attestation.get("contract_sha256") != payload["contract_sha256"]
        or attestation.get("lock_sha256") != payload["lock_sha256"]
        or attestation.get("installation_manifest_sha256")
        != payload["installation_manifest_sha256"]
    ):
        raise RuntimeInstallationError(
            "RUNTIME_PACKAGE_INTEGRITY_MISMATCH", "runtime attestation binding differs"
        )
    return payload


def load_runtime_pointer_integrity(
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Rehash installed packages and host inputs without importing profiles.

    Production startup and qualification still use ``load_runtime_pointer``.
    This narrower full-byte gate is used between trials, where importing both
    CuPy and Torch runtimes again would consume the TP watchdog budget.  It
    verifies the same lock, manifest, RECORD/profile trees, duplicate metadata,
    interpreter topology, ROS/GPU/driver/calibration and owner dependency
    observations.
    """

    payload = load_runtime_pointer_identity(environ=environ)
    snapshot = _capture_runtime_inputs()
    attestation_path = Path(payload["attestation_path"])
    attestation = _load_json(
        attestation_path,
        role="runtime attestation",
        reason_code="RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
    )
    final = runtime_store(environ) / payload["bundle_id"]
    manifest = _verified_installation_manifest(
        final,
        snapshot=snapshot,
        installation_manifest_sha256=payload["installation_manifest_sha256"],
    )
    for profile in PROFILES:
        expected = manifest["profiles"][profile]
        _static_profile_integrity(
            final / profile,
            profile,
            snapshot=snapshot,
            expected=expected,
        )
        pointer_row = payload["profiles"][profile]
        if (
            pointer_row["environment_id"] != expected["environment_id"]
            or pointer_row["record_tree_sha256"]
            != expected["record_tree_sha256"]
            or pointer_row["profile_tree_sha256"]
            != expected["profile_tree_sha256"]
        ):
            raise RuntimeInstallationError(
                "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
                f"{profile} runtime pointer differs from installation manifest",
            )
    try:
        host = attestation["host"]
        uv_path = Path(host["uv"]["executable"])
        helper_path = Path(
            host["owner_dependencies"]["controller_helper"]["path"]
        )
    except (KeyError, TypeError) as exc:
        raise RuntimeInstallationError(
            "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
            "runtime attestation host binding differs",
        ) from exc
    current_host = observe_host(
        snapshot.contract,
        uv_executable=uv_path,
        controller_helper=helper_path,
    )
    if _host_identity(current_host) != _host_identity(host):
        raise RuntimeInstallationError(
            "HOST_CONTRACT_MISMATCH", "runtime host observation differs"
        )
    return payload


def owner_dependency(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    contract = load_runtime_contract()
    declared = contract["owner_dependencies"]
    if name not in declared:
        raise RuntimeInstallationError(
            "OWNER_DEPENDENCY_MISMATCH",
            f"owner dependency is not declared: {name}",
        )
    pointer = load_runtime_pointer(environ=environ)
    attestation = _load_json(
        Path(pointer["attestation_path"]),
        role="runtime attestation",
        reason_code="RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
    )
    try:
        binding = attestation["host"]["owner_dependencies"][name]
    except (KeyError, TypeError) as exc:
        raise RuntimeInstallationError(
            "OWNER_DEPENDENCY_MISMATCH",
            f"owner dependency attestation is missing: {name}",
        ) from exc
    expected = declared[name]
    if (
        not isinstance(binding, dict)
        or set(binding) != {"owner_id", "path", "sha256"}
        or binding.get("owner_id") != expected["owner_id"]
        or binding.get("sha256") != expected["sha256"]
        or not isinstance(binding.get("path"), str)
        or not Path(binding["path"]).is_absolute()
    ):
        raise RuntimeInstallationError(
            "OWNER_DEPENDENCY_MISMATCH",
            f"owner dependency binding differs: {name}",
        )
    path = Path(binding["path"])
    if path.is_symlink() or not path.is_file() or _sha256_file(path) != binding["sha256"]:
        raise RuntimeInstallationError(
            "OWNER_DEPENDENCY_MISMATCH",
            f"owner dependency file differs: {name}",
        )
    return {
        "owner_id": binding["owner_id"],
        "path": binding["path"],
        "sha256": binding["sha256"],
    }


def profile_python(
    profile: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    if profile not in PROFILES:
        raise RuntimeInstallationError(
            "RUNTIME_NOT_PROVISIONED", f"unknown runtime profile {profile!r}"
        )
    pointer = load_runtime_pointer(environ=environ)
    python = Path(pointer["profiles"][profile]["python_executable"])
    if not python.is_file() or not os.access(python, os.X_OK):
        reason = "CONTROL_RUNTIME_INVALID" if profile == "control" else "OPTIMIZER_RUNTIME_INVALID"
        raise RuntimeInstallationError(reason, f"{profile} interpreter is unavailable")
    return python


def require_runtime_profile(
    profile: str,
    *,
    environ: Mapping[str, str] | None = None,
    full_integrity: bool = True,
) -> dict[str, Any]:
    """Require this process to be running under the promoted exact interpreter."""

    if profile not in PROFILES:
        raise RuntimeInstallationError(
            "RUNTIME_NOT_PROVISIONED", f"unknown runtime profile {profile!r}"
        )
    pointer = (
        load_runtime_pointer(environ=environ)
        if full_integrity
        else load_runtime_pointer_identity(environ=environ)
    )
    expected = pointer["profiles"][profile]["python_executable"]
    observed = os.path.abspath(sys.executable)
    expected_prefix = pointer["profiles"][profile]["root"]
    observed_prefix = os.path.abspath(sys.prefix)
    if observed != expected or observed_prefix != expected_prefix:
        reason = (
            "CONTROL_RUNTIME_INVALID"
            if profile == "control"
            else "OPTIMIZER_RUNTIME_INVALID"
        )
        raise RuntimeInstallationError(
            reason,
            f"{profile} process binding differs: expected interpreter {expected} "
            f"and prefix {expected_prefix}, observed {observed} and {observed_prefix}",
        )
    return pointer


def runtime_binding(
    *,
    environ: Mapping[str, str] | None = None,
    runtime_pointer: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the immutable host-local binding used to launch production workers."""

    pointer = (
        load_runtime_pointer(environ=environ)
        if runtime_pointer is None
        else dict(runtime_pointer)
    )
    contract = load_runtime_contract()
    return {
        "schema": "step5d.autotune-v3/runtime-process-binding-v1",
        "bundle_id": pointer["bundle_id"],
        "contract_sha256": pointer["contract_sha256"],
        "lock_sha256": pointer["lock_sha256"],
        "runtime_attestation_sha256": pointer["attestation_sha256"],
        "gpu_uuid": contract["gpu"]["uuid"],
        "profiles": {
            profile: {
                "environment_id": pointer["profiles"][profile]["environment_id"],
                "python_executable": pointer["profiles"][profile]["python_executable"],
                "record_tree_sha256": pointer["profiles"][profile][
                    "record_tree_sha256"
                ],
                "profile_tree_sha256": pointer["profiles"][profile][
                    "profile_tree_sha256"
                ],
            }
            for profile in PROFILES
        },
    }


def runtime_status(*, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    def blocked(
        reason_code: str,
        detail: str,
        *,
        required_environment_id: str | None,
    ) -> dict[str, Any]:
        return {
            "required_environment_id": required_environment_id,
            "observed_environment_id": None,
            "control_ready": False,
            "optimizer_ready": False,
            "host_contract_ready": False,
            "gpu_identity_ready": False,
            "runtime_attestation_sha256": None,
            "attestation_sha256": None,
            "profiles": {
                profile: {
                    "required_environment_id": None,
                    "observed_environment_id": None,
                    "ready": False,
                    "python_executable": None,
                    "record_tree_sha256": None,
                    "profile_tree_sha256": None,
                }
                for profile in PROFILES
            },
            "reason_code": reason_code,
            "detail": detail,
        }

    try:
        required = runtime_bundle_id()
        pointer = load_runtime_pointer_integrity(environ=environ)
    except RuntimeInstallationError as exc:
        return blocked(
            exc.reason_code,
            exc.detail,
            required_environment_id=locals().get("required"),
        )
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        return blocked(
            "RUNTIME_PACKAGE_INTEGRITY_MISMATCH",
            f"runtime status failed closed: {exc}",
            required_environment_id=locals().get("required"),
        )
    return {
        "required_environment_id": required,
        "observed_environment_id": pointer["bundle_id"],
        "control_ready": True,
        "optimizer_ready": True,
        "host_contract_ready": True,
        "gpu_identity_ready": True,
        "runtime_attestation_sha256": pointer["attestation_sha256"],
        "attestation_sha256": pointer["attestation_sha256"],
        "profiles": {
            profile: {
                "required_environment_id": profile_environment_id(profile),
                "observed_environment_id": pointer["profiles"][profile][
                    "environment_id"
                ],
                "ready": True,
                "python_executable": pointer["profiles"][profile][
                    "python_executable"
                ],
                "record_tree_sha256": pointer["profiles"][profile][
                    "record_tree_sha256"
                ],
                "profile_tree_sha256": pointer["profiles"][profile][
                    "profile_tree_sha256"
                ],
            }
            for profile in PROFILES
        },
        "reason_code": None,
        "detail": None,
    }


__all__ = [
    "ATTESTATION_SCHEMA",
    "CONTRACT_PATH",
    "INSTALLATION_SCHEMA",
    "LOCK_PATH",
    "POINTER_SCHEMA",
    "PROFILES",
    "RuntimeInstallationError",
    "current_pointer_path",
    "load_runtime_contract",
    "load_runtime_pointer",
    "load_runtime_pointer_identity",
    "load_runtime_pointer_integrity",
    "lock_sha256",
    "observe_host",
    "observe_profile",
    "owner_dependency",
    "profile_environment_id",
    "profile_python",
    "provision_runtime",
    "require_runtime_profile",
    "runtime_binding",
    "runtime_bundle_id",
    "runtime_contract_sha256",
    "runtime_status",
]
