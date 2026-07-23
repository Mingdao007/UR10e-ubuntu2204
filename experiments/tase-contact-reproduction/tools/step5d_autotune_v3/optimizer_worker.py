"""Internal exact-runtime worker for CUDA BoTorch candidate suggestions."""

from __future__ import annotations

import hashlib
import json
import sys
from typing import Any, Mapping

from step5d_autotune_contract import ForceCandidate
from step5d_autotune_r008_policy import (
    supercycle_batch_a,
    supercycle_batch_b_after_gp_update,
)

from .optimizer_payloads import (
    accepted_history_digest,
    decode_observation,
    encode_suggestions,
    optimizer_identity,
    validate_optimizer_identity,
)
from .optimizer_wire import (
    MODE_SEEDS,
    REQUEST_SCHEMA,
    RESPONSE_SCHEMA,
    canonical_bytes,
    strict_json,
)
from .runtime_installation import require_runtime_profile


def _request(payload: Any) -> tuple[dict[str, Any], bytes]:
    fields = {
        "schema",
        "mode",
        "seed",
        "sequence",
        "optimizer_identity",
        "accepted_history_digest",
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
    validate_optimizer_identity(payload["optimizer_identity"])
    encoded = canonical_bytes(dict(payload))
    return dict(payload), encoded


def run(encoded: bytes) -> dict[str, Any]:
    pointer = require_runtime_profile("optimizer", full_integrity=True)
    request, canonical = _request(strict_json(encoded, "optimizer request"))
    expected_identity = optimizer_identity(
        optimizer_digest=pointer["profiles"]["optimizer"]["record_tree_sha256"],
        build_digest=pointer["profiles"]["optimizer"]["environment_id"],
    )
    if request["optimizer_identity"] != expected_identity:
        raise ValueError("optimizer request deployment identity differs")
    observations = tuple(
        decode_observation(row) for row in request["observations"]
    )
    if request["accepted_history_digest"] != accepted_history_digest(observations):
        raise ValueError("optimizer request accepted history digest differs")
    catalog = tuple(
        ForceCandidate.from_payload(row) for row in request["catalog"]
    )
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
    return {
        "schema": RESPONSE_SCHEMA,
        "ok": True,
        "request_sha256": hashlib.sha256(canonical).hexdigest(),
        "suggestions": encode_suggestions(
            occurrences,
            catalog=catalog,
            identity=expected_identity,
            seed=request["seed"],
            history_digest=request["accepted_history_digest"],
            evidence=evidence,
        ),
        "evidence": dict(evidence),
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
