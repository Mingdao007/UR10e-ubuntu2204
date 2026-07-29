"""Read-only validation of the separately deployed optimizer/GPU certificate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .runtime_installation import (
    RuntimeInstallationError,
    load_runtime_pointer_identity,
    runtime_cache_root,
)


SCHEMA = "step5d.autotune-v3/gpu-functional-attestation-v1"
POINTER_SCHEMA = "step5d.autotune-v3/gpu-functional-pointer-v1"
EXPERIMENT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_PATHS = (
    "config/step5d_liveprep_solver_gate.json",
    "tools/step5c_strict_rnn.py",
    "tools/step5d_autotune_contract.py",
    "tools/step5d_autotune_optimizer.py",
    "tools/step5d_autotune_r008_policy.py",
    "tools/step5d_autotune_supervisor.py",
    "tools/step5d_physics_soft_prior.py",
    "tools/step5d_autotune_v3/batch_producer.py",
    "tools/step5d_autotune_v3/control_policy.py",
    "tools/step5d_autotune_v3/optimizer_deployment.py",
    "tools/step5d_autotune_v3/optimizer_payloads.py",
    "tools/step5d_autotune_v3/optimizer_policy.py",
    "tools/step5d_autotune_v3/optimizer_protocol.py",
    "tools/step5d_autotune_v3/optimizer_types.py",
    "tools/step5d_autotune_v3/optimizer_wire.py",
    "tools/step5d_autotune_v3/shared_contracts.py",
    "tools/step5d_autotune_v3/optimizer_worker.py",
    "tools/step5d_autotune_v3/runtime_environment.py",
    "tools/step5d_autotune_v3/runtime_functional_gates.py",
    "tools/step5d_autotune_v3/runtime_installation.py",
)


class RuntimeFunctionalGateError(RuntimeError):
    """Current native GPU evidence is absent, stale, or failed."""


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
            raise RuntimeFunctionalGateError(
                f"functional gate source is unsafe: {relative}"
            )
        result[relative] = _sha256_file(path)
    return result


def load_gpu_functional_attestation(
    *,
    runtime_pointer: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    try:
        pointer = (
            load_runtime_pointer_identity()
            if runtime_pointer is None
            else dict(runtime_pointer)
        )
        path = (
            runtime_cache_root()
            / pointer["bundle_id"]
            / "gpu-functional/current.json"
        )
        if path.is_symlink() or not path.is_file():
            raise RuntimeFunctionalGateError("GPU functional pointer is missing")
        current = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(current, dict)
            or set(current)
            != {
                "schema",
                "bundle_id",
                "attestation_path",
                "attestation_sha256",
            }
            or current["schema"] != POINTER_SCHEMA
            or current["bundle_id"] != pointer["bundle_id"]
        ):
            raise RuntimeFunctionalGateError(
                "GPU functional pointer binding differs"
            )
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
                "control_environment_id": pointer["profiles"]["control"][
                    "environment_id"
                ],
                "optimizer_environment_id": pointer["profiles"]["optimizer"][
                    "environment_id"
                ],
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
    except (
        RuntimeInstallationError,
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        KeyError,
    ) as exc:
        raise RuntimeFunctionalGateError(
            f"GPU functional evidence is invalid: {exc}"
        ) from exc


__all__ = [
    "RuntimeFunctionalGateError",
    "load_gpu_functional_attestation",
]
