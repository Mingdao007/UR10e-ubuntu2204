#!/usr/bin/env python3
"""Managed, offline preflight for the contact-six CPU package.

This entrypoint has a deliberately small boundary.  It checks the interpreter
created by ``scripts/contact-six.sh``, the exact locked dependency group, the
native six-law facade, the native equality QP, and the calibrated Pinocchio
model.  It never opens a robot, sensor, controller, socket, or ROS node.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = EXPERIMENT_ROOT.parents[1]
TOOLS_ROOT = Path(__file__).resolve().parent
# ``python -I /absolute/path/to/script.py`` intentionally omits the script
# directory from sys.path. Add only this checked-out tools directory so the
# native facade and calibrated model loader are the package under test.
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))
PYPROJECT_PATH = EXPERIMENT_ROOT / "pyproject.toml"
LOCK_PATH = EXPERIMENT_ROOT / "uv.lock"
VENV_NAME = ".venv-contact-six"
DEFAULT_VENV = EXPERIMENT_ROOT / VENV_NAME
DEFAULT_RECEIPT = EXPERIMENT_ROOT / "runs" / "contact-six" / "provision.json"
LAW_CONFIG = EXPERIMENT_ROOT / "config" / "contact_benchmark_laws.json"
QP_LIBRARY = EXPERIMENT_ROOT / "build" / "contact-qp" / "libcontact_qp.so"
LAW_BUILD_ROOT = EXPERIMENT_ROOT / "build" / "contact-six-laws"
PROFILE_ID = "contact-six-cpu-v1"
DEPENDENCY_GROUP = "contact-control"
ROS_PREFIX = Path("/opt/ros/humble")
ROS_PYTHON_PATHS = (
    ROS_PREFIX / "lib/python3.10/site-packages",
    ROS_PREFIX / "local/lib/python3.10/dist-packages",
)
EXPECTED_PACKAGES = {
    "jsonschema": "4.26.0",
    "numpy": "1.24.4",
    "scipy": "1.15.3",
    "osqp": "1.1.1",
    "pytest": "8.4.1",
    "PyYAML": "6.0.3",
    "setuptools": "81.0.0",
    "ur10e-experiment-runtime": "0.1.0",
}
COMPOSITION_MODULES = (
    "ur10e_experiment_runtime",
    "contact_benchmark_runtime",
    "contact_benchmark_provider",
    "contact_benchmark_timing",
    "contact_benchmark_campaign",
    "step5d_autotune_v4_r006.live_adapter",
    "step5d_autotune_v4_r013.live_owner",
)


class PreflightError(RuntimeError):
    """A failed local preflight check."""


@dataclass(frozen=True)
class Paths:
    experiment_root: Path
    venv: Path
    receipt: Path

    @property
    def python(self) -> Path:
        return self.venv / "bin" / "python"

    @property
    def pyproject(self) -> Path:
        return self.experiment_root / "pyproject.toml"

    @property
    def lock(self) -> Path:
        return self.experiment_root / "uv.lock"

    @property
    def law_config(self) -> Path:
        return self.experiment_root / "config" / "contact_benchmark_laws.json"

    @property
    def qp_library(self) -> Path:
        return self.experiment_root / "build" / "contact-qp" / "libcontact_qp.so"

    @property
    def law_build_root(self) -> Path:
        return self.experiment_root / "build" / "contact-six-laws"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_digest(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        return {"path": str(path), "exists": False}
    return {"path": str(path), "exists": True, "sha256": _sha256(path)}


def _json_print(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, indent=2, allow_nan=False))


def _paths(
    experiment_root: Path | str = EXPERIMENT_ROOT,
    venv: Path | str | None = None,
    receipt: Path | str | None = None,
) -> Paths:
    root = Path(experiment_root).expanduser().resolve()
    selected_venv = Path(venv).expanduser() if venv is not None else root / VENV_NAME
    selected_receipt = (
        Path(receipt).expanduser()
        if receipt is not None
        else root / "runs" / "contact-six" / "provision.json"
    )
    return Paths(root, selected_venv.resolve(), selected_receipt.resolve())


def _interpreter_observation(paths: Paths) -> dict[str, Any]:
    actual = Path(sys.executable).absolute()
    expected = paths.python.absolute()
    prefix = Path(sys.prefix).absolute()
    base_prefix = Path(sys.base_prefix).absolute()
    # A venv's python is commonly a symlink to the system executable.  The
    # prefix check therefore matters more than ``Path.resolve()`` equality.
    in_expected_venv = prefix == paths.venv.absolute() and base_prefix != prefix
    executable_in_venv = actual == expected or actual.parent == expected.parent
    return {
        "ok": in_expected_venv and executable_in_venv,
        "actual": str(actual),
        "resolved": str(actual.resolve()),
        "expected": str(expected),
        "sys_prefix": str(prefix),
        "sys_base_prefix": str(base_prefix),
        "venv": str(paths.venv),
    }


def _receipt_observation(paths: Paths) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(paths.receipt),
        "exists": paths.receipt.is_file() and not paths.receipt.is_symlink(),
        "ok": False,
    }
    if not result["exists"]:
        result["action"] = "provision"
        result["error"] = "managed environment has no provision receipt"
        return result
    try:
        payload = json.loads(paths.receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result["action"] = "provision"
        result["error"] = f"cannot read provision receipt: {exc}"
        return result
    if not isinstance(payload, dict):
        result["action"] = "provision"
        result["error"] = "provision receipt is not an object"
        return result
    current_pyproject = _file_digest(paths.pyproject)
    current_lock = _file_digest(paths.lock)
    result["recorded"] = payload
    result["current"] = {"pyproject": current_pyproject, "uv_lock": current_lock}
    expected = {
        "schema": "ur10e.contact-six-provision-v1",
        "profile_id": PROFILE_ID,
        "dependency_group": DEPENDENCY_GROUP,
        "pyproject_sha256": current_pyproject.get("sha256"),
        "uv_lock_sha256": current_lock.get("sha256"),
        "venv": str(paths.venv),
    }
    mismatches = {
        key: {"recorded": payload.get(key), "current": value}
        for key, value in expected.items()
        if payload.get(key) != value
    }
    result["mismatches"] = mismatches
    result["ok"] = not mismatches
    if mismatches:
        result["action"] = "provision"
        result["error"] = "pyproject, lock, profile, or environment differs from provision receipt"
    return result


def write_provision_receipt(
    *,
    experiment_root: Path | str = EXPERIMENT_ROOT,
    venv: Path | str | None = None,
    receipt: Path | str | None = None,
) -> dict[str, Any]:
    """Record the exact local inputs after a successful ``uv sync``."""

    paths = _paths(experiment_root, venv, receipt)
    observation = _interpreter_observation(paths)
    if not observation["ok"]:
        raise PreflightError(
            "provision receipt must be written by the managed contact-six interpreter"
        )
    pyproject = _file_digest(paths.pyproject)
    lock = _file_digest(paths.lock)
    if not pyproject.get("exists") or not lock.get("exists"):
        raise PreflightError("cannot write receipt without pyproject.toml and uv.lock")
    payload = {
        "schema": "ur10e.contact-six-provision-v1",
        "profile_id": PROFILE_ID,
        "dependency_group": DEPENDENCY_GROUP,
        "venv": str(paths.venv),
        "python": str(Path(sys.executable).absolute()),
        "pyproject_sha256": pyproject["sha256"],
        "uv_lock_sha256": lock["sha256"],
        "declared_ros_prefix": str(ROS_PREFIX),
        # ``uv sync`` may consult its configured package index. The
        # no-network guarantee applies to ``status``/prewarm, not provisioning.
        "provision_network_policy": "uv_sync_may_access_configured_package_index",
        "prewarm_network_used": False,
        "device_io": False,
        "motion_authorized": False,
        "live_qualified": False,
    }
    paths.receipt.parent.mkdir(parents=True, exist_ok=True)
    paths.receipt.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return payload


def _prepare_ros_import_paths() -> dict[str, Any]:
    missing = [str(path) for path in ROS_PYTHON_PATHS if not path.is_dir()]
    if missing:
        raise PreflightError(f"ROS Humble Python paths missing: {missing}")
    ament_prefix_path = os.environ.get("AMENT_PREFIX_PATH", "")
    if str(ROS_PREFIX) not in ament_prefix_path.split(":"):
        raise PreflightError("AMENT_PREFIX_PATH must explicitly declare /opt/ros/humble")
    # These are the only external import paths admitted by this package.  The
    # project tools directory is already sys.path[0] when this file is run.
    for path in reversed(ROS_PYTHON_PATHS):
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)
    return {
        "prefix": str(ROS_PREFIX),
        "python_paths": [str(path) for path in ROS_PYTHON_PATHS],
        "ament_prefix_path": ament_prefix_path,
        "ament_declared": True,
    }


def _dependency_observation() -> dict[str, Any]:
    versions: dict[str, str] = {}
    errors: dict[str, str] = {}
    for distribution, expected in EXPECTED_PACKAGES.items():
        try:
            observed = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            errors[distribution] = "package is not installed in the managed environment"
            continue
        versions[distribution] = observed
        if observed != expected:
            errors[distribution] = f"expected {expected}, observed {observed}"
    return {"ok": not errors, "expected": EXPECTED_PACKAGES, "observed": versions, "errors": errors}


def _module_observation() -> dict[str, Any]:
    modules = ("numpy", "scipy", "osqp", "pytest", "yaml", "pinocchio", "xacro")
    loaded: dict[str, str] = {}
    errors: dict[str, str] = {}
    for name in modules:
        try:
            module = importlib.import_module(name)
            loaded[name] = str(Path(module.__file__).resolve())
        except Exception as exc:  # pragma: no cover - exact import error is host-specific
            errors[name] = f"{type(exc).__name__}: {exc}"
    expected_ros = [str(path.resolve()) for path in ROS_PYTHON_PATHS]
    for name in ("pinocchio", "xacro"):
        location = loaded.get(name, "")
        if location and not any(location.startswith(path + os.sep) for path in expected_ros):
            errors[name] = f"{name} did not load from ROS Humble: {location}"
    for forbidden in ("torch", "cupy"):
        if forbidden in sys.modules:
            errors[forbidden] = "forbidden accelerator module was imported"
    return {
        "ok": not errors,
        "loaded": loaded,
        "errors": errors,
        "forbidden_loaded": [name for name in ("torch", "cupy") if name in sys.modules],
    }


def _under(path: Path, root: Path) -> bool:
    """Return whether ``path`` is contained by ``root`` after resolution."""

    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _composition_import_observation(paths: Paths) -> dict[str, Any]:
    """Import the composition modules from the managed package boundary.

    This check deliberately does not add the repository ``src`` directory to
    ``sys.path``.  ``ur10e_experiment_runtime`` must therefore resolve from
    the installed local distribution in the contact-six venv.  The remaining
    modules are checked-out tools and are admitted through ``TOOLS_ROOT``.
    Importing in a fresh status process also makes the accelerator check
    meaningful: no torch/cupy import is tolerated as a transitive side effect.
    """

    before_forbidden = [name for name in ("torch", "cupy") if name in sys.modules]
    loaded: dict[str, str] = {}
    errors: dict[str, str] = {}
    for name in COMPOSITION_MODULES:
        try:
            module = importlib.import_module(name)
            module_file = getattr(module, "__file__", None)
            if not module_file:
                raise PreflightError(f"{name} has no importable __file__")
            location = Path(module_file).resolve()
            loaded[name] = str(location)
            if name == "ur10e_experiment_runtime":
                if not _under(location, paths.venv):
                    errors[name] = (
                        "local runtime resolved outside the managed venv; "
                        f"observed {location}"
                    )
            elif not _under(location, TOOLS_ROOT):
                errors[name] = (
                    "composition module resolved outside the checked-out tools "
                    f"directory; observed {location}"
                )
        except Exception as exc:  # pragma: no cover - host import graph varies
            errors[name] = f"{type(exc).__name__}: {exc}"
    after_forbidden = [name for name in ("torch", "cupy") if name in sys.modules]
    newly_forbidden = [name for name in after_forbidden if name not in before_forbidden]
    if before_forbidden:
        errors["forbidden_before"] = (
            "forbidden accelerator module was already loaded before composition: "
            + ", ".join(before_forbidden)
        )
    if newly_forbidden:
        errors["forbidden_after"] = (
            "composition imported forbidden accelerator module(s): "
            + ", ".join(newly_forbidden)
        )
    return {
        "ok": not errors,
        "modules": list(COMPOSITION_MODULES),
        "loaded": loaded,
        "errors": errors,
        "forbidden_before": before_forbidden,
        "forbidden_after": after_forbidden,
        "new_forbidden": newly_forbidden,
        "managed_runtime_distribution": "ur10e-experiment-runtime==0.1.0",
    }


def _law_prewarm(paths: Paths) -> dict[str, Any]:
    # Imports are intentionally local so a missing venv can produce a useful
    # provision action without accidentally running against global packages.
    from contact_laws import ContactLaw, PUBLIC_LAWS

    if tuple(PUBLIC_LAWS) != ("LAC", "NAC", "SFC", "DSFC", "ISFC", "MSFC"):
        raise PreflightError(f"public native law labels differ: {PUBLIC_LAWS}")
    observations: dict[str, Any] = {}
    for label in PUBLIC_LAWS:
        with ContactLaw.from_config(
            label,
            paths.law_config,
            dimension=3,
            dt_s=0.002,
            build_root=paths.law_build_root,
        ) as law:
            tick = law.step((0.0, 0.0, 0.0), dt_s=0.002)
            observations[label] = {
                "finite": all(
                    value == value and abs(value) != float("inf")
                    for value in (*tick.state, *tick.command, *tick.acceleration)
                ),
                "identity": law.identity,
                "state": list(tick.state),
                "command": list(tick.command),
            }
    return {
        "ok": all(item["finite"] for item in observations.values()),
        "labels": list(PUBLIC_LAWS),
        "observations": observations,
        "build_root": str(paths.law_build_root),
    }


def _qp_prewarm(paths: Paths) -> dict[str, Any]:
    import numpy as np

    from contact_qp import NativeContactQp

    if not paths.qp_library.is_file() or paths.qp_library.is_symlink():
        raise PreflightError(f"native QP library is missing: {paths.qp_library}")
    solver = NativeContactQp(paths.qp_library, deadline_s=None)
    result = solver.solve(
        np.eye(6, dtype=float),
        np.zeros(6, dtype=float),
        np.full(6, -0.01, dtype=float),
        np.full(6, 0.01, dtype=float),
    )
    return {
        "ok": all(np.isfinite(result.qdot)),
        "library": str(paths.qp_library),
        "qdot": list(result.qdot),
        "equality_residual": result.equality_residual,
        "bound_violation": result.bound_violation,
        "deadline_enforced": False,
    }


def _calibrated_model_prewarm() -> dict[str, Any]:
    from step5c_calibrated_kinematics_audit import build_calibrated_model

    bundle = build_calibrated_model()
    return {
        "ok": bundle.model.nq == 6 and bundle.model.nv == 6,
        "calibration_hash": bundle.calibration_hash,
        "nq": int(bundle.model.nq),
        "nv": int(bundle.model.nv),
        "frames": {
            "base": int(bundle.base_frame_id),
            "tool0": int(bundle.tool0_frame_id),
            "flange": int(bundle.flange_frame_id),
        },
        "xacro_source": "/opt/ros/humble/share/ur_description/urdf/ur.urdf.xacro",
    }


def run_status(
    *,
    experiment_root: Path | str = EXPERIMENT_ROOT,
    venv: Path | str | None = None,
    receipt: Path | str | None = None,
) -> tuple[dict[str, Any], int]:
    paths = _paths(experiment_root, venv, receipt)
    interpreter = _interpreter_observation(paths)
    receipt_observation = _receipt_observation(paths)
    payload: dict[str, Any] = {
        "schema": "ur10e.contact-six-cpu-preflight-v1",
        "command": "status",
        "profile_id": PROFILE_ID,
        "dependency_group": DEPENDENCY_GROUP,
        "ok": False,
        # Keep these top-level flags easy for a shell/CI consumer to inspect;
        # ``offline_boundary`` below carries the same contract in a grouped
        # form for evidence readers.
        "network_used": False,
        "device_io": False,
        "motion_authorized": False,
        "live_qualified": False,
        "hardware_qualified": False,
        "full_composition_imports_ready": False,
        "offline_boundary": {
            "network_used": False,
            "device_io": False,
            "motion_authorized": False,
            "live_qualified": False,
            "hardware_qualified": False,
        },
        "interpreter": interpreter,
        "provision": receipt_observation,
        "files": {
            "pyproject": _file_digest(paths.pyproject),
            "uv_lock": _file_digest(paths.lock),
            "law_config": _file_digest(paths.law_config),
            "qp_library": _file_digest(paths.qp_library),
        },
        "environment": {
            "ament_prefix_path": os.environ.get("AMENT_PREFIX_PATH", ""),
            "declared_ros_prefix": str(ROS_PREFIX),
        },
        "checks": {},
    }
    if not interpreter["ok"]:
        payload["action"] = "provision"
        payload["error"] = "status must run in the independent contact-six venv"
        return payload, 2
    if not receipt_observation["ok"]:
        payload["action"] = "provision"
        return payload, 2
    try:
        payload["environment"]["ros"] = _prepare_ros_import_paths()
        payload["checks"]["dependencies"] = _dependency_observation()
        payload["checks"]["imports"] = _module_observation()
        payload["checks"]["composition_imports"] = _composition_import_observation(paths)
        if not payload["checks"]["dependencies"]["ok"]:
            raise PreflightError("declared CPU dependency versions are not satisfied")
        if not payload["checks"]["imports"]["ok"]:
            raise PreflightError("one or more declared imports failed or escaped their source path")
        if not payload["checks"]["composition_imports"]["ok"]:
            raise PreflightError("full contact composition imports are not ready")
        payload["full_composition_imports_ready"] = True
        payload["checks"]["native_laws"] = _law_prewarm(paths)
        payload["checks"]["native_qp"] = _qp_prewarm(paths)
        payload["checks"]["calibrated_model"] = _calibrated_model_prewarm()
        payload["ok"] = all(
            bool(payload["checks"][name]["ok"])
            for name in (
                "dependencies",
                "imports",
                "composition_imports",
                "native_laws",
                "native_qp",
                "calibrated_model",
            )
        )
    except Exception as exc:  # structured failure is part of the offline contract
        payload["error"] = f"{type(exc).__name__}: {exc}"
        payload["action"] = "repair_or_provision"
    return payload, 0 if payload["ok"] else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "record-provision"))
    parser.add_argument("--experiment-root", type=Path, default=EXPERIMENT_ROOT)
    parser.add_argument("--venv", type=Path, default=None)
    parser.add_argument("--receipt", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "record-provision":
        try:
            _json_print(
                write_provision_receipt(
                    experiment_root=args.experiment_root,
                    venv=args.venv,
                    receipt=args.receipt,
                )
            )
        except Exception as exc:
            _json_print({"ok": False, "error": f"{type(exc).__name__}: {exc}", "action": "provision"})
            return 2
        return 0
    payload, returncode = run_status(
        experiment_root=args.experiment_root,
        venv=args.venv,
        receipt=args.receipt,
    )
    _json_print(payload)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
