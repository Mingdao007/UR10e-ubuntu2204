#!/usr/bin/env python3
"""Read-only Ubuntu CuPy timing harness for v30, suitable for SSH stdin.

Example (the local source-bound bundle is read from stdin and writes only
stdout):

  python3 tools/build_step5d_v30_remote_timing_bundle.py | \
    ssh andy7 'cd ... && PYTHONDONTWRITEBYTECODE=1 python3 - \
      --experiment-root "$PWD" --replay-csv runs/.../bridge_rtde_500hz.csv'
"""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import importlib.metadata
import json
import math
import os
import resource
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


PROFILE = {
    "backend": "cupy",
    "inner_iterations": 512,
    "epsilon": 0.010,
    "sigr_exponent_r": 0.8,
    "qdot_cap_rad_s": 0.05,
    "control_hz": 500.0,
}
DIAGNOSTIC_INNER_ITERATION_CHOICES = (128, 256, 512)
DEADLINE_MS = 2.0
DEADLINE_EVENT_CAPACITY = 64
SOLVER_BATCH_SIZE = 100
SOLVER_BATCH_YIELD_S = 0.002
THREAD_ENV_NAMES = (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


@dataclass(frozen=True)
class PreparedReplayRow:
    """Typed observation buffers decoded before the 500 Hz measurement."""

    q: np.ndarray
    qd: np.ndarray
    pose: tuple[float, float, float, float, float, float]
    speed: tuple[float, float, float, float, float, float]
    force_tcp: tuple[float, float, float]
    reaction: np.ndarray
    reaction_tuple: tuple[float, float, float]
    desired_x_m: float
    desired_y_m: float
    desired_vx_m_s: float
    desired_vy_m_s: float


def profile_sha256(profile: Mapping[str, Any]) -> str:
    """Fingerprint one exact runtime profile without path or host metadata."""

    encoded = json.dumps(
        dict(profile),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_profile_selection(
    requested_inner_iterations: int | None,
    *,
    effective_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind canonical, requested, and solver-effective profiles distinctly.

    Passing ``--inner-iterations`` is always a diagnostic sweep request, even
    when the requested value is the canonical 512.  This prevents an explicit
    sweep run from being reused as formal readiness evidence by filename or by
    an otherwise identical effective profile.
    """

    if (
        requested_inner_iterations is not None
        and int(requested_inner_iterations)
        not in DIAGNOSTIC_INNER_ITERATION_CHOICES
    ):
        choices = ", ".join(str(value) for value in DIAGNOSTIC_INNER_ITERATION_CHOICES)
        raise ValueError(f"inner_iterations diagnostic override must be one of: {choices}")
    canonical_profile = dict(PROFILE)
    requested_profile = dict(canonical_profile)
    if requested_inner_iterations is not None:
        requested_profile["inner_iterations"] = int(requested_inner_iterations)
    effective = (
        dict(requested_profile)
        if effective_profile is None
        else dict(effective_profile)
    )
    if effective != requested_profile:
        raise ValueError(
            "solver-effective runtime profile does not match the requested profile"
        )
    diagnostic_override = requested_inner_iterations is not None
    selection: dict[str, Any] = {
        "schema_version": "step5d_v30_timing_profile_selection_v1",
        "mode": (
            "diagnostic_inner_iterations_sweep"
            if diagnostic_override
            else "canonical_formal_candidate"
        ),
        "diagnostic_override_requested": diagnostic_override,
        "acceptance_profile_eligible": not diagnostic_override,
        "canonical_profile": canonical_profile,
        "canonical_profile_sha256": profile_sha256(canonical_profile),
        "requested_profile": requested_profile,
        "requested_profile_sha256": profile_sha256(requested_profile),
        "effective_profile": effective,
        "effective_profile_sha256": profile_sha256(effective),
        "fixed_contract": {
            "backend": "cupy",
            "epsilon": 0.010,
            "sigr_exponent_r": 0.8,
            "qdot_cap_rad_s": 0.05,
            "control_hz": 500.0,
            "safety_gates_unchanged": True,
            "dls_runtime_fallback_allowed": False,
        },
    }
    selection["selection_sha256"] = profile_sha256(selection)
    return selection


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-root", type=Path, default=Path.cwd())
    parser.add_argument("--replay-csv", type=Path, required=True)
    parser.add_argument("--solver-samples", type=int, default=10_000)
    parser.add_argument("--tick-samples", type=int, default=30_000)
    parser.add_argument("--safe-hold-samples", type=int, default=30_000)
    parser.add_argument("--component-diagnostic-samples", type=int, default=0)
    parser.add_argument("--component-outlier-threshold-ms", type=float, default=2.0)
    parser.add_argument("--component-outlier-ring-size", type=int, default=32)
    parser.add_argument("--pace-500hz", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--include-raw-samples", action="store_true")
    parser.add_argument(
        "--inner-iterations",
        type=int,
        choices=DIAGNOSTIC_INNER_ITERATION_CHOICES,
        default=None,
        help=(
            "diagnostic-only strict-RNN override; any explicit value marks "
            "the artifact ineligible for formal readiness"
        ),
    )
    return parser


def finite(row: Mapping[str, str], key: str) -> float:
    value = float(row[key])
    if not math.isfinite(value):
        raise ValueError(f"nonfinite replay field: {key}")
    return value


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def vector(row: Mapping[str, str], prefix: str, length: int) -> np.ndarray:
    return np.asarray([finite(row, f"{prefix}{index}") for index in range(length)], dtype=float)


def _is_compatible_source_row(row: Mapping[str, str]) -> bool:
    """Accept historical CuPy observations without inheriting their iteration count."""

    try:
        return (
            row.get("_step5d_rnn_backend") == "cupy"
            and math.isclose(float(row.get("_step5d_rnn_epsilon") or 0.0), 0.010)
            and math.isclose(float(row.get("_step5d_rnn_sigr_exponent_r") or 0.0), 0.8)
        )
    except (TypeError, ValueError):
        return False


def load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    required = {
        *(f"ur_actual_q_{index}" for index in range(6)),
        *(f"ur_actual_qd_{index}" for index in range(6)),
        *(f"ur_actual_TCP_pose_{index}" for index in range(6)),
        *(f"ur_actual_TCP_speed_{index}" for index in range(6)),
        *(f"_step4e_force_t_{axis}" for axis in "xyz"),
        *(f"_step4e_control_normal_b_{axis}" for axis in "xyz"),
        "_step4e_desired_x_m",
        "_step4e_desired_y_m",
        "_step4e_desired_vx_m_s",
        "_step4e_desired_vy_m_s",
    }
    missing = sorted(required - set(rows[0] if rows else ()))
    if missing:
        raise RuntimeError(f"replay CSV missing required columns: {missing}")
    selected = [row for row in rows if _is_compatible_source_row(row)]
    if not selected:
        raise RuntimeError("replay CSV has no compatible CuPy/epsilon=0.010/r=0.8 observation rows")
    return selected


def _tuple3(values: np.ndarray) -> tuple[float, float, float]:
    return tuple(float(value) for value in values.reshape(3))  # type: ignore[return-value]


def _tuple6(values: np.ndarray) -> tuple[float, float, float, float, float, float]:
    return tuple(float(value) for value in values.reshape(6))  # type: ignore[return-value]


def prepare_rows(rows: Sequence[Mapping[str, str]]) -> list[PreparedReplayRow]:
    """Remove CSV parsing/allocation from the measured control tick."""

    prepared: list[PreparedReplayRow] = []
    for row in rows:
        q = vector(row, "ur_actual_q_", 6)
        qd = vector(row, "ur_actual_qd_", 6)
        pose = vector(row, "ur_actual_TCP_pose_", 6)
        speed = vector(row, "ur_actual_TCP_speed_", 6)
        force_tcp = np.asarray([finite(row, f"_step4e_force_t_{axis}") for axis in "xyz"], dtype=float)
        reaction = np.asarray(
            [finite(row, f"_step4e_control_normal_b_{axis}") for axis in "xyz"],
            dtype=float,
        )
        prepared.append(
            PreparedReplayRow(
                q=q,
                qd=qd,
                pose=_tuple6(pose),
                speed=_tuple6(speed),
                force_tcp=_tuple3(force_tcp),
                reaction=reaction,
                reaction_tuple=_tuple3(reaction),
                desired_x_m=finite(row, "_step4e_desired_x_m"),
                desired_y_m=finite(row, "_step4e_desired_y_m"),
                desired_vx_m_s=finite(row, "_step4e_desired_vx_m_s"),
                desired_vy_m_s=finite(row, "_step4e_desired_vy_m_s"),
            )
        )
    return prepared


def wait_until(deadline_s: float, *, spin_window_s: float = 0.0005) -> None:
    """Absolute-deadline sleep with a short spin tail for 500 Hz pacing."""

    while True:
        remaining = deadline_s - time.perf_counter()
        if remaining <= 0.0:
            return
        if remaining > spin_window_s:
            time.sleep(remaining - spin_window_s)
            continue
        while time.perf_counter() < deadline_s:
            pass
        return


def distribution(values: Sequence[float], deadline_ms: float = 2.0) -> dict[str, Any]:
    array = np.asarray(values, dtype=float)
    finite_values = array[np.isfinite(array)]
    return {
        "samples": int(array.size),
        "nonfinite_count": int(array.size - finite_values.size),
        "mean_ms": float(np.mean(finite_values)) if finite_values.size else None,
        "p95_ms": float(np.percentile(finite_values, 95)) if finite_values.size else None,
        "p99_ms": float(np.percentile(finite_values, 99)) if finite_values.size else None,
        "max_ms": float(np.max(finite_values)) if finite_values.size else None,
        "compute_deadline_miss_count": int(np.count_nonzero(finite_values >= deadline_ms)),
    }


def value_distribution(values: Sequence[float]) -> dict[str, Any]:
    """Summarize deferred control evidence after leaving the measured loop."""

    array = np.asarray(values, dtype=float)
    finite_values = array[np.isfinite(array)]
    return {
        "samples": int(array.size),
        "nonfinite_count": int(array.size - finite_values.size),
        "min": float(np.min(finite_values)) if finite_values.size else None,
        "mean": float(np.mean(finite_values)) if finite_values.size else None,
        "p99": float(np.percentile(finite_values, 99)) if finite_values.size else None,
        "max": float(np.max(finite_values)) if finite_values.size else None,
    }


def runtime_environment() -> dict[str, Any]:
    """Bind the process scheduling context used for wall-clock evidence."""

    try:
        affinity: list[int] | None = sorted(int(value) for value in os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = None
    try:
        scheduler_policy: int | None = int(os.sched_getscheduler(0))
    except (AttributeError, OSError):
        scheduler_policy = None
    try:
        scheduler_priority: int | None = int(os.sched_getparam(0).sched_priority)
    except (AttributeError, OSError):
        scheduler_priority = None
    try:
        nice_value: int | None = int(os.getpriority(os.PRIO_PROCESS, 0))
    except (AttributeError, OSError):
        nice_value = None
    cupy_module = sys.modules.get("cupy")
    try:
        cuda_runtime_version: int | None = int(
            cupy_module.cuda.runtime.runtimeGetVersion()
        )
        cuda_driver_version: int | None = int(
            cupy_module.cuda.runtime.driverGetVersion()
        )
    except (AttributeError, RuntimeError):
        cuda_runtime_version = None
        cuda_driver_version = None
    try:
        from cupy_backends.cuda.libs import nvrtc

        nvrtc_version: list[int] | None = [int(value) for value in nvrtc.getVersion()]
    except (ImportError, RuntimeError):
        nvrtc_version = None

    def package_version(name: str) -> str | None:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None

    def rlimit(name: str) -> list[int] | None:
        identifier = getattr(resource, name, None)
        if identifier is None:
            return None
        soft, hard = resource.getrlimit(identifier)
        return [int(soft), int(hard)]

    return {
        "nice": nice_value,
        "scheduler_policy": scheduler_policy,
        "scheduler_policy_name": {
            getattr(os, "SCHED_OTHER", 0): "SCHED_OTHER",
            getattr(os, "SCHED_FIFO", 1): "SCHED_FIFO",
            getattr(os, "SCHED_RR", 2): "SCHED_RR",
        }.get(scheduler_policy, "unknown"),
        "scheduler_priority": scheduler_priority,
        "scheduler_limits": {
            "rtprio": rlimit("RLIMIT_RTPRIO"),
            "rttime_us": rlimit("RLIMIT_RTTIME"),
            "memlock_bytes": rlimit("RLIMIT_MEMLOCK"),
        },
        "cpu_affinity": affinity,
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "pythonpath": os.environ.get("PYTHONPATH"),
        "ld_library_path": os.environ.get("LD_LIBRARY_PATH"),
        "cuda": {
            "runtime_version": cuda_runtime_version,
            "driver_version": cuda_driver_version,
            "nvrtc_version": nvrtc_version,
            "runtime_package": package_version("nvidia-cuda-runtime-cu12"),
            "nvrtc_package": package_version("nvidia-cuda-nvrtc-cu12"),
            "nvjitlink_package": package_version("nvidia-nvjitlink-cu12"),
        },
        "versions": {
            name: str(getattr(sys.modules.get(name), "__version__", "unavailable"))
            for name in ("numpy", "cupy", "pinocchio")
        },
        "thread_environment": {
            name: os.environ.get(name) for name in THREAD_ENV_NAMES
        },
    }


def gpu_device_metadata() -> dict[str, Any]:
    """Capture stable CuPy device identity outside every measured loop."""

    cupy_module = sys.modules.get("cupy")
    if cupy_module is None:
        raise RuntimeError("CuPy device metadata requires the loaded cupy module")
    device_id = int(cupy_module.cuda.runtime.getDevice())
    properties = cupy_module.cuda.runtime.getDeviceProperties(device_id)

    def property_value(name: str) -> Any:
        return properties.get(name, properties.get(name.encode("ascii")))

    raw_name = property_value("name")
    name = (
        raw_name.decode("utf-8", errors="replace")
        if isinstance(raw_name, bytes)
        else str(raw_name or "")
    ).strip("\x00")
    return {
        "device_id": device_id,
        "name": name,
        "compute_capability": [
            int(property_value("major")),
            int(property_value("minor")),
        ],
        "total_memory_bytes": int(property_value("totalGlobalMem")),
        "pci_bus_id": int(property_value("pciBusID")),
        "pci_device_id": int(property_value("pciDeviceID")),
    }


NVIDIA_SMI_FIELDS = (
    "driver_version",
    "name",
    "pci.bus_id",
    "clocks.current.sm",
    "clocks.current.memory",
    "temperature.gpu",
    "utilization.gpu",
    "power.draw",
    "persistence_mode",
)


def nvidia_smi_snapshot(device_id: int) -> dict[str, Any]:
    """Capture one bounded nvidia-smi snapshot outside measured loops."""

    command = [
        "nvidia-smi",
        "--id",
        str(int(device_id)),
        f"--query-gpu={','.join(NVIDIA_SMI_FIELDS)}",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "ok": False,
            "command": command,
            "error_type": type(exc).__name__,
        }
    rows = [row.strip() for row in completed.stdout.splitlines() if row.strip()]
    values = [value.strip() for value in rows[0].split(",")] if rows else []
    ok = completed.returncode == 0 and len(rows) == 1 and len(values) == len(
        NVIDIA_SMI_FIELDS
    )
    return {
        "ok": ok,
        "command": command,
        "returncode": int(completed.returncode),
        "fields": list(NVIDIA_SMI_FIELDS),
        "values": dict(zip(NVIDIA_SMI_FIELDS, values)) if ok else {},
        "stderr": completed.stderr.strip(),
    }


def deferred_control_summary(
    buffer: Any,
    deferred_fields: Sequence[str],
) -> dict[str, Any]:
    """Summarize command-path and reference-ramp evidence after the loop."""

    field = {name: index for index, name in enumerate(deferred_fields)}
    values = buffer.numeric[: buffer.count]
    accepted = values[:, field["accepted"]]
    ramp_active = values[:, field["reference_ramp_active"]]
    raw_desired = values[
        :,
        field["raw_desired_twist_0"] : field["raw_desired_twist_5"] + 1,
    ]
    governed_desired = values[
        :,
        field["governed_desired_twist_0"] : field["governed_desired_twist_5"] + 1,
    ]
    raw_to_governed_error = np.linalg.norm(
        raw_desired - governed_desired,
        axis=1,
    )
    desired_approach = values[:, field["desired_approach_m_s"]]
    predicted_approach = values[:, field["predicted_approach_m_s"]]
    normal_sign_mismatch = np.logical_and(
        desired_approach > 0.0,
        predicted_approach <= 0.0,
    )
    mismatch_indices = np.flatnonzero(normal_sign_mismatch)
    execute_count = sum(
        action == "execute" for action in buffer.actions[: buffer.count]
    )
    safe_hold_count = sum(
        action == "safe_hold" for action in buffer.actions[: buffer.count]
    )
    accepted_count = int(np.count_nonzero(accepted == 1.0))
    return {
        "samples": int(buffer.count),
        "accepted_count": accepted_count,
        "execute_count": int(execute_count),
        "safe_hold_count": int(safe_hold_count),
        "execute_path_proven": bool(
            buffer.count > 0
            and accepted_count == buffer.count
            and execute_count == buffer.count
        ),
        "reference_ramp_active_count": int(
            np.count_nonzero(ramp_active == 1.0)
        ),
        "reference_ramp_scale": value_distribution(
            values[:, field["reference_ramp_scale"]]
        ),
        "raw_to_governed_twist_error_norm": value_distribution(
            raw_to_governed_error
        ),
        "normal_sign_mismatch": {
            "total_count": int(mismatch_indices.size),
            "first_tick": bool(normal_sign_mismatch[0]) if buffer.count else False,
            "first_10_ticks_count": int(
                np.count_nonzero(normal_sign_mismatch[:10])
            ),
            "first_50_ticks_count": int(
                np.count_nonzero(normal_sign_mismatch[:50])
            ),
            "first_index": (
                int(mismatch_indices[0]) if mismatch_indices.size else None
            ),
        },
        "residual_norm": value_distribution(
            values[:, field["residual_norm"]]
        ),
        "desired_approach_m_s": value_distribution(
            values[:, field["desired_approach_m_s"]]
        ),
        "predicted_approach_m_s": value_distribution(
            values[:, field["predicted_approach_m_s"]]
        ),
    }


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    if args.solver_samples < 1 or args.tick_samples < 1 or args.safe_hold_samples < 1:
        raise SystemExit("solver/tick/safe-hold sample counts must be positive")
    if args.component_diagnostic_samples < 0:
        raise SystemExit("component diagnostic sample count must be non-negative")
    if args.component_outlier_threshold_ms <= 0.0 or args.component_outlier_ring_size < 1:
        raise SystemExit("component outlier threshold/ring size must be positive")
    requested_profile = build_profile_selection(
        args.inner_iterations
    )["requested_profile"]

    root = args.experiment_root.resolve()
    tools_dir = root / "tools"
    sys.path.insert(0, str(tools_dir))
    from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver
    import step5c_calibrated_kinematics_audit as kinematics
    from step5d_paper_outer_loop import (
        Step5dOuterLoopConfig,
        Step5dOuterLoopInputs,
        Step5dOuterLoopState,
        compute_step5d_outer_loop,
        rnn_target_state_from_outer_loop,
    )
    from step5d_control_contract import (
        build_slew_compatible_reference,
        ControlCandidate,
        DeferredV30Diagnostics,
        SafetyEnvelope,
        Step5dObservation,
        StrictRnnControlPolicy,
        V30_DEFERRED_NUMERIC_FIELDS,
    )
    from kunwei_rtde_bridge import (
        limit_step5d_live_xdot,
        scale_step5d_xdot_for_joint_feasibility,
        step5d_omega_bounds,
        step5d_tcp_jacobian_base,
        step5d_v30_contract_pipeline,
    )

    module_sha_fields = {
        "contact_semantics_sha256": "contact_semantics",
        "solver_sha256": "step5c_strict_rnn",
        "outer_loop_sha256": "step5d_paper_outer_loop",
        "control_contract_sha256": "step5d_control_contract",
        "runtime_interface_sha256": "step5d_runtime_interface",
        "kinematics_sha256": "step5c_calibrated_kinematics_audit",
        "bridge_sha256": "kunwei_rtde_bridge",
    }
    auxiliary_source_binding = globals().get("__v30_auxiliary_source_binding__")
    if not isinstance(auxiliary_source_binding, dict):
        auxiliary_source_binding = {}
    source_binding = {
        "delivery": globals().get("__v30_source_delivery__"),
        **{
            field: getattr(
                sys.modules.get(module_name),
                "__v30_source_sha256__",
                None,
            )
            for field, module_name in module_sha_fields.items()
        },
        "harness_sha256": globals().get("__v30_source_sha256__"),
        **auxiliary_source_binding,
    }
    required_auxiliary_source_fields = (
        "bundler_sha256",
        "aggregator_sha256",
        "readiness_builder_sha256",
    )
    if source_binding["delivery"] != "stdin_bundle" or any(
        not isinstance(source_binding.get(key), str)
        or len(str(source_binding.get(key))) != 64
        for key in (
            *module_sha_fields,
            "harness_sha256",
            *required_auxiliary_source_fields,
        )
    ):
        raise RuntimeError(
            "v30 timing refuses unbound remote filesystem sources; run the local stdin bundler"
        )

    replay_csv = args.replay_csv if args.replay_csv.is_absolute() else root / args.replay_csv
    artifact_binding = {
        "replay_csv": {
            "path": str(replay_csv),
            "size": replay_csv.stat().st_size,
            "sha256": sha256_path(replay_csv),
        },
        "paper_truth": {
            "path": str(root / "config" / "step5d_liveprep_solver_gate.json"),
            "sha256": sha256_path(root / "config" / "step5d_liveprep_solver_gate.json"),
        },
        "profile_selection": {
            "path": str(root / "config" / "step5d_v30_profile_selection.json"),
            "sha256": sha256_path(
                root / "config" / "step5d_v30_profile_selection.json"
            ),
        },
        "stage_table": {
            "path": str(root / "config" / "step5_stage_table.json"),
            "sha256": sha256_path(root / "config" / "step5_stage_table.json"),
        },
        "calibration_yaml": {
            "path": str(kinematics.DEFAULT_CALIBRATION_YAML),
            "sha256": sha256_path(kinematics.DEFAULT_CALIBRATION_YAML),
        },
        "ur_xacro": {
            "path": str(kinematics.DEFAULT_XACRO_PATH),
            "sha256": sha256_path(kinematics.DEFAULT_XACRO_PATH),
        },
    }
    rows = load_rows(replay_csv)
    model_started = time.perf_counter()
    model_bundle = kinematics.build_calibrated_model()
    finite_rows = kinematics.finite_run_rows(replay_csv)
    tcp_offset = np.asarray(kinematics.infer_tcp_offset(model_bundle, finite_rows)["mean"], dtype=float)
    model_prepare_ms = (time.perf_counter() - model_started) * 1000.0
    prepared_rows = prepare_rows(rows)

    solver_started = time.perf_counter()
    solver = StrictTaseRnnSolver(
        StrictRnnConfig(
            paper_truth_path=root / "config" / "step5d_liveprep_solver_gate.json",
            qdot_limit_rad_s=requested_profile["qdot_cap_rad_s"],
            epsilon=requested_profile["epsilon"],
            sigr_exponent_r=requested_profile["sigr_exponent_r"],
            inner_iterations=requested_profile["inner_iterations"],
            backend=requested_profile["backend"],
        )
    )
    effective_profile = {
        "backend": solver.config.backend,
        "inner_iterations": int(solver.config.inner_iterations),
        "epsilon": float(solver.config.epsilon),
        "sigr_exponent_r": float(solver.config.sigr_exponent_r),
        "qdot_cap_rad_s": float(solver.config.qdot_limit_rad_s),
        "control_hz": float(requested_profile["control_hz"]),
    }
    profile_selection = build_profile_selection(
        args.inner_iterations,
        effective_profile=effective_profile,
    )
    if not solver.cupy_host_staging_pinned:
        raise RuntimeError("v30 timing requires page-locked CuPy host staging")
    if not solver.cupy_dedicated_stream:
        raise RuntimeError("v30 timing requires one dedicated nonblocking CuPy stream")
    parallel_equivalence = solver.cupy_parallel_equivalence or {}
    if (
        parallel_equivalence.get("bitwise_equal") is not True
        or int(parallel_equivalence.get("samples", 0)) < 100
        or parallel_equivalence.get("parallel_block_threads") != 6
    ):
        raise RuntimeError("v30 timing requires block-6/serial startup equivalence")
    cupy_precompile_ms = (time.perf_counter() - solver_started) * 1000.0
    gpu_device = gpu_device_metadata()
    nvidia_smi_start = nvidia_smi_snapshot(gpu_device["device_id"])

    config = Step5dOuterLoopConfig(
        kp=4.0,
        ko=0.5,
        kf=1.0,
        Md_scalar=240.0,
        Bd_scalar=11_000.0,
        force_target_n=12.0,
        delay_T_s=0.002,
        force_sign_convention="step5_step6_positive_normal_load",
    )

    def tick_inputs(
        row: PreparedReplayRow,
        state: Any,
    ) -> tuple[Any, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        q = row.q
        qd = row.qd
        pose = row.pose
        outer = compute_step5d_outer_loop(
            config,
            state,
            Step5dOuterLoopInputs(
                tcp_pose_base=pose,
                tcp_speed_base=row.speed,
                force_tcp_n=row.force_tcp,
                x_pd_base=(row.desired_x_m, row.desired_y_m, pose[2]),
                xdot_pd_base=(row.desired_vx_m_s, row.desired_vy_m_s, 0.0),
                dt_s=0.002,
                cmd_valid=True,
                control_reaction_normal_base=row.reaction_tuple,
            ),
            include_diagnostics="compact",
        )
        jacobian = step5d_tcp_jacobian_base(model_bundle, q, tcp_offset)
        lower, upper = step5d_omega_bounds(
            q,
            model_bundle.model.lowerPositionLimit,
            model_bundle.model.upperPositionLimit,
            alpha_s_inv=1.0,
            qdot_limit_rad_s=effective_profile["qdot_cap_rad_s"],
        )
        limited, _ = limit_step5d_live_xdot(
            np.asarray(outer.xdot_c, dtype=float),
            max_linear_m_s=0.004,
            max_angular_rad_s=0.015,
        )
        feasible, _ = scale_step5d_xdot_for_joint_feasibility(
            limited,
            jacobian,
            qdot_cap_rad_s=effective_profile["qdot_cap_rad_s"],
            safety=0.9,
        )
        target = rnn_target_state_from_outer_loop(
            outer,
            J=jacobian,
            omega_minus=lower,
            omega_plus=upper,
            dt_s=0.002,
            epsilon=effective_profile["epsilon"],
            r=effective_profile["sigr_exponent_r"],
        )
        target["xdot_c"] = feasible
        return outer, q, qd, jacobian, row.reaction, target

    def contract_observation(
        *,
        row: PreparedReplayRow,
        outer: Any,
        q: np.ndarray,
        qd: np.ndarray,
        jacobian: np.ndarray,
        reaction: np.ndarray,
        target: Mapping[str, Any],
        sequence: int,
        timestamp_s: float,
        force_nonpressing_desired: bool = False,
    ) -> Any:
        reaction_tuple = _tuple3(reaction)
        approach_tuple = tuple(-value for value in reaction_tuple)
        desired = np.asarray(target["xdot_c"], dtype=float).copy()
        if force_nonpressing_desired:
            desired[:3] = 0.001 * reaction
        return Step5dObservation(
            sequence=sequence,
            timestamp_s=timestamp_s,
            q=_tuple6(q),
            qd=_tuple6(qd),
            tcp_pose=row.pose,
            tcp_twist=row.speed,
            wrench=(*row.force_tcp, 0.0, 0.0, 0.0),
            jacobian=tuple(  # type: ignore[arg-type]
                tuple(float(value) for value in values) for values in jacobian
            ),
            desired_twist=_tuple6(desired),
            reaction_normal=reaction_tuple,
            approach_normal=approach_tuple,  # type: ignore[arg-type]
            command_frame="base",
            normal_frame="base",
            path_time_s=sequence * 0.002,
            force_error_n=float(outer.diagnostics["e_f"]),
            orientation_error_rad=float(
                outer.diagnostics["outer_orientation_angle_rad"]
            ),
            omega_minus=_tuple6(np.asarray(target["omega_minus"], dtype=float)),
            omega_plus=_tuple6(np.asarray(target["omega_plus"], dtype=float)),
            dt_s=0.002,
        )

    outer_state = Step5dOuterLoopState()
    first_outer, first_q, first_qd, first_jacobian, _, first_target = tick_inputs(prepared_rows[0], outer_state)
    solver.warm_start(
        J=first_jacobian,
        xdot_c=first_target["xdot_c"],
        omega_minus=first_target["omega_minus"],
        omega_plus=first_target["omega_plus"],
    )
    # Avoid list growth and cyclic-GC scans in the measured loops.  Reference
    # counting remains active; the previous GC state is always restored.
    solver_ms = np.empty(args.solver_samples, dtype=np.float64)
    solver_batch_reentry_count = (args.solver_samples - 1) // SOLVER_BATCH_SIZE
    solver_batch_reentry_ms = np.empty(
        solver_batch_reentry_count,
        dtype=np.float64,
    )
    full_tick_ms = np.empty(args.tick_samples, dtype=np.float64)
    safe_hold_ms = np.empty(args.safe_hold_samples, dtype=np.float64)
    solver_miss_indices = np.empty(DEADLINE_EVENT_CAPACITY, dtype=np.int64)
    solver_batch_reentry_miss_indices = np.empty(
        solver_batch_reentry_count,
        dtype=np.int64,
    )
    full_compute_miss_indices = np.empty(DEADLINE_EVENT_CAPACITY, dtype=np.int64)
    full_schedule_miss_indices = np.empty(DEADLINE_EVENT_CAPACITY, dtype=np.int64)
    safe_compute_miss_indices = np.empty(DEADLINE_EVENT_CAPACITY, dtype=np.int64)
    safe_schedule_miss_indices = np.empty(DEADLINE_EVENT_CAPACITY, dtype=np.int64)
    solver_miss_total = 0
    solver_batch_reentry_miss_total = 0
    full_compute_miss_total = 0
    full_schedule_miss_total = 0
    safe_compute_miss_total = 0
    safe_schedule_miss_total = 0
    full_compute_consecutive = 0
    full_compute_max_consecutive = 0
    full_schedule_consecutive = 0
    full_schedule_max_consecutive = 0
    safe_compute_consecutive = 0
    safe_compute_max_consecutive = 0
    safe_schedule_consecutive = 0
    safe_schedule_max_consecutive = 0
    component_fields = (
        "cpu_pack_ms",
        "h2d_enqueue_ms",
        "kernel_enqueue_ms",
        "d2h_enqueue_ms",
        "host_completion_wait_ms",
        "cuda_h2d_ms",
        "cuda_kernel_ms",
        "cuda_d2h_ms",
        "cupy_component_wall_ms",
        "solve_api_wall_ms",
    )
    component_samples = max(0, int(args.component_diagnostic_samples))
    component_values = np.empty(
        (component_samples, len(component_fields)),
        dtype=np.float64,
    )
    outlier_ring_size = max(1, int(args.component_outlier_ring_size))
    component_outlier_indices = np.empty(outlier_ring_size, dtype=np.int64)
    component_outlier_values = np.empty(
        (outlier_ring_size, len(component_fields)),
        dtype=np.float64,
    )
    component_outlier_total = 0
    policy = StrictRnnControlPolicy(solver)
    safety_envelope = SafetyEnvelope(
        qdot_cap_rad_s=effective_profile["qdot_cap_rad_s"],
        max_normal_tracking_error_m_s=5e-4,
        max_residual_norm=1e-3,
    )
    full_tick_deferred = DeferredV30Diagnostics(capacity=args.tick_samples)
    safe_hold_deferred = DeferredV30Diagnostics(capacity=args.safe_hold_samples)
    gc_was_enabled = gc.isenabled()
    gc.collect()
    gc.disable()
    try:
        solver_batch_reentry_index = 0
        for index in range(args.solver_samples):
            if index and index % SOLVER_BATCH_SIZE == 0:
                # Keep the full-speed microbenchmark from exhausting Linux's
                # realtime runtime budget.  The yield itself remains outside
                # every solve interval.  The first solve after each yield is
                # timed and retained separately as batch-reentry evidence; it
                # is not silently discarded or mixed into the 10k steady
                # solver samples below.  The 500 Hz full/safe loops are
                # unchanged.
                time.sleep(SOLVER_BATCH_YIELD_S)
                reentry_started = time.perf_counter()
                solver.solve(
                    actual_q=first_q,
                    actual_qd=first_qd,
                    target_state=first_target,
                )
                solver_batch_reentry_ms[solver_batch_reentry_index] = (
                    time.perf_counter() - reentry_started
                ) * 1000.0
                if (
                    solver_batch_reentry_ms[solver_batch_reentry_index]
                    >= DEADLINE_MS
                ):
                    solver_batch_reentry_miss_indices[
                        solver_batch_reentry_miss_total
                    ] = solver_batch_reentry_index
                    solver_batch_reentry_miss_total += 1
                solver_batch_reentry_index += 1
            steady_started = time.perf_counter()
            solver.solve(actual_q=first_q, actual_qd=first_qd, target_state=first_target)
            solver_ms[index] = (time.perf_counter() - steady_started) * 1000.0
            if solver_ms[index] >= DEADLINE_MS:
                if solver_miss_total < DEADLINE_EVENT_CAPACITY:
                    solver_miss_indices[solver_miss_total] = index
                solver_miss_total += 1
        if solver_batch_reentry_index != solver_batch_reentry_count:
            raise RuntimeError("solver batch-reentry accounting mismatch")
        first_post_warm_ms = float(solver_ms[0])

        if component_samples:
            solver.reset_state()
            solver.warm_start(
                J=first_jacobian,
                xdot_c=first_target["xdot_c"],
                omega_minus=first_target["omega_minus"],
                omega_plus=first_target["omega_plus"],
            )
            for index in range(component_samples):
                _result, components = solver.solve_component_timed(
                    actual_q=first_q,
                    actual_qd=first_qd,
                    target_state=first_target,
                )
                for field_index, field in enumerate(component_fields):
                    component_values[index, field_index] = float(components[field])
                if components["solve_api_wall_ms"] >= float(
                    args.component_outlier_threshold_ms
                ):
                    slot = component_outlier_total % outlier_ring_size
                    component_outlier_indices[slot] = index
                    component_outlier_values[slot, :] = component_values[index, :]
                    component_outlier_total += 1

        solver.reset_state()
        outer_state = Step5dOuterLoopState()
        previous_qdot: tuple[float, float, float, float, float, float] | None = None
        full_tick_schedule_deadline_miss_count = 0
        full_tick_schedule_max_lateness_ms = 0.0
        full_tick_reason_counts: dict[str, int] = {}
        schedule_start = time.perf_counter()
        period_s = 1.0 / effective_profile["control_hz"]
        for index in range(args.tick_samples):
            release = schedule_start + index * period_s
            if args.pace_500hz and index:
                wait_until(release)
            row = prepared_rows[index % len(prepared_rows)]
            tick_started = time.perf_counter()
            outer, q, qd, jacobian, reaction, target = tick_inputs(row, outer_state)
            outer_state = outer.next_state
            observation = contract_observation(
                row=row,
                outer=outer,
                q=q,
                qd=qd,
                jacobian=jacobian,
                reaction=reaction,
                target=target,
                sequence=index,
                timestamp_s=tick_started,
            )
            governed_observation = build_slew_compatible_reference(
                observation,
                previous_qdot=previous_qdot,
            )
            if index == 0:
                solver.warm_start(
                    J=governed_observation.jacobian,
                    xdot_c=governed_observation.desired_twist,
                    omega_minus=governed_observation.omega_minus,
                    omega_plus=governed_observation.omega_plus,
                )
            raw_candidate = policy.compute(governed_observation)
            candidate, _dls_shadow, decision, command = step5d_v30_contract_pipeline(
                governed_observation,
                raw_candidate,
                previous_qdot=previous_qdot,
                safety_envelope=safety_envelope,
                deferred_diagnostics=full_tick_deferred,
            )
            register_values = command.as_register_values()
            if register_values[47] != 524.0 or register_values[43] != 1.0:
                raise RuntimeError("runtime-shaped full tick produced invalid register contract")
            previous_qdot = decision.qdot if decision.accepted else None
            full_tick_reason_counts[decision.reason] = (
                full_tick_reason_counts.get(decision.reason, 0) + 1
            )
            finished = time.perf_counter()
            full_tick_ms[index] = (finished - tick_started) * 1000.0
            if full_tick_ms[index] >= DEADLINE_MS:
                if full_compute_miss_total < DEADLINE_EVENT_CAPACITY:
                    full_compute_miss_indices[full_compute_miss_total] = index
                full_compute_miss_total += 1
                full_compute_consecutive += 1
                full_compute_max_consecutive = max(
                    full_compute_max_consecutive,
                    full_compute_consecutive,
                )
            else:
                full_compute_consecutive = 0
            deadline = release + period_s
            lateness_ms = max(0.0, (finished - deadline) * 1000.0)
            if lateness_ms > 0.0:
                if full_schedule_miss_total < DEADLINE_EVENT_CAPACITY:
                    full_schedule_miss_indices[full_schedule_miss_total] = index
                full_schedule_miss_total += 1
                full_schedule_consecutive += 1
                full_schedule_max_consecutive = max(
                    full_schedule_max_consecutive,
                    full_schedule_consecutive,
                )
                full_tick_schedule_deadline_miss_count += 1
                full_tick_schedule_max_lateness_ms = max(
                    full_tick_schedule_max_lateness_ms,
                    lateness_ms,
                )
            else:
                full_schedule_consecutive = 0

        full_tick_elapsed_wall_s = time.perf_counter() - schedule_start

        solver.reset_state()
        outer_state = Step5dOuterLoopState()
        safe_hold_reason_counts: dict[str, int] = {}
        safe_hold_schedule_deadline_miss_count = 0
        safe_hold_schedule_max_lateness_ms = 0.0
        safe_hold_schedule_start = time.perf_counter()
        for index in range(args.safe_hold_samples):
            release = safe_hold_schedule_start + index * period_s
            if args.pace_500hz and index:
                wait_until(release)
            row = prepared_rows[index % len(prepared_rows)]
            started = time.perf_counter()
            outer, q, qd, jacobian, reaction, target = tick_inputs(row, outer_state)
            outer_state = outer.next_state
            if index == 0:
                solver.warm_start(
                    J=jacobian,
                    xdot_c=target["xdot_c"],
                    omega_minus=target["omega_minus"],
                    omega_plus=target["omega_plus"],
                )
            observation = contract_observation(
                row=row,
                outer=outer,
                q=q,
                qd=qd,
                jacobian=jacobian,
                reaction=reaction,
                target=target,
                sequence=index,
                timestamp_s=started,
                force_nonpressing_desired=True,
            )
            raw_candidate = policy.compute(observation)
            _candidate, _dls_shadow, decision, command = step5d_v30_contract_pipeline(
                observation,
                raw_candidate,
                previous_qdot=None,
                safety_envelope=safety_envelope,
                deferred_diagnostics=safe_hold_deferred,
            )
            register_values = command.as_register_values()
            if (
                decision.action != "safe_hold"
                or command.qdot != (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
                or register_values[43] != 1.0
                or register_values[28] != 0.0
                or register_values[47] != 524.0
            ):
                raise RuntimeError("runtime-shaped safe-hold register contract failed")
            safe_hold_reason_counts[decision.reason] = (
                safe_hold_reason_counts.get(decision.reason, 0) + 1
            )
            finished = time.perf_counter()
            safe_hold_ms[index] = (finished - started) * 1000.0
            if safe_hold_ms[index] >= DEADLINE_MS:
                if safe_compute_miss_total < DEADLINE_EVENT_CAPACITY:
                    safe_compute_miss_indices[safe_compute_miss_total] = index
                safe_compute_miss_total += 1
                safe_compute_consecutive += 1
                safe_compute_max_consecutive = max(
                    safe_compute_max_consecutive,
                    safe_compute_consecutive,
                )
            else:
                safe_compute_consecutive = 0
            deadline = release + period_s
            lateness_ms = max(0.0, (finished - deadline) * 1000.0)
            if lateness_ms > 0.0:
                if safe_schedule_miss_total < DEADLINE_EVENT_CAPACITY:
                    safe_schedule_miss_indices[safe_schedule_miss_total] = index
                safe_schedule_miss_total += 1
                safe_schedule_consecutive += 1
                safe_schedule_max_consecutive = max(
                    safe_schedule_max_consecutive,
                    safe_schedule_consecutive,
                )
                safe_hold_schedule_deadline_miss_count += 1
                safe_hold_schedule_max_lateness_ms = max(
                    safe_hold_schedule_max_lateness_ms,
                    lateness_ms,
                )
            else:
                safe_schedule_consecutive = 0
        safe_hold_elapsed_wall_s = time.perf_counter() - safe_hold_schedule_start
    finally:
        if gc_was_enabled:
            gc.enable()
    nvidia_smi_end = nvidia_smi_snapshot(gpu_device["device_id"])

    component_diagnostics: dict[str, Any] | None = None
    if component_samples:
        retained = min(component_outlier_total, outlier_ring_size)
        if component_outlier_total <= outlier_ring_size:
            slots = list(range(retained))
        else:
            oldest = component_outlier_total % outlier_ring_size
            slots = [
                (oldest + offset) % outlier_ring_size
                for offset in range(retained)
            ]
        outlier_ring = []
        for slot in slots:
            outlier_ring.append(
                {
                    "sample_index": int(component_outlier_indices[slot]),
                    **{
                        field: float(component_outlier_values[slot, field_index])
                        for field_index, field in enumerate(component_fields)
                    },
                }
            )
        component_diagnostics = {
            "mode": "diagnostic_only_not_acceptance_timing",
            "samples": component_samples,
            "outlier_threshold_ms": float(args.component_outlier_threshold_ms),
            "outlier_total": component_outlier_total,
            "outlier_ring_capacity": outlier_ring_size,
            "outlier_ring": outlier_ring,
            "all_samples_retained_in_aggregates": True,
            "outlier_samples_discarded_from_aggregates": False,
            "components": {
                field: distribution(component_values[:, field_index])
                for field_index, field in enumerate(component_fields)
            },
        }

    def miss_event_summary(
        indices: np.ndarray,
        total: int,
        *,
        capacity: int = DEADLINE_EVENT_CAPACITY,
        max_consecutive: int | None = None,
    ) -> dict[str, Any]:
        retained = min(int(total), int(capacity))
        result = {
            "total": int(total),
            "retained_indices": indices[:retained].tolist(),
            "capacity": int(capacity),
            "overflowed": int(total) > int(capacity),
        }
        if max_consecutive is not None:
            result["max_consecutive"] = int(max_consecutive)
        return result

    payload: dict[str, Any] = {
        "schema_version": "step5d_v30_remote_timing_raw_v2",
        "profile": effective_profile,
        "profile_sha256": profile_selection["effective_profile_sha256"],
        "profile_selection": profile_selection,
        "runtime_environment": runtime_environment(),
        "gpu_device": gpu_device,
        "nvidia_smi": {
            "capture_scope": "outside_measured_solver_and_500hz_loops",
            "start": nvidia_smi_start,
            "end": nvidia_smi_end,
        },
        "source_binding": source_binding,
        "artifact_binding": artifact_binding,
        "source_csv": str(replay_csv),
        "source_rows": len(rows),
        "model_prepare_ms": model_prepare_ms,
        "cupy_precompile_ms": cupy_precompile_ms,
        "cupy_host_staging_pinned": solver.cupy_host_staging_pinned,
        "cupy_dedicated_stream": solver.cupy_dedicated_stream,
        "cupy_stream_priority": solver.cupy_stream_priority,
        "cupy_stream_priority_capability": solver.cupy_stream_priority_capability,
        "cupy_parallel_equivalence": parallel_equivalence,
        "cupy_component_diagnostics": component_diagnostics,
        "precompile_outside_control_loop": True,
        "first_post_warm_ms": first_post_warm_ms,
        "solver_microbenchmark_pacing": {
            "mode": "unmeasured_fixed_batch_yield_with_measured_reentry",
            "batch_size": SOLVER_BATCH_SIZE,
            "yield_s": SOLVER_BATCH_YIELD_S,
            "yield_included_in_single_solve_latency": False,
            "steady_samples": int(args.solver_samples),
            "steady_samples_per_batch": SOLVER_BATCH_SIZE,
            "measured_reentry_after_each_yield": True,
            "reentry_samples": int(solver_batch_reentry_count),
            "reentry_sample_boundaries": list(
                range(
                    SOLVER_BATCH_SIZE,
                    args.solver_samples,
                    SOLVER_BATCH_SIZE,
                )
            ),
            "reentry_included_in_steady_solver_summary": False,
            "all_reentry_samples_retained_raw": True,
            "reason": "avoid_linux_sched_fifo_runtime_throttling_during_10k_stress",
            "full_tick_loop_affected": False,
            "safe_hold_loop_affected": False,
        },
        "solver": distribution(solver_ms),
        "solver_batch_reentry": distribution(solver_batch_reentry_ms),
        "solver_batch_reentry_ms": solver_batch_reentry_ms.tolist(),
        "full_tick": distribution(full_tick_ms),
        "safe_hold": distribution(safe_hold_ms),
        "full_tick_schedule_deadline_miss_count": full_tick_schedule_deadline_miss_count,
        "full_tick_schedule_max_lateness_ms": full_tick_schedule_max_lateness_ms,
        "safe_hold_schedule_deadline_miss_count": safe_hold_schedule_deadline_miss_count,
        "safe_hold_schedule_max_lateness_ms": safe_hold_schedule_max_lateness_ms,
        "full_tick_reason_counts": dict(sorted(full_tick_reason_counts.items())),
        "safe_hold_reason_counts": dict(sorted(safe_hold_reason_counts.items())),
        "full_tick_control_diagnostics": deferred_control_summary(
            full_tick_deferred,
            V30_DEFERRED_NUMERIC_FIELDS,
        ),
        "safe_hold_control_diagnostics": deferred_control_summary(
            safe_hold_deferred,
            V30_DEFERRED_NUMERIC_FIELDS,
        ),
        "deadline_miss_diagnostics": {
            "solver_compute": miss_event_summary(
                solver_miss_indices,
                solver_miss_total,
            ),
            "solver_batch_reentry_compute": miss_event_summary(
                solver_batch_reentry_miss_indices,
                solver_batch_reentry_miss_total,
                capacity=solver_batch_reentry_count,
            ),
            "full_tick_compute": miss_event_summary(
                full_compute_miss_indices,
                full_compute_miss_total,
                max_consecutive=full_compute_max_consecutive,
            ),
            "full_tick_schedule": miss_event_summary(
                full_schedule_miss_indices,
                full_schedule_miss_total,
                max_consecutive=full_schedule_max_consecutive,
            ),
            "safe_hold_compute": miss_event_summary(
                safe_compute_miss_indices,
                safe_compute_miss_total,
                max_consecutive=safe_compute_max_consecutive,
            ),
            "safe_hold_schedule": miss_event_summary(
                safe_schedule_miss_indices,
                safe_schedule_miss_total,
                max_consecutive=safe_schedule_max_consecutive,
            ),
        },
        "runtime_path": (
            "Step5dObservation->SlewCompatibleReference->"
            "StrictRnnControlPolicy->ControlCandidate->"
            "step5d_v30_contract_pipeline->SafetyEnvelope->RegisterCommand->"
            "DeferredV30Diagnostics"
        ),
        "runtime_path_source": "kunwei_rtde_bridge.step5d_v30_contract_pipeline",
        "full_tick_deferred_diagnostics": {
            "count": full_tick_deferred.count,
            "capacity": full_tick_deferred.capacity,
            "overflowed": full_tick_deferred.overflowed,
        },
        "safe_hold_deferred_diagnostics": {
            "count": safe_hold_deferred.count,
            "capacity": safe_hold_deferred.capacity,
            "overflowed": safe_hold_deferred.overflowed,
        },
        "paced_500hz": bool(args.pace_500hz),
        "pacing_provenance": {
            "clock": "time.perf_counter",
            "control_hz": effective_profile["control_hz"],
            "period_s": 1.0 / effective_profile["control_hz"],
            "full_tick_release_policy": "absolute",
            "safe_hold_release_policy": "independent_absolute",
        },
        "elapsed_full_tick_wall_s": full_tick_elapsed_wall_s,
        "elapsed_safe_hold_wall_s": safe_hold_elapsed_wall_s,
        "safety_boundary": [
            "read-only source evidence",
            "stdout JSON only",
            "no RTDE connection",
            "no Dashboard connection",
            "no controller write",
            "no bridge start",
            "no motion authorization",
        ],
    }
    if args.include_raw_samples:
        payload.update(
            {
                "solver_ms": solver_ms.tolist(),
                "tick_ms": full_tick_ms.tolist(),
                "safe_hold_ms": safe_hold_ms.tolist(),
            }
        )
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
