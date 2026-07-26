"""Profile-selective, pointer-bound environments for production children."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from .runtime_installation import (
    PROFILES,
    load_runtime_contract,
    load_runtime_pointer_identity,
    runtime_profile_cache_root,
)


EXPERIMENT_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = EXPERIMENT_ROOT.parents[1]
RUNTIME_SOURCE = REPOSITORY_ROOT / "src/ur10e_experiment_runtime"

PASSTHROUGH_KEYS = (
    "HOME",
    "LANG",
    "LC_ALL",
    "ROS_DOMAIN_ID",
    "STEP5D_V3_CANONICAL_LAUNCHER",
    "STEP5D_V3_LAUNCH_ATTEMPT_ID",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)
DETERMINISTIC_VALUES = {
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONNOUSERSITE": "1",
}
RUNTIME_BINDING_KEYS = frozenset(
    {
        "AMENT_PREFIX_PATH",
        "CUBLAS_WORKSPACE_CONFIG",
        "CUDA_VISIBLE_DEVICES",
        "CUPY_CACHE_DIR",
        "LD_LIBRARY_PATH",
        "PATH",
        "PYTHONPATH",
        "STEP5D_V3_CONTROL_ENVIRONMENT_ID",
        "STEP5D_V3_CONTROL_PYTHON",
        "STEP5D_V3_GPU_UUID",
        "STEP5D_V3_OPTIMIZER_ENVIRONMENT_ID",
        "STEP5D_V3_OPTIMIZER_PYTHON",
        "STEP5D_V3_RUNTIME_ATTESTATION_SHA256",
        "STEP5D_V3_RUNTIME_BUNDLE_ID",
        "STEP5D_V3_RUNTIME_PROFILE",
        "UR10E_RNN_GPU_DEVICE",
        *DETERMINISTIC_VALUES,
    }
)


def _absolute_environment_path(values: Mapping[str, str], name: str) -> None:
    value = values.get(name)
    if value and not Path(value).is_absolute():
        raise ValueError(f"{name} must be absolute")


def _python_paths(profile: str, contract: Mapping[str, Any]) -> list[Path]:
    paths = [EXPERIMENT_ROOT / "tools", RUNTIME_SOURCE]
    if profile == "control":
        abi = ".".join(str(contract["python"]["version"]).split(".")[:2])
        ros_prefix = Path(contract["ros"]["prefix"])
        paths.extend(
            (
                ros_prefix / f"lib/python{abi}/site-packages",
                ros_prefix / f"local/lib/python{abi}/dist-packages",
            )
        )
    missing = [path for path in paths if not path.is_dir()]
    if missing:
        raise ValueError(f"runtime Python source path is missing: {missing[0]}")
    return paths


def _cuda_library_paths(profile_root: Path, contract: Mapping[str, Any]) -> list[Path]:
    if profile_root.is_symlink() or not profile_root.is_dir():
        raise ValueError("control runtime root is unsafe")
    abi = ".".join(str(contract["python"]["version"]).split(".")[:2])
    nvidia = profile_root / f"lib/python{abi}/site-packages/nvidia"
    paths = [
        nvidia / package / "lib"
        for package in ("cuda_nvrtc", "nvjitlink", "cuda_runtime")
        if (nvidia / package / "lib").is_dir()
    ]
    required = {
        nvidia / "cuda_nvrtc/lib",
        nvidia / "cuda_runtime/lib",
    }
    if not required.issubset(set(paths)):
        raise ValueError("control runtime CUDA libraries are incomplete")
    return paths


def production_runtime_environment(
    source: Mapping[str, str],
    *,
    profile: str = "control",
    additions: Mapping[str, str] | None = None,
    runtime_pointer: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Build one sanitized environment bound to an exact promoted profile."""

    if profile not in PROFILES:
        raise ValueError(f"unknown runtime profile: {profile}")
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in source.items()
    ):
        raise ValueError("runtime environment must map strings to strings")
    for name in ("XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        _absolute_environment_path(source, name)
    pointer = (
        load_runtime_pointer_identity(environ=source)
        if runtime_pointer is None
        else dict(runtime_pointer)
    )
    contract = load_runtime_contract()
    profile_row = pointer["profiles"][profile]
    profile_root = Path(profile_row["root"])
    environment = {
        name: source[name]
        for name in PASSTHROUGH_KEYS
        if source.get(name, "")
    }
    environment.setdefault("HOME", "/nonexistent")
    environment.setdefault("LANG", "C.UTF-8")
    environment.setdefault("LC_ALL", "C.UTF-8")
    environment.update(DETERMINISTIC_VALUES)
    environment.update(
        {
            "CUDA_VISIBLE_DEVICES": contract["gpu"]["uuid"],
            "PATH": f"{profile_root / 'bin'}:/usr/bin:/bin",
            "PYTHONPATH": os.pathsep.join(
                str(path) for path in _python_paths(profile, contract)
            ),
            "STEP5D_V3_CONTROL_ENVIRONMENT_ID": pointer["profiles"]["control"][
                "environment_id"
            ],
            "STEP5D_V3_CONTROL_PYTHON": pointer["profiles"]["control"][
                "python_executable"
            ],
            "STEP5D_V3_GPU_UUID": contract["gpu"]["uuid"],
            "STEP5D_V3_RUNTIME_ATTESTATION_SHA256": pointer[
                "attestation_sha256"
            ],
            "STEP5D_V3_RUNTIME_BUNDLE_ID": pointer["bundle_id"],
            "STEP5D_V3_RUNTIME_PROFILE": profile,
            "UR10E_RNN_GPU_DEVICE": "cuda:0",
        }
    )
    cache_root = runtime_profile_cache_root(source) / pointer["bundle_id"] / profile
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if profile == "control":
        cupy_cache = cache_root / "cupy"
        cupy_cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment.update(
            {
                "AMENT_PREFIX_PATH": contract["ros"]["prefix"],
                "CUPY_CACHE_DIR": str(cupy_cache),
                "LD_LIBRARY_PATH": os.pathsep.join(
                    str(path)
                    for path in _cuda_library_paths(profile_root, contract)
                ),
            }
        )
    else:
        environment.update(
            {
                "STEP5D_V3_OPTIMIZER_ENVIRONMENT_ID": pointer["profiles"][
                    "optimizer"
                ]["environment_id"],
                "STEP5D_V3_OPTIMIZER_PYTHON": pointer["profiles"]["optimizer"][
                    "python_executable"
                ],
            }
        )
        environment["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
    if additions is not None:
        if any(
            not isinstance(key, str) or not isinstance(value, str) or not value
            for key, value in additions.items()
        ):
            raise ValueError("runtime environment additions must be non-empty strings")
        protected = RUNTIME_BINDING_KEYS.intersection(additions)
        if protected:
            raise ValueError(
                "runtime environment additions override protected keys: "
                + ",".join(sorted(protected))
            )
        environment.update(additions)
    return environment


__all__ = [
    "DETERMINISTIC_VALUES",
    "PASSTHROUGH_KEYS",
    "RUNTIME_BINDING_KEYS",
    "production_runtime_environment",
]
