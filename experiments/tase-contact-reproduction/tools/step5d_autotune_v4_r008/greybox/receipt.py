"""Content-addressed grey-box identification receipt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping
import hashlib
import json
import math

from .reconstruct import GreyboxError

if TYPE_CHECKING:
    from .identify import IdentificationResult


SCHEMA = "step5d.greybox/r008-contact-plant-identification-v1"
PARENT_PATHS = (
    "config/step5d/autotune_v4_r006.json",
    "tools/step5d_paper_outer_loop.py",
    "tools/step5d_autotune_v4_r004/path_controller.py",
    "tools/step5d_autotune_v4_r004/calibrated_runtime.py",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def parent_hashes(repo_root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    root = Path(repo_root)
    for relative in PARENT_PATHS:
        path = root / relative
        if not path.is_file():
            raise GreyboxError(f"parent file missing: {relative}")
        out[relative] = sha256_file(path)
    return out


@dataclass(frozen=True)
class GreyboxReceipt:
    document: Mapping[str, Any]
    digest: str
    path: Path | None = None

    def write(self, directory: Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.digest}.json"
        path.write_bytes(canonical_bytes(self.document) + b"\n")
        # Re-hash the exact on-disk bytes excluding the trailing newline that
        # editors add is unnecessary here: digest is of the canonical body.
        return path


def build_receipt(
    result: "IdentificationResult",
    *,
    run_root: Path,
    repo_root: Path,
) -> GreyboxReceipt:
    run_root = Path(run_root).resolve()
    repo_root = Path(repo_root).resolve()
    plant = result.plant
    bundle_shas = {
        str(item.trace.attempt_sequence): item.trace.bundle_sha256 for item in result.reconstructed
    }
    source_bundle_sha = sha256_bytes(
        canonical_bytes({"bundles": bundle_shas, "run_root": str(run_root.name)})
    )
    gate_documents = [
        {
            "name": gate.name,
            "passed": gate.passed,
            "value": gate.value,
            "limit": gate.limit,
            "detail": dict(gate.detail),
        }
        for gate in result.gates
    ]
    if any(not gate["passed"] for gate in gate_documents):
        failed = [gate["name"] for gate in gate_documents if not gate["passed"]]
        raise GreyboxError(f"identification gates failed: {', '.join(failed)}")

    body: dict[str, Any] = {
        "schema": SCHEMA,
        "run_root": str(run_root),
        "source_bundle_sha256": source_bundle_sha,
        "bundle_sha256_by_attempt": bundle_shas,
        "parent_hashes": parent_hashes(repo_root),
        "identified": {
            "stiffness_n_per_m": float(plant.stiffness_n_per_m),
            "rho": float(plant.rho),
            "kinematic_c_v_m_s": float(result.kinematic_c_v_m_s),
            "seed_stiffness_n_per_m": float(result.seed_stiffness_n_per_m),
            "surface_path_time_s": [float(value) for value in plant.surface_path_time_s],
            "surface_height_m": [float(value) for value in plant.surface_height_m],
            "deltas_m": {
                str(key): float(value) for key, value in sorted(plant.deltas_m.items())
            },
        },
        "gates": gate_documents,
        "fit": {
            "equation_error_cost": float(result.equation_error_cost),
            "closed_loop_cost": float(result.closed_loop_cost),
            "closed_loop_mae_n": {
                str(item.trace.attempt_sequence): float(sim.mae_n)
                for item, sim in zip(result.reconstructed, result.closed_loop, strict=True)
            },
            "sealed_mae_n": {
                str(item.trace.attempt_sequence): float(item.trace.sealed_mae_n)
                for item in result.reconstructed
            },
        },
    }
    # Provisional digest over the body without the seal, then seal.
    provisional = sha256_bytes(canonical_bytes(body))
    body["builder_seal"] = {
        "algorithm": "sha256",
        "digest": provisional,
        "trial_count": len(result.reconstructed),
    }
    digest = sha256_bytes(canonical_bytes(body))
    body["artifact_sha256"] = digest
    # Final digest includes artifact_sha256 field set to the digest of the
    # sealed-without-artifact body — recompute once for self-consistency.
    digest = sha256_bytes(canonical_bytes({key: value for key, value in body.items() if key != "artifact_sha256"}))
    body["artifact_sha256"] = digest
    return GreyboxReceipt(document=body, digest=digest)


def load_receipt(path: Path) -> GreyboxReceipt:
    path = Path(path)
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GreyboxError(f"receipt is not JSON: {path}") from exc
    if document.get("schema") != SCHEMA:
        raise GreyboxError(f"unexpected receipt schema: {document.get('schema')}")
    body = {key: value for key, value in document.items() if key != "artifact_sha256"}
    digest = sha256_bytes(canonical_bytes(body))
    declared = document.get("artifact_sha256")
    if declared != digest:
        raise GreyboxError("receipt artifact_sha256 mismatch")
    if not math.isclose(float(document["identified"]["rho"]), float(document["identified"]["rho"])):
        raise GreyboxError("receipt rho is not finite")
    return GreyboxReceipt(document=document, digest=digest, path=path)


__all__ = [
    "PARENT_PATHS",
    "SCHEMA",
    "GreyboxReceipt",
    "build_receipt",
    "canonical_bytes",
    "load_receipt",
    "parent_hashes",
    "sha256_bytes",
    "sha256_file",
]
