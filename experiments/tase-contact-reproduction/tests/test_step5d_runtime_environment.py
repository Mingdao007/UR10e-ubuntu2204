from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v3.runtime_calibration import CUDA_BOOTSTRAP_MARKER  # noqa: E402
from step5d_autotune_v3.runtime_environment import (  # noqa: E402
    DETERMINISTIC_VALUES,
    production_runtime_environment,
)


def test_runtime_environment_is_sanitized_deterministic_and_cuda_bound() -> None:
    source = {
        "PATH": "/usr/bin",
        "PYTHONPATH": "/repo/tools",
        "AMENT_PREFIX_PATH": "/opt/ros/humble",
        "STEP5D_V3_CANONICAL_LAUNCHER": "/repo/scripts/step5d-autotune-v3.sh",
        "UNRELATED_SECRET": "must-not-propagate",
        "OMP_NUM_THREADS": "99",
    }
    environment = production_runtime_environment(
        source,
        additions={"STEP5D_V3_RUNTIME_TICKET": "/run/ticket.json"},
    )

    assert "UNRELATED_SECRET" not in environment
    assert environment["STEP5D_V3_RUNTIME_TICKET"] == "/run/ticket.json"
    assert environment[CUDA_BOOTSTRAP_MARKER] == "1"
    assert environment["PYTHONPATH"].endswith(":" + source["PYTHONPATH"])
    for name, value in DETERMINISTIC_VALUES.items():
        assert environment[name] == value


def test_runtime_environment_rejects_empty_internal_binding() -> None:
    try:
        production_runtime_environment({}, additions={"TICKET": ""})
    except ValueError as exc:
        assert "non-empty" in str(exc)
    else:
        raise AssertionError("empty internal environment binding was accepted")
