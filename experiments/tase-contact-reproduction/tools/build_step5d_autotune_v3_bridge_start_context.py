#!/usr/bin/env python3
"""Build one write-once Step5d V3 NO_ARM bridge-start context.

This tool is intentionally incapable of creating either certification-motion
or campaign authorization.  It performs no controller, robot, bridge, sensor,
or network write.  A context is emitted only after the repository readiness
owner proves that the fresh V3 controller read-back matches the local triplet.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
from typing import Any, Mapping

from step5d_autotune_v3.arming import BridgeStartContext
from step5d_autotune_v3.identity_layers import (
    release_basis_fingerprint,
    runtime_environment_fingerprint,
    runtime_environment_manifest,
)
from step5d_autotune_v3.readiness import (
    ReleaseReadinessError,
    resolve_release_readiness,
)
from step5d_autotune_v3.runtime_calibration import (
    DEFAULT_STABLE_PYTHON_RUNTIME,
    RuntimeCalibrationError,
    stable_cuda_environment,
)


ROOT = Path(__file__).resolve().parents[1]
_THREAD_ENVIRONMENT = (
    "CUDA_VISIBLE_DEVICES",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "UR10E_RNN_GPU_DEVICE",
)


class BridgeContextBuildError(RuntimeError):
    """The passive deployment identity is incomplete or unsafe."""


def _distribution_version(
    *names: str,
    search_path: Path | None = None,
) -> str | None:
    if search_path is not None:
        expected = {name.lower().replace("_", "-") for name in names}
        for distribution in importlib.metadata.distributions(path=[str(search_path)]):
            observed = str(distribution.metadata.get("Name") or "").lower().replace(
                "_", "-"
            )
            if observed in expected:
                return distribution.version
    for name in names:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return None


def _nvidia_driver_version() -> str | None:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=driver_version",
                "--format=csv,noheader",
                "--id=0",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    rows = [row.strip() for row in result.stdout.splitlines() if row.strip()]
    return rows[0] if len(rows) == 1 else None


def _debian_package_version(name: str) -> str | None:
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", name],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def _module_version(name: str) -> str | None:
    try:
        module = importlib.import_module(name)
    except (ImportError, SystemError):
        return None
    value = getattr(module, "__version__", None)
    return str(value) if isinstance(value, (str, int, float)) else None


def capture_passive_runtime_environment() -> dict[str, Any]:
    """Capture identity inputs without benchmarking or changing host state."""

    stable_environment = stable_cuda_environment(dict(os.environ))
    stable_runtime = Path(
        stable_environment.get(
            "STEP5D_PYTHON_RUNTIME_ROOT",
            str(DEFAULT_STABLE_PYTHON_RUNTIME),
        )
    )
    scheduler = os.sched_getscheduler(0)
    scheduler_names = {
        getattr(os, name): name
        for name in ("SCHED_OTHER", "SCHED_BATCH", "SCHED_IDLE", "SCHED_FIFO", "SCHED_RR")
        if hasattr(os, name)
    }
    return {
        "capture_mode": "passive_no_pressure_test_no_host_mutation",
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "kernel": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "scheduler": {
            "policy": scheduler,
            "policy_name": scheduler_names.get(scheduler, f"UNKNOWN_{scheduler}"),
            "priority": os.sched_getparam(0).sched_priority,
            "nice": os.getpriority(os.PRIO_PROCESS, 0),
            "cpu_affinity": sorted(os.sched_getaffinity(0)),
        },
        "thread_environment": {
            name: stable_environment.get(name) for name in _THREAD_ENVIRONMENT
        },
        "packages": {
            "cupy": _distribution_version(
                "cupy-cuda12x", "cupy", search_path=stable_runtime
            ),
            "numpy": _distribution_version("numpy", search_path=stable_runtime),
            "pinocchio": (
                _distribution_version("pin", "pinocchio")
                or _module_version("pinocchio")
                or _debian_package_version("ros-humble-pinocchio")
            ),
            "cuda_runtime": _distribution_version(
                "nvidia-cuda-runtime-cu12", search_path=stable_runtime
            ),
        },
        "gpu": {
            "selected_device": stable_environment.get("UR10E_RNN_GPU_DEVICE", "0"),
            "visible_devices": stable_environment.get("CUDA_VISIBLE_DEVICES"),
            "driver_version": _nvidia_driver_version(),
        },
    }


def build_context(
    root: Path,
    *,
    plant_epoch: int,
    runtime_environment: Mapping[str, Any] | None = None,
) -> BridgeStartContext:
    if isinstance(plant_epoch, bool) or not isinstance(plant_epoch, int) or plant_epoch < 1:
        raise BridgeContextBuildError("plant_epoch must be a positive integer")
    root = root.expanduser().resolve(strict=True)
    report = resolve_release_readiness(root)
    if report.get("deployment_ready") is not True:
        raise BridgeContextBuildError(
            "fresh V3 TP read-back does not match the local triplet"
        )
    identity = report.get("identity") or {}
    environment = dict(
        runtime_environment
        if runtime_environment is not None
        else capture_passive_runtime_environment()
    )
    environment_manifest = runtime_environment_manifest(environment)
    environment_fingerprint = runtime_environment_fingerprint(environment)
    basis_components = {
        "tick_semantics_fingerprint": identity["tick_semantics_fingerprint"],
        "timing_harness_fingerprint": identity["timing_harness_fingerprint"],
        "runtime_environment_fingerprint": environment_fingerprint,
        "deployment_fingerprint": identity["deployment_fingerprint"],
        "orchestration_fingerprint": identity["orchestration_fingerprint"],
    }
    return BridgeStartContext(
        **basis_components,
        release_basis_fingerprint=release_basis_fingerprint(
            **basis_components,
            plant_epoch=plant_epoch,
        ),
        local_triplet_sha256=identity["local_triplet_sha256"],
        plant_epoch=plant_epoch,
        deployment_readback_sha256=report["controller_readback_sha256"],
        runtime_environment_manifest=environment_manifest,
    )


def write_once(path: Path, payload: Mapping[str, Any]) -> None:
    destination = path.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise BridgeContextBuildError("bridge-start context output already exists")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (
        json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    handle, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary_path = Path(temporary)
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary_path, destination)
    except FileExistsError as exc:
        raise BridgeContextBuildError(
            "bridge-start context output already exists"
        ) from exc
    finally:
        temporary_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--plant-epoch", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        context = build_context(args.root, plant_epoch=args.plant_epoch)
        write_once(args.output, context.document())
    except (
        BridgeContextBuildError,
        ReleaseReadinessError,
        RuntimeCalibrationError,
        KeyError,
        ValueError,
    ) as exc:
        parser.error(str(exc))
    report = {
        "schema": "step5d.autotune-v3/bridge-start-context-build-result-v1",
        "output": str(args.output.expanduser().absolute()),
        "context_sha256": context.fingerprint,
        "release_basis_fingerprint": context.release_basis_fingerprint,
        "plant_epoch": context.plant_epoch,
        "bridge_start_ready": True,
        "motion_authorized": False,
        "campaign_authorized": False,
        "hardware_actions": False,
        "timing_pressure_test": False,
    }
    if args.json:
        print(json.dumps(report, allow_nan=False, sort_keys=True))
    else:
        print(f"bridge_start_context={report['output']}")
        print("bridge_start_ready=true")
        print("motion_authorized=false")
        print("campaign_authorized=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
