"""CUDA qLogNEI worker for r005, entered only through the shared resolver."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Mapping

from step5d_optimizer_runtime import (
    ATTESTATION_SCHEMA,
    REQUEST_SCHEMA,
    RESPONSE_SCHEMA,
)


class OptimizerWorkerError(RuntimeError):
    """The isolated optimizer child could not prove its exact environment."""


def _expected_environment(expected: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "bundle_id",
        "runtime_attestation_sha256",
        "profile",
        "environment_id",
        "environment_hash",
        "required_versions",
        "cuda_available",
        "gpu_name",
        "gpu_uuid",
    }
    if set(expected) != required or expected.get("schema") != ATTESTATION_SCHEMA:
        raise OptimizerWorkerError("optimizer expected-attestation fields differ")
    if expected.get("profile") != "optimizer":
        raise OptimizerWorkerError("optimizer child profile differs")
    if expected.get("cuda_available") is not True:
        raise OptimizerWorkerError("optimizer child requires CUDA")
    versions = expected.get("required_versions")
    if not isinstance(versions, Mapping) or set(versions) != {"torch", "botorch", "gpytorch"}:
        raise OptimizerWorkerError("optimizer required Torch/BoTorch/GPyTorch versions differ")
    return dict(expected)


def _self_attest(expected: Mapping[str, Any]) -> dict[str, Any]:
    expected_payload = _expected_environment(expected)
    # This is the only module in the r005 tree that imports Torch/BoTorch/
    # GPyTorch.  It is executed by the exact V3 optimizer interpreter.
    import torch

    versions = {
        name: importlib.metadata.version(name)
        for name in ("torch", "botorch", "gpytorch")
    }
    cuda_available = bool(torch.cuda.is_available())
    if not cuda_available:
        raise OptimizerWorkerError("CUDA is unavailable; CPU optimizer fallback is forbidden")
    torch.cuda.set_device(0)
    properties = torch.cuda.get_device_properties(0)
    gpu_name = str(torch.cuda.get_device_name(0))
    raw_uuid = getattr(properties, "uuid", None)
    if isinstance(raw_uuid, bytes):
        raw_uuid = raw_uuid.decode("utf-8", errors="strict")
    gpu_uuid = str(raw_uuid) if raw_uuid is not None else ""
    if gpu_uuid and not gpu_uuid.startswith("GPU-"):
        gpu_uuid = "GPU-" + gpu_uuid
    observed = {
        "schema": ATTESTATION_SCHEMA,
        "bundle_id": os.environ.get("STEP5D_V3_RUNTIME_BUNDLE_ID"),
        "runtime_attestation_sha256": os.environ.get(
            "STEP5D_V3_RUNTIME_ATTESTATION_SHA256"
        ),
        "profile": os.environ.get("STEP5D_V3_RUNTIME_PROFILE"),
        "environment_id": os.environ.get("STEP5D_SHARED_OPTIMIZER_ENVIRONMENT_ID"),
        "environment_hash": os.environ.get("STEP5D_SHARED_OPTIMIZER_ENVIRONMENT_HASH"),
        "required_versions": versions,
        "cuda_available": cuda_available,
        "gpu_name": gpu_name,
        "gpu_uuid": gpu_uuid,
    }
    if observed != expected_payload:
        raise OptimizerWorkerError("optimizer child self-attestation differs from V3 expectation")
    return observed


def _candidate(value: Any):
    from step5d_autotune_v4_r005.contracts import Candidate

    if not isinstance(value, Mapping):
        raise OptimizerWorkerError("optimizer candidate payload is not an object")
    payload = dict(value)
    payload.pop("i_off", None)
    try:
        return Candidate(**payload)
    except (TypeError, ValueError, RuntimeError) as exc:
        raise OptimizerWorkerError("optimizer candidate payload is outside r005") from exc


def _observation(value: Any):
    """Reject the removed caller-observation wire path unconditionally."""

    raise OptimizerWorkerError(
        "caller observation/objective payloads are forbidden; bind the durable ledger instead"
    )


def _digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise OptimizerWorkerError(f"{role} must be a lowercase SHA-256")
    return value


def _regular_ledger_path(value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise OptimizerWorkerError("optimizer ledger path is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise OptimizerWorkerError("optimizer ledger path must be absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise OptimizerWorkerError("optimizer ledger path contains a symlink")
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise OptimizerWorkerError("optimizer ledger path is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(mode) or not path.is_file():
        raise OptimizerWorkerError("optimizer ledger path is not a regular file")
    if str(path.resolve(strict=True)) != value:
        raise OptimizerWorkerError("optimizer ledger path is not canonical")
    return path


def _ledger_binding(value: Any) -> dict[str, Any]:
    required = {
        "ledger_path",
        "ledger_byte_sha256",
        "campaign_fingerprint",
        "eoat_sha256",
        "refs",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise OptimizerWorkerError("optimizer ledger binding fields differ")
    path = _regular_ledger_path(value["ledger_path"])
    _digest(value["ledger_byte_sha256"], "ledger byte digest")
    _digest(value["campaign_fingerprint"], "ledger campaign fingerprint")
    _digest(value["eoat_sha256"], "ledger EOAT identity")
    refs = value["refs"]
    if not isinstance(refs, list):
        raise OptimizerWorkerError("optimizer ledger refs are not ordered")
    normalized_refs: list[dict[str, Any]] = []
    for ref in refs:
        if not isinstance(ref, Mapping) or set(ref) != {"attempt_sequence", "observation_uid"}:
            raise OptimizerWorkerError("optimizer ledger observation ref fields differ")
        sequence = ref["attempt_sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise OptimizerWorkerError("optimizer ledger observation ref sequence is invalid")
        normalized_refs.append(
            {
                "attempt_sequence": sequence,
                "observation_uid": _digest(ref["observation_uid"], "observation UID"),
            }
        )
    if len({ref["attempt_sequence"] for ref in normalized_refs}) != len(normalized_refs):
        raise OptimizerWorkerError("optimizer ledger observation refs repeat an attempt")
    return {
        "ledger_path": str(path),
        "ledger_byte_sha256": value["ledger_byte_sha256"],
        "campaign_fingerprint": value["campaign_fingerprint"],
        "eoat_sha256": value["eoat_sha256"],
        "refs": normalized_refs,
    }


def _load_fresh_ledger(binding: Mapping[str, Any]):
    """Cold-read the ledger and return only its fresh verified records."""

    from step5d_autotune_v4_r005.observations import ObservationLedger

    normalized = _ledger_binding(binding)
    path = Path(normalized["ledger_path"])
    try:
        before_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if before_digest != normalized["ledger_byte_sha256"]:
            raise OptimizerWorkerError("ledger digest differs before child verification")
        ledger = ObservationLedger(
            path,
            campaign_fingerprint=normalized["campaign_fingerprint"],
            eoat_sha256=normalized["eoat_sha256"],
        )
        audit = ledger.fresh_process_verify_details()
        identity = ledger.read_only_identity()
    except OptimizerWorkerError:
        raise
    except Exception as exc:
        raise OptimizerWorkerError("child fresh ledger verification failed") from exc
    if (
        identity["ledger_path"] != normalized["ledger_path"]
        or identity["campaign_fingerprint"] != normalized["campaign_fingerprint"]
        or identity["eoat_sha256"] != normalized["eoat_sha256"]
        or identity["ledger_byte_sha256"] != normalized["ledger_byte_sha256"]
        or audit.get("ledger_sha256") != normalized["ledger_byte_sha256"]
    ):
        raise OptimizerWorkerError("child fresh ledger identity differs from request")
    after_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if after_digest != normalized["ledger_byte_sha256"]:
        raise OptimizerWorkerError("ledger digest drifted after child verification")
    records = tuple(ledger.records)
    actual_refs = tuple(
        {
            "attempt_sequence": record.attempt_sequence,
            "observation_uid": record.observation_uid,
        }
        for record in records
    )
    if actual_refs != tuple(normalized["refs"]):
        raise OptimizerWorkerError("child ledger records differ from ordered request refs")
    return records


def _request(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema",
        "profile",
        "expected_attestation",
        "payload",
    }:
        raise OptimizerWorkerError("optimizer request fields differ")
    if value.get("schema") != REQUEST_SCHEMA or value.get("profile") != "optimizer":
        raise OptimizerWorkerError("optimizer request identity differs")
    expected = value.get("expected_attestation")
    payload = value.get("payload")
    if not isinstance(expected, Mapping) or not isinstance(payload, Mapping):
        raise OptimizerWorkerError("optimizer request payload is incomplete")
    expected_payload = _expected_environment(expected)
    required_payload = {"ledger_binding", "choices", "pending", "incumbent", "seed"}
    if set(payload) != required_payload:
        raise OptimizerWorkerError("r005 qLogNEI request fields differ")
    ledger_binding = _ledger_binding(payload["ledger_binding"])
    if not isinstance(payload["choices"], list) or not isinstance(payload["pending"], list):
        raise OptimizerWorkerError("r005 qLogNEI candidate catalog differs")
    if isinstance(payload["seed"], bool) or not isinstance(payload["seed"], int) or payload["seed"] < 0:
        raise OptimizerWorkerError("r005 qLogNEI seed differs")
    normalized_payload = dict(payload)
    normalized_payload["ledger_binding"] = ledger_binding
    return expected_payload, normalized_payload


def run(request: Mapping[str, Any]) -> dict[str, Any]:
    expected, payload = _request(request)
    observations = _load_fresh_ledger(payload["ledger_binding"])
    if sum(1 for record in observations if record.eligible) < 6:
        raise OptimizerWorkerError(
            "fresh ledger has fewer than six eligible observations for BO training"
        )
    attestation = _self_attest(expected)
    from step5d_autotune_v4_r005.optimizer import V4BoAdapter
    from step5d_autotune_optimizer import candidate_vector, cuda_botorch_joint_candidates
    from step5d_x_pending import install_qlognei_x_pending

    choices = tuple(_candidate(row) for row in payload["choices"])
    pending = tuple(_candidate(row) for row in payload["pending"])
    incumbent = _candidate(payload["incumbent"])
    v3_observations = tuple(V4BoAdapter._to_v3_observation(row) for row in observations)
    v3_choices = tuple(V4BoAdapter._to_v3(row) for row in choices)
    v3_pending = tuple(V4BoAdapter._to_v3(row) for row in pending)
    v3_incumbent = V4BoAdapter._to_v3(incumbent)
    with install_qlognei_x_pending(
        [candidate_vector(item) for item in v3_pending]
    ) as pending_binding:
        selected, metadata = cuda_botorch_joint_candidates(
            v3_observations,
            v3_choices,
            q=1,
            seed=int(payload["seed"]),
            anchor=v3_incumbent,
        )
    if len(selected) != 1:
        raise OptimizerWorkerError("qLogNEI did not return exactly one candidate")
    selected_candidate = _candidate(
        __import__("step5d_autotune_v4_r005.contracts", fromlist=["Candidate"])
        .Candidate.from_named7d(candidate_vector(selected[0]))
        .canonical
    )
    return {
        "selected": selected_candidate.canonical,
        "metadata": {
            **dict(metadata),
            "worker": "resolved_v3_optimizer_profile",
            "x_pending_count": len(pending),
            "x_pending_shape": pending_binding["shape"],
        },
        "attestation": attestation,
    }


def main() -> int:
    try:
        encoded = sys.stdin.buffer.read()
        request = json.loads(encoded.decode("utf-8"))
        result = run(request)
        body = {
            "schema": RESPONSE_SCHEMA,
            "ok": True,
            "request_sha256": hashlib.sha256(encoded).hexdigest(),
            "attestation": result.pop("attestation"),
            "result": result,
        }
        sys.stdout.write(json.dumps(body, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema": RESPONSE_SCHEMA,
                    "ok": False,
                    "reason_code": "OPTIMIZER_WORKER_FAILED",
                    "detail": f"{type(exc).__name__}:{exc}",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 78


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run"]
