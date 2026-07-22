from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v3.runtime_environment import (  # noqa: E402
    DETERMINISTIC_VALUES,
    production_runtime_environment,
)


def _runtime_pointer(tmp_path: Path) -> dict[str, object]:
    profiles: dict[str, dict[str, str]] = {}
    for index, profile in enumerate(("control", "optimizer"), start=1):
        root = tmp_path / "runtimes" / profile
        (root / "bin").mkdir(parents=True)
        profiles[profile] = {
            "root": str(root),
            "python_executable": str(root / "bin/python"),
            "environment_id": str(index) * 64,
        }
    control_site = (
        Path(profiles["control"]["root"])
        / "lib/python3.10/site-packages/nvidia"
    )
    for package in ("cuda_nvrtc", "nvjitlink", "cuda_runtime"):
        (control_site / package / "lib").mkdir(parents=True)
    return {
        "bundle_id": "a" * 64,
        "attestation_sha256": "b" * 64,
        "profiles": profiles,
    }


def _source(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/caller/conda/bin:/usr/bin",
        "PYTHONPATH": "/caller/private-sidecar",
        "LD_LIBRARY_PATH": "/caller/cuda",
        "AMENT_PREFIX_PATH": "/caller/ros",
        "CONDA_PREFIX": "/caller/conda",
        "VIRTUAL_ENV": "/caller/venv",
        "UNRELATED_SECRET": "must-not-propagate",
        "OMP_NUM_THREADS": "99",
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "STEP5D_V3_CANONICAL_LAUNCHER": "/repo/scripts/step5d-autotune-v3.sh",
    }


def test_control_runtime_environment_is_sanitized_and_pointer_bound(
    tmp_path: Path,
) -> None:
    pointer = _runtime_pointer(tmp_path)
    environment = production_runtime_environment(
        _source(tmp_path),
        profile="control",
        runtime_pointer=pointer,
        additions={"STEP5D_V3_RUNTIME_TICKET": "/run/ticket.json"},
    )

    assert "UNRELATED_SECRET" not in environment
    assert "CONDA_PREFIX" not in environment
    assert "VIRTUAL_ENV" not in environment
    assert "/caller/private-sidecar" not in environment["PYTHONPATH"]
    assert environment["PATH"] == (
        f"{pointer['profiles']['control']['root']}/bin:/usr/bin:/bin"
    )
    assert environment["STEP5D_V3_RUNTIME_PROFILE"] == "control"
    assert environment["STEP5D_V3_RUNTIME_BUNDLE_ID"] == pointer["bundle_id"]
    assert environment["STEP5D_V3_CONTROL_ENVIRONMENT_ID"] == "1" * 64
    assert environment["STEP5D_V3_OPTIMIZER_ENVIRONMENT_ID"] == "2" * 64
    assert environment["STEP5D_V3_CONTROL_PYTHON"].endswith("/control/bin/python")
    assert environment["STEP5D_V3_OPTIMIZER_PYTHON"].endswith(
        "/optimizer/bin/python"
    )
    assert environment["CUDA_VISIBLE_DEVICES"].startswith("GPU-")
    assert environment["UR10E_RNN_GPU_DEVICE"] == "cuda:0"
    assert environment["AMENT_PREFIX_PATH"] == "/opt/ros/humble"
    assert environment["CUPY_CACHE_DIR"].endswith(
        f"/{pointer['bundle_id']}/control/cupy"
    )
    assert "/caller/cuda" not in environment["LD_LIBRARY_PATH"]
    assert environment["STEP5D_V3_RUNTIME_TICKET"] == "/run/ticket.json"
    for name, value in DETERMINISTIC_VALUES.items():
        assert environment[name] == value


def test_optimizer_runtime_excludes_control_and_optional_bindings(
    tmp_path: Path,
) -> None:
    pointer = _runtime_pointer(tmp_path)
    environment = production_runtime_environment(
        _source(tmp_path),
        profile="optimizer",
        runtime_pointer=pointer,
    )

    assert environment["STEP5D_V3_RUNTIME_PROFILE"] == "optimizer"
    assert environment["PATH"] == (
        f"{pointer['profiles']['optimizer']['root']}/bin:/usr/bin:/bin"
    )
    assert environment["CUBLAS_WORKSPACE_CONFIG"] == ":4096:8"
    assert "AMENT_PREFIX_PATH" not in environment
    assert "CUPY_CACHE_DIR" not in environment
    assert "LD_LIBRARY_PATH" not in environment
    assert "/opt/ros" not in environment["PYTHONPATH"]
    assert "/caller/private-sidecar" not in environment["PYTHONPATH"]


@pytest.mark.parametrize(
    "additions",
    [
        {"TICKET": ""},
        {"PATH": "/tmp/override"},
        {"PYTHONNOUSERSITE": "0"},
        {"STEP5D_V3_CONTROL_PYTHON": "/tmp/python"},
    ],
)
def test_runtime_environment_rejects_invalid_or_protected_additions(
    tmp_path: Path,
    additions: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        production_runtime_environment(
            _source(tmp_path),
            runtime_pointer=_runtime_pointer(tmp_path),
            additions=additions,
        )


def test_runtime_environment_rejects_unknown_profile_and_relative_xdg(
    tmp_path: Path,
) -> None:
    pointer = _runtime_pointer(tmp_path)
    with pytest.raises(ValueError, match="unknown runtime profile"):
        production_runtime_environment(
            _source(tmp_path), profile="analysis", runtime_pointer=pointer
        )

    source = _source(tmp_path)
    source["XDG_CACHE_HOME"] = "relative/cache"
    with pytest.raises(ValueError, match="XDG_CACHE_HOME must be absolute"):
        production_runtime_environment(source, runtime_pointer=pointer)
