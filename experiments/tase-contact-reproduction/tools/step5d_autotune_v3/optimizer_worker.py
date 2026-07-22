"""Internal exact-runtime worker for CUDA BoTorch production proposals."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

from step5d_autotune_contract import ForceCandidate
from step5d_autotune_r008_policy import (
    supercycle_batch_a,
    supercycle_batch_b_after_gp_update,
)

from .optimizer_protocol import (
    MODE_SEEDS,
    REQUEST_SCHEMA,
    RESPONSE_SCHEMA,
    canonical_bytes,
    decode_observation,
    occurrence_payload,
    strict_json,
)
from .runtime_installation import require_runtime_profile


_FORBIDDEN_MODULES = ("cupy", "matplotlib", "mujoco", "pandas", "pinocchio", "xacro")


def _module_closure() -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    violations: list[str] = []
    for name, module in sorted(sys.modules.items()):
        if name.split(".", 1)[0] in _FORBIDDEN_MODULES:
            violations.append(f"forbidden_module:{name}")
        raw_path = getattr(module, "__file__", None)
        if not isinstance(raw_path, str):
            continue
        try:
            path = str(Path(raw_path).resolve(strict=True))
        except OSError:
            violations.append(f"unresolved_module_path:{name}")
            continue
        if "/.codex-python/" in path or "/site-packages/pandas/" in path:
            violations.append(f"forbidden_module_path:{name}:{path}")
        rows.append({"module": name, "path": path})
    digest = hashlib.sha256(canonical_bytes(rows)).hexdigest()
    return {
        "sha256": digest,
        "module_file_count": len(rows),
        "violations": violations,
    }


def _request(payload: Any) -> tuple[dict[str, Any], bytes]:
    fields = {
        "schema",
        "mode",
        "seed",
        "sequence",
        "runtime",
        "observations",
        "catalog",
        "batch_a_closure",
    }
    if not isinstance(payload, Mapping) or set(payload) != fields:
        raise ValueError("optimizer request fields differ")
    mode = payload["mode"]
    if (
        payload.get("schema") != REQUEST_SCHEMA
        or mode not in MODE_SEEDS
        or payload.get("seed") != MODE_SEEDS[mode]
        or isinstance(payload.get("sequence"), bool)
        or not isinstance(payload.get("sequence"), int)
        or payload["sequence"] < 3
        or not isinstance(payload.get("observations"), list)
        or not isinstance(payload.get("catalog"), list)
    ):
        raise ValueError("optimizer request contract differs")
    encoded = canonical_bytes(dict(payload))
    return dict(payload), encoded


def run(encoded: bytes) -> dict[str, Any]:
    # The spawning control process already completed full package/host
    # verification.  Rebind this short-lived worker to the same immutable
    # pointer without spending the optimizer response budget rehashing both
    # runtime trees.
    pointer = require_runtime_profile("optimizer", full_integrity=False)
    request, canonical = _request(strict_json(encoded, "optimizer request"))
    expected_runtime = {
        "bundle_id": pointer["bundle_id"],
        "environment_id": pointer["profiles"]["optimizer"]["environment_id"],
        "attestation_sha256": pointer["attestation_sha256"],
    }
    if request["runtime"] != expected_runtime:
        raise ValueError("optimizer request runtime binding differs")
    observations = tuple(decode_observation(row) for row in request["observations"])
    catalog = tuple(ForceCandidate.from_payload(row) for row in request["catalog"])
    if len({candidate.candidate_uid for candidate in catalog}) != len(catalog):
        raise ValueError("optimizer request catalog repeats a candidate")
    if request["mode"] == "rolling_batch_a":
        if request["batch_a_closure"] is not None:
            raise ValueError("Batch A request cannot carry Batch A closure")
        occurrences, evidence = supercycle_batch_a(
            observations,
            catalog,
            sequence=request["sequence"],
            seed=request["seed"],
        )
    else:
        if not isinstance(request["batch_a_closure"], Mapping):
            raise ValueError("Batch B request requires Batch A closure")
        occurrences, evidence = supercycle_batch_b_after_gp_update(
            observations,
            catalog,
            sequence=request["sequence"],
            batch_a_closure=request["batch_a_closure"],
            seed=request["seed"],
        )
    import torch

    closure = _module_closure()
    if closure["violations"]:
        raise ValueError("optimizer module closure contains forbidden inputs")
    return {
        "schema": RESPONSE_SCHEMA,
        "ok": True,
        "request_sha256": hashlib.sha256(canonical).hexdigest(),
        "runtime": expected_runtime,
        "gpu": {
            "visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
            "mapped_device": "cuda:0",
            "name": torch.cuda.get_device_name(0),
            "torch_version": importlib.metadata.version("torch"),
            "botorch_version": importlib.metadata.version("botorch"),
            "gpytorch_version": importlib.metadata.version("gpytorch"),
            "torch_cuda_version": torch.version.cuda,
        },
        "occurrences": [occurrence_payload(value) for value in occurrences],
        "evidence": dict(evidence),
        "module_closure": closure,
    }


def main() -> int:
    try:
        encoded = sys.stdin.buffer.read(8 * 1024 * 1024 + 1)
        if len(encoded) > 8 * 1024 * 1024:
            raise ValueError("optimizer request exceeds size limit")
        result = run(encoded)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason_code": "OPTIMIZER_WORKER_FAILED",
                    "detail": f"{type(exc).__name__}:{exc}",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 78
    sys.stdout.buffer.write(canonical_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
