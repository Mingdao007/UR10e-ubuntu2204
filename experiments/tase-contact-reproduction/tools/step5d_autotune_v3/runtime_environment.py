"""One deterministic child environment for qualification and live runtime."""

from __future__ import annotations

from typing import Mapping

from .runtime_calibration import stable_cuda_environment


PASSTHROUGH_KEYS = (
    "AMENT_PREFIX_PATH",
    "CUDA_VISIBLE_DEVICES",
    "HOME",
    "LANG",
    "LC_ALL",
    "LD_LIBRARY_PATH",
    "NVIDIA_VISIBLE_DEVICES",
    "PATH",
    "PYTHONPATH",
    "ROS_DISTRO",
    "ROS_DOMAIN_ID",
    "STEP5D_PYTHON_RUNTIME_ROOT",
    "STEP5D_V3_CANONICAL_LAUNCHER",
    "UR10E_RNN_GPU_DEVICE",
)
DETERMINISTIC_VALUES = {
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "PYTHONHASHSEED": "0",
}


def production_runtime_environment(
    source: Mapping[str, str],
    *,
    additions: Mapping[str, str] | None = None,
) -> dict[str, str]:
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in source.items()
    ):
        raise ValueError("runtime environment must map strings to strings")
    environment = {
        name: source[name]
        for name in PASSTHROUGH_KEYS
        if source.get(name, "")
    }
    environment.update(DETERMINISTIC_VALUES)
    environment = stable_cuda_environment(environment)
    if additions is not None:
        if any(
            not isinstance(key, str) or not isinstance(value, str) or not value
            for key, value in additions.items()
        ):
            raise ValueError("runtime environment additions must be non-empty strings")
        environment.update(additions)
    return environment


__all__ = [
    "DETERMINISTIC_VALUES",
    "PASSTHROUGH_KEYS",
    "production_runtime_environment",
]
