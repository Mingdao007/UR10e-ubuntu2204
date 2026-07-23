"""Online adapter for the isolated optimizer process and suggestion contract."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from step5d_autotune_contract import ForceCandidate
from step5d_autotune_optimizer import Observation
from step5d_autotune_r008_policy import PlannedOccurrence

from .optimizer_payloads import (
    accepted_history_digest,
    candidate_payload,
    decode_observation,
    decode_suggestion,
    observation_payload,
    optimizer_identity,
)
from .optimizer_wire import (
    MAX_RESPONSE_BYTES,
    MODE_SEEDS,
    REQUEST_SCHEMA,
    RESPONSE_SCHEMA,
    OptimizerWireError,
    canonical_bytes as _wire_canonical_bytes,
    strict_json as _wire_strict_json,
)
from .runtime_environment import production_runtime_environment
from .runtime_installation import load_runtime_pointer
from .shared_contracts import OptimizerDeploymentCertificate


WORKER_MODULE = "step5d_autotune_v3.optimizer_worker"


class OptimizerProtocolError(RuntimeError):
    """The optimizer child or its machine response failed closed."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return _wire_canonical_bytes(value)
    except OptimizerWireError as exc:
        raise OptimizerProtocolError(str(exc)) from exc


def strict_json(encoded: bytes, role: str) -> Any:
    try:
        return _wire_strict_json(encoded, role)
    except OptimizerWireError as exc:
        raise OptimizerProtocolError(str(exc)) from exc


def _pointer_certificate_fields(
    pointer: Mapping[str, Any],
) -> dict[str, str]:
    try:
        profile = pointer["profiles"]["optimizer"]
        return {
            "optimizer_digest": profile["record_tree_sha256"],
            "build_digest": profile["environment_id"],
            "runtime_attestation_digest": pointer["attestation_sha256"],
            "module_closure_digest": profile["profile_tree_sha256"],
        }
    except (KeyError, TypeError) as exc:
        raise OptimizerProtocolError(
            "optimizer runtime pointer lacks deployment identities"
        ) from exc


def deployment_certificate(
    *,
    runtime_pointer: Mapping[str, Any],
    gpu_attestation_digest: str,
) -> OptimizerDeploymentCertificate:
    return OptimizerDeploymentCertificate(
        **_pointer_certificate_fields(runtime_pointer),
        gpu_attestation_digest=gpu_attestation_digest,
    )


class ExactOptimizerClient:
    """Run BO under its promoted Python without putting attestation on the wire."""

    def __init__(
        self,
        *,
        deployment: OptimizerDeploymentCertificate,
        runtime_pointer: Mapping[str, Any] | None = None,
        timeout_s: float = 180.0,
    ) -> None:
        if timeout_s <= 0.0:
            raise ValueError("optimizer timeout must be positive")
        if not isinstance(deployment, OptimizerDeploymentCertificate):
            raise ValueError("optimizer deployment certificate is required")
        self.pointer = dict(runtime_pointer or load_runtime_pointer())
        expected = _pointer_certificate_fields(self.pointer)
        if any(getattr(deployment, name) != value for name, value in expected.items()):
            raise OptimizerProtocolError(
                "optimizer deployment certificate differs from runtime pointer"
            )
        self.deployment = deployment
        self.identity = optimizer_identity(
            optimizer_digest=deployment.optimizer_digest,
            build_digest=deployment.build_digest,
        )
        self.timeout_s = float(timeout_s)

    def propose(
        self,
        *,
        mode: str,
        observations: Sequence[Observation],
        catalog: Sequence[ForceCandidate],
        sequence: int,
        batch_a_closure: Mapping[str, Any] | None = None,
    ) -> tuple[tuple[PlannedOccurrence, ...], Mapping[str, Any]]:
        if mode not in MODE_SEEDS:
            raise OptimizerProtocolError(f"unsupported optimizer mode: {mode}")
        history_digest = accepted_history_digest(observations)
        request = {
            "schema": REQUEST_SCHEMA,
            "mode": mode,
            "seed": MODE_SEEDS[mode],
            "sequence": sequence,
            "optimizer_identity": self.identity,
            "accepted_history_digest": history_digest,
            "observations": [observation_payload(value) for value in observations],
            "catalog": [candidate_payload(value) for value in catalog],
            "batch_a_closure": (
                None if batch_a_closure is None else dict(batch_a_closure)
            ),
        }
        encoded = canonical_bytes(request)
        request_sha256 = hashlib.sha256(encoded).hexdigest()
        optimizer_python = self.pointer["profiles"]["optimizer"]["python_executable"]
        environment = production_runtime_environment(
            os.environ,
            profile="optimizer",
            runtime_pointer=self.pointer,
        )
        try:
            completed = subprocess.run(
                [optimizer_python, "-B", "-m", WORKER_MODULE],
                input=encoded,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=Path(__file__).resolve().parents[2],
                env=environment,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OptimizerProtocolError(
                f"optimizer worker could not complete: {exc}"
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise OptimizerProtocolError(
                f"optimizer worker failed ({completed.returncode}): {detail}"
            )
        if len(completed.stdout) > MAX_RESPONSE_BYTES:
            raise OptimizerProtocolError(
                "optimizer worker response exceeds size limit"
            )
        response = strict_json(completed.stdout, "optimizer response")
        fields = {
            "schema",
            "ok",
            "request_sha256",
            "suggestions",
            "evidence",
        }
        if (
            not isinstance(response, Mapping)
            or set(response) != fields
            or response.get("schema") != RESPONSE_SCHEMA
            or response.get("ok") is not True
            or response.get("request_sha256") != request_sha256
            or not isinstance(response.get("suggestions"), list)
            or len(response["suggestions"]) != 5
            or not isinstance(response.get("evidence"), Mapping)
        ):
            raise OptimizerProtocolError(
                "optimizer response fields or binding differ"
            )
        try:
            occurrences = tuple(
                decode_suggestion(
                    row,
                    identity=self.identity,
                    seed=MODE_SEEDS[mode],
                    history_digest=history_digest,
                )
                for row in response["suggestions"]
            )
        except (TypeError, ValueError) as exc:
            raise OptimizerProtocolError(
                f"optimizer suggestion contract differs: {exc}"
            ) from exc
        if (
            tuple(row.row_index for row in occurrences) != (1, 2, 3, 4, 5)
            or any(
                row.logical_batch_sequence != sequence
                or row.plan_revision != sequence
                for row in occurrences
            )
            or len({row.occurrence_uid for row in occurrences}) != len(occurrences)
            or response["evidence"].get("seed") != MODE_SEEDS[mode]
            or not isinstance(response["evidence"].get("optimizer"), Mapping)
            or response["evidence"]["optimizer"].get("device") != "cuda:0"
        ):
            raise OptimizerProtocolError(
                "optimizer worker suggestion contract differs"
            )
        return occurrences, {
            **dict(response["evidence"]),
            "worker_request_sha256": request_sha256,
            "optimizer_deployment_certificate_digest": self.deployment.digest,
        }


__all__ = [
    "ExactOptimizerClient",
    "MAX_RESPONSE_BYTES",
    "MODE_SEEDS",
    "OptimizerProtocolError",
    "REQUEST_SCHEMA",
    "RESPONSE_SCHEMA",
    "WORKER_MODULE",
    "canonical_bytes",
    "candidate_payload",
    "decode_observation",
    "deployment_certificate",
    "observation_payload",
    "strict_json",
]
