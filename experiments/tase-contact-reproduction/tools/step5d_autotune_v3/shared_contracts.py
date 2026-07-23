"""Pure wire contracts shared by online execution and offline optimization.

This module intentionally has no filesystem, process, GPU, ROS, controller, or
network dependency.  It defines data and validation only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence


TRIAL_SPEC_SCHEMA = "step5d.autotune-v3/trial-spec-v1"
TRIAL_RESULT_SCHEMA = "step5d.autotune-v3/trial-result-v1"
CANDIDATE_SUGGESTION_SCHEMA = "step5d.autotune-v3/candidate-suggestion-v1"
OPTIMIZER_DEPLOYMENT_CERTIFICATE_SCHEMA = (
    "step5d.autotune-v3/optimizer-deployment-certificate-v1"
)
_HEX = frozenset("0123456789abcdef")
_FORBIDDEN_CONSTRAINT_KEYS = frozenset(
    {
        "arm",
        "authorization",
        "execute",
        "lease",
        "live",
        "motion",
        "play",
        "writer",
    }
)


class SharedContractError(ValueError):
    pass


class TerminalDisposition(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    SAFETY_STOP = "SAFETY_STOP"
    TIMEOUT = "TIMEOUT"
    PROCESS_FAILURE = "PROCESS_FAILURE"
    REJECTED = "REJECTED"


def _sha256(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or not set(value) <= _HEX
    ):
        raise SharedContractError(f"{role} must be a lowercase SHA-256")
    return value


def _text(value: Any, role: str, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
    ):
        raise SharedContractError(f"{role} must be bounded non-empty text")
    return value


def _positive_int(value: Any, role: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SharedContractError(f"{role} must be a positive integer")
    return value


def _non_negative_int(value: Any, role: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SharedContractError(f"{role} must be a non-negative integer")
    return value


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SharedContractError(f"{role} must be finite")
    number = float(value)
    if not math.isfinite(number):
        raise SharedContractError(f"{role} must be finite")
    return number


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                _plain(value),
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SharedContractError(f"contract is not canonical JSON: {exc}") from exc


def canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _freeze_mapping(value: Mapping[str, Any], role: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SharedContractError(f"{role} must be an object")
    frozen: dict[str, Any] = {}
    for raw_key, raw_value in sorted(value.items()):
        key = _text(raw_key, f"{role} key", maximum=128)
        if isinstance(raw_value, Mapping):
            frozen[key] = _freeze_mapping(raw_value, f"{role}.{key}")
        elif isinstance(raw_value, (tuple, list)):
            frozen[key] = tuple(
                _freeze_mapping(item, f"{role}.{key}")
                if isinstance(item, Mapping)
                else item
                for item in raw_value
            )
        else:
            frozen[key] = raw_value
    canonical_bytes(frozen)
    return MappingProxyType(frozen)


def _exact(value: Any, fields: set[str], role: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise SharedContractError(f"{role} fields differ")
    return value


@dataclass(frozen=True)
class UnitParameter:
    value: float
    unit: str
    lower_bound: float
    upper_bound: float

    def __post_init__(self) -> None:
        value = _finite(self.value, "parameter value")
        lower = _finite(self.lower_bound, "parameter lower bound")
        upper = _finite(self.upper_bound, "parameter upper bound")
        if lower > upper or not lower <= value <= upper:
            raise SharedContractError("parameter value must be within ordered bounds")
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "lower_bound", lower)
        object.__setattr__(self, "upper_bound", upper)
        object.__setattr__(self, "unit", _text(self.unit, "parameter unit", maximum=32))

    def to_payload(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "bounds": {
                "lower": self.lower_bound,
                "upper": self.upper_bound,
            },
        }

    @classmethod
    def from_payload(cls, value: Any) -> "UnitParameter":
        row = _exact(value, {"value", "unit", "bounds"}, "unit parameter")
        bounds = _exact(row["bounds"], {"lower", "upper"}, "parameter bounds")
        return cls(
            value=row["value"],
            unit=row["unit"],
            lower_bound=bounds["lower"],
            upper_bound=bounds["upper"],
        )


def _parameters(value: Mapping[str, Any]) -> Mapping[str, UnitParameter]:
    if not isinstance(value, Mapping) or not value:
        raise SharedContractError("parameters must be a non-empty object")
    result: dict[str, UnitParameter] = {}
    for raw_name, raw_parameter in sorted(value.items()):
        name = _text(raw_name, "parameter name", maximum=64)
        parameter = (
            raw_parameter
            if isinstance(raw_parameter, UnitParameter)
            else UnitParameter.from_payload(raw_parameter)
        )
        result[name] = parameter
    return MappingProxyType(result)


def _parameter_payload(value: Mapping[str, UnitParameter]) -> dict[str, Any]:
    return {name: parameter.to_payload() for name, parameter in sorted(value.items())}


@dataclass(frozen=True)
class TrialSpec:
    trial_id: str
    suggestion_id: str
    parameters: Mapping[str, UnitParameter]
    release_id: str
    safety_id: str
    deadline_unix_ns: int
    required_observations: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "trial_id", _text(self.trial_id, "trial ID"))
        object.__setattr__(
            self,
            "suggestion_id",
            _sha256(self.suggestion_id, "suggestion ID"),
        )
        object.__setattr__(self, "parameters", _parameters(self.parameters))
        object.__setattr__(self, "release_id", _sha256(self.release_id, "release ID"))
        object.__setattr__(self, "safety_id", _sha256(self.safety_id, "safety ID"))
        object.__setattr__(
            self,
            "deadline_unix_ns",
            _positive_int(self.deadline_unix_ns, "trial deadline"),
        )
        observations = tuple(
            _text(value, "required observation", maximum=96)
            for value in self.required_observations
        )
        if not observations or len(set(observations)) != len(observations):
            raise SharedContractError("required observations must be unique and non-empty")
        object.__setattr__(self, "required_observations", observations)

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema": TRIAL_SPEC_SCHEMA,
            "trial_id": self.trial_id,
            "suggestion_id": self.suggestion_id,
            "parameters": _parameter_payload(self.parameters),
            "release_id": self.release_id,
            "safety_id": self.safety_id,
            "deadline_unix_ns": self.deadline_unix_ns,
            "required_observations": list(self.required_observations),
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.identity_payload())

    def to_payload(self) -> dict[str, Any]:
        return {**self.identity_payload(), "digest": self.digest}

    @classmethod
    def from_payload(cls, value: Any) -> "TrialSpec":
        row = _exact(
            value,
            {
                "schema",
                "trial_id",
                "suggestion_id",
                "parameters",
                "release_id",
                "safety_id",
                "deadline_unix_ns",
                "required_observations",
                "digest",
            },
            "TrialSpec",
        )
        if row["schema"] != TRIAL_SPEC_SCHEMA:
            raise SharedContractError("TrialSpec schema differs")
        observations = row["required_observations"]
        if not isinstance(observations, list):
            raise SharedContractError("TrialSpec observations must be a list")
        result = cls(
            trial_id=row["trial_id"],
            suggestion_id=row["suggestion_id"],
            parameters=row["parameters"],
            release_id=row["release_id"],
            safety_id=row["safety_id"],
            deadline_unix_ns=row["deadline_unix_ns"],
            required_observations=tuple(observations),
        )
        if row["digest"] != result.digest:
            raise SharedContractError("TrialSpec digest differs")
        return result


@dataclass(frozen=True)
class MetricValue:
    value: float
    unit: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _finite(self.value, "metric value"))
        object.__setattr__(self, "unit", _text(self.unit, "metric unit", maximum=32))

    def to_payload(self) -> dict[str, Any]:
        return {"value": self.value, "unit": self.unit}

    @classmethod
    def from_payload(cls, value: Any) -> "MetricValue":
        row = _exact(value, {"value", "unit"}, "metric")
        return cls(value=row["value"], unit=row["unit"])


@dataclass(frozen=True)
class ArtifactReference:
    role: str
    uri: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", _text(self.role, "artifact role", maximum=64))
        object.__setattr__(self, "uri", _text(self.uri, "artifact URI", maximum=1024))
        object.__setattr__(self, "sha256", _sha256(self.sha256, "artifact SHA-256"))
        object.__setattr__(
            self,
            "size_bytes",
            _non_negative_int(self.size_bytes, "artifact size"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "uri": self.uri,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_payload(cls, value: Any) -> "ArtifactReference":
        return cls(**_exact(value, {"role", "uri", "sha256", "size_bytes"}, "artifact"))


@dataclass(frozen=True)
class SafetyObservation:
    name: str
    passed: bool
    observed_at_unix_ns: int
    evidence_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "name",
            _text(self.name, "safety observation", maximum=96),
        )
        if not isinstance(self.passed, bool):
            raise SharedContractError("safety observation passed must be boolean")
        object.__setattr__(
            self,
            "observed_at_unix_ns",
            _positive_int(self.observed_at_unix_ns, "safety observation time"),
        )
        object.__setattr__(
            self,
            "evidence_sha256",
            _sha256(self.evidence_sha256, "safety evidence SHA-256"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "observed_at_unix_ns": self.observed_at_unix_ns,
            "evidence_sha256": self.evidence_sha256,
        }

    @classmethod
    def from_payload(cls, value: Any) -> "SafetyObservation":
        return cls(
            **_exact(
                value,
                {"name", "passed", "observed_at_unix_ns", "evidence_sha256"},
                "safety observation",
            )
        )


@dataclass(frozen=True)
class TrialResult:
    trial_id: str
    disposition: TerminalDisposition
    metrics: Mapping[str, MetricValue]
    stop_reason: str
    started_at_unix_ns: int
    completed_at_unix_ns: int
    safety_observations: tuple[SafetyObservation, ...]
    artifacts: tuple[ArtifactReference, ...]
    runner_version: str
    trial_spec_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "trial_id", _text(self.trial_id, "trial ID"))
        if not isinstance(self.disposition, TerminalDisposition):
            try:
                object.__setattr__(
                    self,
                    "disposition",
                    TerminalDisposition(self.disposition),
                )
            except (TypeError, ValueError) as exc:
                raise SharedContractError("terminal disposition differs") from exc
        metric_values: dict[str, MetricValue] = {}
        for raw_name, raw_metric in sorted(self.metrics.items()):
            name = _text(raw_name, "metric name", maximum=64)
            metric_values[name] = (
                raw_metric
                if isinstance(raw_metric, MetricValue)
                else MetricValue.from_payload(raw_metric)
            )
        if self.disposition is TerminalDisposition.SUCCEEDED and not metric_values:
            raise SharedContractError("successful TrialResult requires metrics")
        object.__setattr__(self, "metrics", MappingProxyType(metric_values))
        object.__setattr__(
            self,
            "stop_reason",
            _text(self.stop_reason, "stop reason", maximum=256),
        )
        started = _positive_int(self.started_at_unix_ns, "trial start")
        completed = _positive_int(self.completed_at_unix_ns, "trial completion")
        if completed < started:
            raise SharedContractError("trial completion precedes start")
        object.__setattr__(self, "started_at_unix_ns", started)
        object.__setattr__(self, "completed_at_unix_ns", completed)
        observations = tuple(self.safety_observations)
        if any(not isinstance(value, SafetyObservation) for value in observations):
            raise SharedContractError("safety observations differ")
        if len({value.name for value in observations}) != len(observations):
            raise SharedContractError("safety observations repeat a name")
        object.__setattr__(self, "safety_observations", observations)
        artifacts = tuple(self.artifacts)
        if any(not isinstance(value, ArtifactReference) for value in artifacts):
            raise SharedContractError("artifact references differ")
        if len({value.role for value in artifacts}) != len(artifacts):
            raise SharedContractError("artifact references repeat a role")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(
            self,
            "runner_version",
            _text(self.runner_version, "runner version", maximum=128),
        )
        object.__setattr__(
            self,
            "trial_spec_digest",
            _sha256(self.trial_spec_digest, "TrialSpec digest"),
        )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema": TRIAL_RESULT_SCHEMA,
            "trial_id": self.trial_id,
            "disposition": self.disposition.value,
            "metrics": {
                name: metric.to_payload() for name, metric in sorted(self.metrics.items())
            },
            "stop_reason": self.stop_reason,
            "timing": {
                "started_at_unix_ns": self.started_at_unix_ns,
                "completed_at_unix_ns": self.completed_at_unix_ns,
            },
            "safety_observations": [
                value.to_payload() for value in self.safety_observations
            ],
            "artifacts": [value.to_payload() for value in self.artifacts],
            "runner_version": self.runner_version,
            "trial_spec_digest": self.trial_spec_digest,
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.identity_payload())

    def to_payload(self) -> dict[str, Any]:
        return {**self.identity_payload(), "digest": self.digest}

    @classmethod
    def from_payload(cls, value: Any) -> "TrialResult":
        row = _exact(
            value,
            {
                "schema",
                "trial_id",
                "disposition",
                "metrics",
                "stop_reason",
                "timing",
                "safety_observations",
                "artifacts",
                "runner_version",
                "trial_spec_digest",
                "digest",
            },
            "TrialResult",
        )
        if row["schema"] != TRIAL_RESULT_SCHEMA:
            raise SharedContractError("TrialResult schema differs")
        timing = _exact(
            row["timing"],
            {"started_at_unix_ns", "completed_at_unix_ns"},
            "TrialResult timing",
        )
        if not isinstance(row["metrics"], Mapping):
            raise SharedContractError("TrialResult metrics must be an object")
        if not isinstance(row["safety_observations"], list):
            raise SharedContractError(
                "TrialResult safety observations must be a list"
            )
        if not isinstance(row["artifacts"], list):
            raise SharedContractError("TrialResult artifacts must be a list")
        result = cls(
            trial_id=row["trial_id"],
            disposition=row["disposition"],
            metrics={
                name: MetricValue.from_payload(metric)
                for name, metric in row["metrics"].items()
            },
            stop_reason=row["stop_reason"],
            started_at_unix_ns=timing["started_at_unix_ns"],
            completed_at_unix_ns=timing["completed_at_unix_ns"],
            safety_observations=tuple(
                SafetyObservation.from_payload(observation)
                for observation in row["safety_observations"]
            ),
            artifacts=tuple(
                ArtifactReference.from_payload(artifact)
                for artifact in row["artifacts"]
            ),
            runner_version=row["runner_version"],
            trial_spec_digest=row["trial_spec_digest"],
        )
        if row["digest"] != result.digest:
            raise SharedContractError("TrialResult digest differs")
        return result


def accept_terminal_result(
    current: TrialResult | None,
    proposed: TrialResult,
) -> TrialResult:
    if not isinstance(proposed, TrialResult):
        raise SharedContractError("proposed terminal result differs")
    if current is None:
        return proposed
    if not isinstance(current, TrialResult) or current.trial_id != proposed.trial_id:
        raise SharedContractError("terminal result trial identity differs")
    if current.digest != proposed.digest:
        raise SharedContractError("trial already has a different terminal result")
    return current


@dataclass(frozen=True)
class CandidateSuggestion:
    suggestion_id: str
    parameters: Mapping[str, UnitParameter]
    optimizer_digest: str
    schema_digest: str
    build_digest: str
    seed: int
    accepted_history_digest: str
    constraints: Mapping[str, Any]
    uncertainty: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "suggestion_id",
            _sha256(self.suggestion_id, "suggestion ID"),
        )
        object.__setattr__(self, "parameters", _parameters(self.parameters))
        for name in ("optimizer_digest", "schema_digest", "build_digest"):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        object.__setattr__(self, "seed", _non_negative_int(self.seed, "optimizer seed"))
        object.__setattr__(
            self,
            "accepted_history_digest",
            _sha256(self.accepted_history_digest, "accepted history digest"),
        )
        constraints = _freeze_mapping(self.constraints, "constraints")
        if set(constraints) & _FORBIDDEN_CONSTRAINT_KEYS:
            raise SharedContractError(
                "CandidateSuggestion cannot carry execution authorization"
            )
        object.__setattr__(self, "constraints", constraints)
        object.__setattr__(
            self,
            "uncertainty",
            _freeze_mapping(self.uncertainty, "uncertainty"),
        )
        expected = canonical_sha256(self.identity_payload(include_id=False))
        if self.suggestion_id != expected:
            raise SharedContractError("suggestion ID is not content-derived")

    def identity_payload(self, *, include_id: bool = True) -> dict[str, Any]:
        payload = {
            "schema": CANDIDATE_SUGGESTION_SCHEMA,
            "parameters": _parameter_payload(self.parameters),
            "optimizer_digest": self.optimizer_digest,
            "schema_digest": self.schema_digest,
            "build_digest": self.build_digest,
            "seed": self.seed,
            "accepted_history_digest": self.accepted_history_digest,
            "constraints": _plain(self.constraints),
            "uncertainty": _plain(self.uncertainty),
        }
        if include_id:
            payload["suggestion_id"] = self.suggestion_id
        return payload

    @classmethod
    def create(
        cls,
        *,
        parameters: Mapping[str, UnitParameter],
        optimizer_digest: str,
        schema_digest: str,
        build_digest: str,
        seed: int,
        accepted_history_digest: str,
        constraints: Mapping[str, Any],
        uncertainty: Mapping[str, Any],
    ) -> "CandidateSuggestion":
        provisional = {
            "schema": CANDIDATE_SUGGESTION_SCHEMA,
            "parameters": _parameter_payload(_parameters(parameters)),
            "optimizer_digest": _sha256(optimizer_digest, "optimizer digest"),
            "schema_digest": _sha256(schema_digest, "schema digest"),
            "build_digest": _sha256(build_digest, "build digest"),
            "seed": _non_negative_int(seed, "optimizer seed"),
            "accepted_history_digest": _sha256(
                accepted_history_digest,
                "accepted history digest",
            ),
            "constraints": _plain(_freeze_mapping(constraints, "constraints")),
            "uncertainty": _plain(_freeze_mapping(uncertainty, "uncertainty")),
        }
        return cls(
            suggestion_id=canonical_sha256(provisional),
            parameters=parameters,
            optimizer_digest=optimizer_digest,
            schema_digest=schema_digest,
            build_digest=build_digest,
            seed=seed,
            accepted_history_digest=accepted_history_digest,
            constraints=constraints,
            uncertainty=uncertainty,
        )

    def to_payload(self) -> dict[str, Any]:
        return self.identity_payload()

    @classmethod
    def from_payload(cls, value: Any) -> "CandidateSuggestion":
        row = _exact(
            value,
            {
                "schema",
                "suggestion_id",
                "parameters",
                "optimizer_digest",
                "schema_digest",
                "build_digest",
                "seed",
                "accepted_history_digest",
                "constraints",
                "uncertainty",
            },
            "CandidateSuggestion",
        )
        if row["schema"] != CANDIDATE_SUGGESTION_SCHEMA:
            raise SharedContractError("CandidateSuggestion schema differs")
        values = dict(row)
        values.pop("schema")
        return cls(**values)


@dataclass(frozen=True)
class OptimizerDeploymentCertificate:
    optimizer_digest: str
    build_digest: str
    runtime_attestation_digest: str
    gpu_attestation_digest: str
    module_closure_digest: str

    def __post_init__(self) -> None:
        for name in (
            "optimizer_digest",
            "build_digest",
            "runtime_attestation_digest",
            "gpu_attestation_digest",
            "module_closure_digest",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))

    def to_payload(self) -> dict[str, str]:
        return {
            "schema": OPTIMIZER_DEPLOYMENT_CERTIFICATE_SCHEMA,
            "optimizer_digest": self.optimizer_digest,
            "build_digest": self.build_digest,
            "runtime_attestation_digest": self.runtime_attestation_digest,
            "gpu_attestation_digest": self.gpu_attestation_digest,
            "module_closure_digest": self.module_closure_digest,
        }

    @classmethod
    def from_payload(cls, value: Any) -> "OptimizerDeploymentCertificate":
        row = _exact(
            value,
            {
                "schema",
                "optimizer_digest",
                "build_digest",
                "runtime_attestation_digest",
                "gpu_attestation_digest",
                "module_closure_digest",
            },
            "OptimizerDeploymentCertificate",
        )
        if row["schema"] != OPTIMIZER_DEPLOYMENT_CERTIFICATE_SCHEMA:
            raise SharedContractError(
                "OptimizerDeploymentCertificate schema differs"
            )
        values = dict(row)
        values.pop("schema")
        return cls(**values)

    @property
    def digest(self) -> str:
        return canonical_sha256(self.to_payload())


SHARED_SCHEMA_DIGEST = canonical_sha256(
    {
        "candidate_suggestion": CANDIDATE_SUGGESTION_SCHEMA,
        "optimizer_deployment_certificate": OPTIMIZER_DEPLOYMENT_CERTIFICATE_SCHEMA,
        "trial_result": TRIAL_RESULT_SCHEMA,
        "trial_spec": TRIAL_SPEC_SCHEMA,
    }
)


__all__ = [
    "ArtifactReference",
    "CANDIDATE_SUGGESTION_SCHEMA",
    "CandidateSuggestion",
    "MetricValue",
    "OPTIMIZER_DEPLOYMENT_CERTIFICATE_SCHEMA",
    "OptimizerDeploymentCertificate",
    "SHARED_SCHEMA_DIGEST",
    "SafetyObservation",
    "SharedContractError",
    "TRIAL_RESULT_SCHEMA",
    "TRIAL_SPEC_SCHEMA",
    "TerminalDisposition",
    "TrialResult",
    "TrialSpec",
    "UnitParameter",
    "accept_terminal_result",
    "canonical_bytes",
    "canonical_sha256",
]
