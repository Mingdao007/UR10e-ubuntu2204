"""Strict control-to-optimizer process protocol for production proposals."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from step5d_autotune_contract import Evaluation, ForceCandidate
from step5d_autotune_optimizer import Observation
from step5d_autotune_r008_policy import PlannedOccurrence
from ur10e_experiment_runtime import ControlCandidateUid

from .runtime_environment import production_runtime_environment
from .runtime_installation import load_runtime_pointer


REQUEST_SCHEMA = "step5d.autotune-v3/optimizer-request-v1"
RESPONSE_SCHEMA = "step5d.autotune-v3/optimizer-response-v1"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
WORKER_MODULE = "step5d_autotune_v3.optimizer_worker"
MODE_SEEDS = {"rolling_batch_a": 9009, "rolling_batch_b": 9010}


class OptimizerProtocolError(RuntimeError):
    """The optimizer child or its machine response failed closed."""


def canonical_bytes(value: Any) -> bytes:
    try:
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
    except (TypeError, ValueError) as exc:
        raise OptimizerProtocolError(f"optimizer payload is not canonical JSON: {exc}") from exc


def strict_json(encoded: bytes, role: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise OptimizerProtocolError(f"{role} repeats key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            encoded.decode("ascii"),
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"forbidden JSON constant {value!r}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise OptimizerProtocolError(f"{role} is not strict JSON: {exc}") from exc


def candidate_payload(candidate: ForceCandidate) -> dict[str, float]:
    return {
        "target_force_n": candidate.target_force_n,
        "force_p_gain": candidate.force_p_gain,
        "force_i_gain": candidate.force_i_gain,
        "force_damping": candidate.force_damping,
    }


def observation_payload(observation: Observation) -> dict[str, Any]:
    return {
        "candidate": candidate_payload(observation.candidate),
        "evaluation": observation.evaluation.history_payload(),
        "profile_id": observation.profile_id,
        "plant_epoch": observation.plant_epoch,
        "latest_trace_sha256": observation.latest_trace_sha256,
        "control_candidate_uid": (
            None
            if observation.control_candidate_uid is None
            else str(observation.control_candidate_uid)
        ),
    }


def decode_observation(payload: Any) -> Observation:
    required = {
        "candidate",
        "evaluation",
        "profile_id",
        "plant_epoch",
        "latest_trace_sha256",
        "control_candidate_uid",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise OptimizerProtocolError("optimizer observation fields differ")
    evaluation = payload["evaluation"]
    evaluation_fields = {
        "schema_version",
        "trial_uid",
        "backend_id",
        "eligible",
        "disposition",
        "objective_mae_n",
        "force_bias_n",
        "force_std_n",
        "coverage_12_plus_minus_1_ratio",
        "complete_bins",
        "safe_closure",
        "structural_failures",
        "metrics",
    }
    if not isinstance(evaluation, Mapping) or set(evaluation) != evaluation_fields:
        raise OptimizerProtocolError("optimizer evaluation fields differ")
    from step5d_autotune_contract import TrialDisposition

    evaluation_values = dict(evaluation)
    evaluation_values.pop("schema_version")
    evaluation_values["disposition"] = TrialDisposition(evaluation_values["disposition"])
    evaluation_values["structural_failures"] = tuple(
        evaluation_values["structural_failures"]
    )
    control_uid = payload["control_candidate_uid"]
    return Observation(
        candidate=ForceCandidate.from_payload(payload["candidate"]),
        evaluation=Evaluation(**evaluation_values),
        profile_id=payload["profile_id"],
        plant_epoch=payload["plant_epoch"],
        latest_trace_sha256=payload["latest_trace_sha256"],
        control_candidate_uid=(
            None if control_uid is None else ControlCandidateUid.parse(control_uid)
        ),
    )


def occurrence_payload(value: PlannedOccurrence) -> dict[str, Any]:
    return {
        "logical_batch_sequence": value.logical_batch_sequence,
        "row_index": value.row_index,
        "candidate": candidate_payload(value.candidate),
        "plan_revision": value.plan_revision,
        "selection_role": value.selection_role,
        "replicate_ordinal": value.replicate_ordinal,
    }


def decode_occurrence(value: Any) -> PlannedOccurrence:
    fields = {
        "logical_batch_sequence",
        "row_index",
        "candidate",
        "plan_revision",
        "selection_role",
        "replicate_ordinal",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise OptimizerProtocolError("optimizer occurrence fields differ")
    return PlannedOccurrence(
        logical_batch_sequence=value["logical_batch_sequence"],
        row_index=value["row_index"],
        candidate=ForceCandidate.from_payload(value["candidate"]),
        plan_revision=value["plan_revision"],
        selection_role=value["selection_role"],
        replicate_ordinal=value["replicate_ordinal"],
    )


class ExactOptimizerClient:
    """Execute optimizer-only code under the exact promoted optimizer Python."""

    def __init__(
        self,
        *,
        runtime_pointer: Mapping[str, Any] | None = None,
        timeout_s: float = 180.0,
    ) -> None:
        if timeout_s <= 0.0:
            raise ValueError("optimizer timeout must be positive")
        self.pointer = dict(runtime_pointer or load_runtime_pointer())
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
        request = {
            "schema": REQUEST_SCHEMA,
            "mode": mode,
            "seed": MODE_SEEDS[mode],
            "sequence": sequence,
            "runtime": {
                "bundle_id": self.pointer["bundle_id"],
                "environment_id": self.pointer["profiles"]["optimizer"][
                    "environment_id"
                ],
                "attestation_sha256": self.pointer["attestation_sha256"],
            },
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
            raise OptimizerProtocolError(f"optimizer worker could not complete: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise OptimizerProtocolError(
                f"optimizer worker failed ({completed.returncode}): {detail}"
            )
        if len(completed.stdout) > MAX_RESPONSE_BYTES:
            raise OptimizerProtocolError("optimizer worker response exceeds size limit")
        response = strict_json(completed.stdout, "optimizer response")
        fields = {
            "schema",
            "ok",
            "request_sha256",
            "runtime",
            "gpu",
            "occurrences",
            "evidence",
            "module_closure",
        }
        if (
            not isinstance(response, Mapping)
            or set(response) != fields
            or response.get("schema") != RESPONSE_SCHEMA
            or response.get("ok") is not True
            or response.get("request_sha256") != request_sha256
            or response.get("runtime") != request["runtime"]
            or not isinstance(response.get("occurrences"), list)
            or len(response["occurrences"]) != 5
            or not isinstance(response.get("evidence"), Mapping)
        ):
            raise OptimizerProtocolError("optimizer response fields or binding differ")
        closure = response["module_closure"]
        if (
            not isinstance(closure, Mapping)
            or set(closure) != {"sha256", "module_file_count", "violations"}
            or not isinstance(closure.get("sha256"), str)
            or len(closure["sha256"]) != 64
            or isinstance(closure.get("module_file_count"), bool)
            or not isinstance(closure.get("module_file_count"), int)
            or closure["module_file_count"] < 1
            or closure.get("violations") != []
        ):
            raise OptimizerProtocolError("optimizer worker module closure differs")
        gpu = response["gpu"]
        gpu_fields = {
            "visible_devices",
            "mapped_device",
            "name",
            "torch_version",
            "botorch_version",
            "gpytorch_version",
            "torch_cuda_version",
        }
        if (
            not isinstance(gpu, Mapping)
            or set(gpu) != gpu_fields
            or gpu.get("visible_devices") != environment["CUDA_VISIBLE_DEVICES"]
            or gpu.get("mapped_device") != "cuda:0"
            or any(
                not isinstance(gpu.get(field), str) or not gpu[field]
                for field in gpu_fields - {"visible_devices", "mapped_device"}
            )
        ):
            raise OptimizerProtocolError("optimizer worker GPU binding differs")
        occurrences = tuple(decode_occurrence(row) for row in response["occurrences"])
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
            raise OptimizerProtocolError("optimizer worker occurrence contract differs")
        return occurrences, {
            **dict(response["evidence"]),
            "worker_request_sha256": request_sha256,
            "worker_gpu": response["gpu"],
            "worker_module_closure": closure,
        }


__all__ = [
    "ExactOptimizerClient",
    "MODE_SEEDS",
    "OptimizerProtocolError",
    "REQUEST_SCHEMA",
    "RESPONSE_SCHEMA",
    "candidate_payload",
    "canonical_bytes",
    "decode_observation",
    "occurrence_payload",
    "strict_json",
]
