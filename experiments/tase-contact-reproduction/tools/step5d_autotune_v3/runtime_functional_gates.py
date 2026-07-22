"""Native control/optimizer CUDA gates bound to the promoted runtime."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence

from .runtime_environment import production_runtime_environment
from .runtime_installation import (
    RuntimeInstallationError,
    load_runtime_pointer,
    require_runtime_profile,
    runtime_cache_root,
)


SCHEMA = "step5d.autotune-v3/gpu-functional-attestation-v1"
POINTER_SCHEMA = "step5d.autotune-v3/gpu-functional-pointer-v1"
WORKER_SCHEMA = "step5d.autotune-v3/optimizer-functional-worker-v1"
EXPERIMENT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATHS = (
    "config/step5d_liveprep_solver_gate.json",
    "tools/step5c_strict_rnn.py",
    "tools/step5d_autotune_contract.py",
    "tools/step5d_autotune_optimizer.py",
    "tools/step5d_autotune_r008_policy.py",
    "tools/step5d_autotune_supervisor.py",
    "tools/step5d_autotune_v3/batch_producer.py",
    "tools/step5d_autotune_v3/optimizer_protocol.py",
    "tools/step5d_autotune_v3/optimizer_worker.py",
    "tools/step5d_autotune_v3/runtime_environment.py",
    "tools/step5d_autotune_v3/runtime_functional_gates.py",
    "tools/step5d_autotune_v3/runtime_installation.py",
)


class RuntimeFunctionalGateError(RuntimeError):
    """Current native GPU evidence is absent, stale, or failed."""


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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_binding() -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in SOURCE_PATHS:
        path = EXPERIMENT_ROOT / relative
        if path.is_symlink() or not path.is_file():
            raise RuntimeFunctionalGateError(f"functional gate source is unsafe: {relative}")
        result[relative] = _sha256_file(path)
    return result


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
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _fixed_observations() -> tuple[Any, ...]:
    from step5d_autotune_contract import Evaluation, ForceCandidate, TrialDisposition
    from step5d_autotune_optimizer import Observation
    from step5d_autotune_r008_policy import BASELINE

    candidates = (
        BASELINE,
        BASELINE,
        BASELINE,
        ForceCandidate.from_log2(p=-0.25, damping=0.0, i=0.0),
        ForceCandidate.from_log2(p=0.25, damping=0.0, i=0.0),
        ForceCandidate.from_log2(p=0.0, damping=0.25, i=0.0),
    )
    objectives = (0.42, 0.39, 0.41, 0.36, 0.34, 0.31)
    observations = []
    for index, (candidate, objective) in enumerate(
        zip(candidates, objectives, strict=True), start=1
    ):
        trial_uid = hashlib.sha256(f"step5d-native-gpu-gate:{index}".encode()).hexdigest()
        evaluation = Evaluation(
            trial_uid=trial_uid,
            backend_id="step5d_v35_native_backend_v1",
            eligible=True,
            disposition=TrialDisposition.OBJECTIVE,
            objective_mae_n=objective,
            force_bias_n=0.0,
            force_std_n=0.05,
            coverage_12_plus_minus_1_ratio=0.95,
            complete_bins=550,
            safe_closure=True,
        )
        observations.append(
            Observation(
                candidate=candidate,
                evaluation=evaluation,
                profile_id="step5d_native_gpu_gate_v1",
                plant_epoch=1,
                latest_trace_sha256=hashlib.sha256(
                    f"step5d-native-gpu-trace:{index}".encode()
                ).hexdigest(),
            )
        )
    return tuple(observations)


def run_optimizer_worker_gate() -> dict[str, Any]:
    pointer = require_runtime_profile("optimizer", full_integrity=False)
    forbidden = {"torch", "botorch", "gpytorch"}
    before_5 = sorted(forbidden.intersection(sys.modules))
    if before_5:
        raise RuntimeFunctionalGateError("optimizer CUDA modules loaded before prefix-5 gate")
    from step5d_autotune_optimizer import choose_candidate

    observations = _fixed_observations()
    after_5_candidate, after_5 = choose_candidate(
        list(observations[:5]),
        profile_id="step5d_native_gpu_gate_v1",
        plant_epoch=1,
        require_cuda_botorch=True,
        optimizer_seed=9009,
    )
    after_5_modules = sorted(forbidden.intersection(sys.modules))
    if after_5_modules or after_5.get("selection") != "bounded_initial_axial_exploration":
        raise RuntimeFunctionalGateError("prefix-5 unexpectedly entered the CUDA optimizer")
    after_6_candidate, after_6 = choose_candidate(
        list(observations),
        profile_id="step5d_native_gpu_gate_v1",
        plant_epoch=1,
        require_cuda_botorch=True,
        optimizer_seed=9009,
    )
    if (
        after_6.get("selection") != "botorch_qLogNoisyExpectedImprovement_q1_cuda"
        or after_6.get("device") != "cuda:0"
        or after_6.get("seed") != 9009
    ):
        raise RuntimeFunctionalGateError("sixth observation did not enter seeded CUDA qLogNEI")
    from step5d_autotune_r008_policy import supercycle_batch_a
    from .batch_producer import production_candidate_catalog

    rolling, rolling_evidence = supercycle_batch_a(
        observations,
        production_candidate_catalog(),
        sequence=3,
        seed=9009,
    )
    rolling_uids = [str(row.candidate.candidate_uid) for row in rolling]
    if (
        len(rolling_uids) != 5
        or len(set(rolling_uids)) != 5
        or rolling_evidence.get("optimizer", {}).get("device") != "cuda:0"
    ):
        raise RuntimeFunctionalGateError("rolling revision-3 q4 gate differs")
    forbidden_loaded = sorted(
        name
        for name in ("cupy", "matplotlib", "mujoco", "pandas", "pinocchio", "xacro")
        if name in sys.modules
    )
    if forbidden_loaded:
        raise RuntimeFunctionalGateError(
            "optimizer process loaded forbidden modules: " + ",".join(forbidden_loaded)
        )
    runtime = pointer["profiles"]["optimizer"]
    semantic = {
        "after_5_candidate_uid": after_5_candidate.candidate_uid,
        "after_6_candidate_uid": after_6_candidate.candidate_uid,
        "rolling_candidate_uids": rolling_uids,
    }
    return {
        "schema": WORKER_SCHEMA,
        "ok": True,
        "runtime": {
            "bundle_id": pointer["bundle_id"],
            "environment_id": runtime["environment_id"],
            "attestation_sha256": pointer["attestation_sha256"],
            "python_executable": os.path.abspath(sys.executable),
            "python_prefix": os.path.abspath(sys.prefix),
        },
        "observation_material_sha256": hashlib.sha256(
            _canonical_bytes(
                [
                    {
                        "candidate_uid": row.candidate.candidate_uid,
                        "trial_uid": row.evaluation.trial_uid,
                        "objective": row.objective,
                    }
                    for row in observations
                ]
            )
        ).hexdigest(),
        "after_5": {**after_5, "torch_modules_loaded": after_5_modules},
        "after_6": after_6,
        "rolling_revision3": rolling_evidence,
        "semantic": semantic,
        "semantic_sha256": hashlib.sha256(_canonical_bytes(semantic)).hexdigest(),
        "forbidden_modules_loaded": forbidden_loaded,
    }


def _run_control_gate() -> tuple[dict[str, Any], Any]:
    pointer = require_runtime_profile("control")
    if any(name in sys.modules for name in ("torch", "botorch", "gpytorch")):
        raise RuntimeFunctionalGateError("control process loaded optimizer modules before gate")
    import cupy
    import numpy as np
    from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver

    solver = StrictTaseRnnSolver(
        StrictRnnConfig(
            paper_truth_path=(
                EXPERIMENT_ROOT / "config/step5d_liveprep_solver_gate.json"
            ),
            qdot_limit_rad_s=0.5,
            epsilon=0.010,
            sigr_exponent_r=0.8,
            inner_iterations=512,
            backend="cupy",
        )
    )
    result = solver.solve(
        actual_q=np.zeros(6),
        actual_qd=np.zeros(6),
        target_state={
            "J": np.eye(6),
            "xdot_c": np.zeros(6),
            "omega_minus": np.full(6, -0.05),
            "omega_plus": np.full(6, 0.05),
            "dt": 0.002,
            "epsilon": 0.010,
            "r": 0.8,
            "cmd_valid": True,
        },
    )
    equivalence = solver.cupy_parallel_equivalence
    if (
        result.solver_status != 40.0
        or not all(value == 0.0 for value in result.qdot)
        or not isinstance(equivalence, Mapping)
        or equivalence.get("bitwise_equal") is not True
        or equivalence.get("samples") != 100
    ):
        raise RuntimeFunctionalGateError("production Strict RNN CUDA gate differs")
    if any(name in sys.modules for name in ("torch", "botorch", "gpytorch")):
        raise RuntimeFunctionalGateError("control process loaded optimizer modules")
    return (
        {
            "ok": True,
            "runtime": {
                "bundle_id": pointer["bundle_id"],
                "environment_id": pointer["profiles"]["control"]["environment_id"],
                "attestation_sha256": pointer["attestation_sha256"],
                "python_executable": os.path.abspath(sys.executable),
                "python_prefix": os.path.abspath(sys.prefix),
            },
            "cupy_version": cupy.__version__,
            "device_count": int(cupy.cuda.runtime.getDeviceCount()),
            "device_name": (
                lambda value: value.decode("utf-8") if isinstance(value, bytes) else str(value)
            )(cupy.cuda.runtime.getDeviceProperties(0).get("name", "unknown")),
            "strict_rnn": {
                "inner_iterations": 512,
                "backend": result.diagnostics.get("backend"),
                "qdot": list(result.qdot),
                "parallel_equivalence": dict(equivalence),
                "host_staging_pinned": solver.cupy_host_staging_pinned,
                "dedicated_stream": solver.cupy_dedicated_stream,
            },
            "optimizer_modules_loaded": [],
        },
        solver,
    )


def _optimizer_subprocess(pointer: Mapping[str, Any]) -> dict[str, Any]:
    python = pointer["profiles"]["optimizer"]["python_executable"]
    environment = production_runtime_environment(
        os.environ,
        profile="optimizer",
        runtime_pointer=pointer,
        additions={"STEP5D_V3_FUNCTIONAL_GATE_WORKER": "1"},
    )
    completed = subprocess.run(
        [python, "-B", "-m", "step5d_autotune_v3.runtime_functional_gates", "--optimizer-worker"],
        cwd=EXPERIMENT_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=600.0,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeFunctionalGateError(
            "optimizer functional worker failed: "
            + completed.stderr.decode("utf-8", errors="replace").strip()
        )
    try:
        payload = json.loads(completed.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeFunctionalGateError(f"optimizer functional worker output is invalid: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != WORKER_SCHEMA or payload.get("ok") is not True:
        raise RuntimeFunctionalGateError("optimizer functional worker evidence differs")
    return payload


def run_native_functional_gates() -> tuple[dict[str, Any], dict[str, str]]:
    pointer = require_runtime_profile("control")
    control, resident_solver = _run_control_gate()
    first = _optimizer_subprocess(pointer)
    second = _optimizer_subprocess(pointer)
    if (
        first["semantic_sha256"] != second["semantic_sha256"]
        or first["semantic"] != second["semantic"]
    ):
        raise RuntimeFunctionalGateError("fresh-process optimizer replay differs")
    # Keep the production CuPy allocations resident until both optimizer runs finish.
    if resident_solver.cupy_parallel_equivalence.get("bitwise_equal") is not True:
        raise RuntimeFunctionalGateError("resident control solver lost equivalence")
    payload = {
        "schema": SCHEMA,
        "observed_at_unix_ns": time.time_ns(),
        "runtime": {
            "bundle_id": pointer["bundle_id"],
            "attestation_sha256": pointer["attestation_sha256"],
            "control_environment_id": pointer["profiles"]["control"]["environment_id"],
            "optimizer_environment_id": pointer["profiles"]["optimizer"]["environment_id"],
        },
        "source_fingerprints": _source_binding(),
        "control": control,
        "optimizer": {
            "first": first,
            "replay": second,
            "semantic_sha256": first["semantic_sha256"],
        },
        "coexistence": {
            "control_allocations_resident_during_optimizer": True,
            "optimizer_replay_equal": True,
        },
        "overall_pass": True,
    }
    evidence_bytes = (
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(evidence_bytes).hexdigest()
    root = runtime_cache_root() / pointer["bundle_id"] / "gpu-functional"
    evidence_path = root / "evidence" / digest / "attestation.json"
    _atomic_json(evidence_path, payload)
    reference = {"path": str(evidence_path), "sha256": _sha256_file(evidence_path)}
    _atomic_json(
        root / "current.json",
        {
            "schema": POINTER_SCHEMA,
            "bundle_id": pointer["bundle_id"],
            "attestation_path": reference["path"],
            "attestation_sha256": reference["sha256"],
        },
    )
    return payload, reference


def load_gpu_functional_attestation(
    *,
    runtime_pointer: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    try:
        pointer = (
            load_runtime_pointer()
            if runtime_pointer is None
            else dict(runtime_pointer)
        )
        path = runtime_cache_root() / pointer["bundle_id"] / "gpu-functional/current.json"
        if path.is_symlink() or not path.is_file():
            raise RuntimeFunctionalGateError("GPU functional pointer is missing")
        current = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(current, dict)
            or set(current) != {"schema", "bundle_id", "attestation_path", "attestation_sha256"}
            or current["schema"] != POINTER_SCHEMA
            or current["bundle_id"] != pointer["bundle_id"]
        ):
            raise RuntimeFunctionalGateError("GPU functional pointer binding differs")
        evidence_path = Path(current["attestation_path"])
        expected_parent = path.parent / "evidence" / current["attestation_sha256"]
        if (
            not evidence_path.is_absolute()
            or evidence_path.is_symlink()
            or not evidence_path.is_file()
            or evidence_path.parent != expected_parent
            or _sha256_file(evidence_path) != current["attestation_sha256"]
        ):
            raise RuntimeFunctionalGateError("GPU functional evidence bytes differ")
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != SCHEMA
            or payload.get("overall_pass") is not True
            or payload.get("runtime")
            != {
                "bundle_id": pointer["bundle_id"],
                "attestation_sha256": pointer["attestation_sha256"],
                "control_environment_id": pointer["profiles"]["control"]["environment_id"],
                "optimizer_environment_id": pointer["profiles"]["optimizer"]["environment_id"],
            }
            or payload.get("source_fingerprints") != _source_binding()
        ):
            raise RuntimeFunctionalGateError("GPU functional evidence is stale")
        return payload, {
            "path": str(evidence_path),
            "sha256": current["attestation_sha256"],
        }
    except RuntimeFunctionalGateError:
        raise
    except (RuntimeInstallationError, OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeFunctionalGateError(f"GPU functional evidence is invalid: {exc}") from exc


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments == ["--optimizer-worker"]:
            if os.environ.get("STEP5D_V3_FUNCTIONAL_GATE_WORKER") != "1":
                raise RuntimeFunctionalGateError("optimizer functional worker lacks parent binding")
            result = run_optimizer_worker_gate()
        elif not arguments:
            result, reference = run_native_functional_gates()
            result = {**result, "evidence": reference}
        else:
            raise RuntimeFunctionalGateError("internal functional gate argv differs")
    except Exception as exc:
        print(f"{type(exc).__name__}:{exc}", file=sys.stderr)
        return 78
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "RuntimeFunctionalGateError",
    "load_gpu_functional_attestation",
    "run_native_functional_gates",
    "run_optimizer_worker_gate",
]
