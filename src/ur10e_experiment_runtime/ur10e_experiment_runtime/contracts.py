"""Typed, side-effect-free contracts for the manifest runtime."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .identity import strict_json_loads


class ExperimentRuntimeError(RuntimeError):
    """Base class for deterministic runtime failures."""


class SpecValidationError(ExperimentRuntimeError, ValueError):
    """Raised when an ExperimentSpec or RunManifest is invalid."""


class RegistryError(ExperimentRuntimeError, LookupError):
    """Raised when a component is missing, duplicated, or malformed."""


class SafetyGateError(ExperimentRuntimeError):
    """Raised when a requested lane exceeds the offline safety envelope."""


class UnsupportedExecutionError(SafetyGateError):
    """Raised when this offline package is asked to perform HIL/live work."""


class OutputPathError(ExperimentRuntimeError, OSError):
    """Raised when a run cannot claim a new exclusive output directory."""


class ResourceLockError(ExperimentRuntimeError, OSError):
    """Raised when a host-local exclusive resource lock cannot be acquired."""


@dataclass(frozen=True)
class AutotuneCandidate:
    """Stage-neutral P/I/damping parameter candidate."""

    p_gain: float
    i_gain: float
    damping: float

    def __post_init__(self) -> None:
        values = {
            "p_gain": self.p_gain,
            "i_gain": self.i_gain,
            "damping": self.damping,
        }
        for name, raw_value in values.items():
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                raise SpecValidationError(f"{name} must be a finite number")
            value = float(raw_value)
            if not math.isfinite(value):
                raise SpecValidationError(f"{name} must be a finite number")
            if name in {"p_gain", "damping"} and value <= 0:
                raise SpecValidationError(f"{name} must be greater than zero")
            if name == "i_gain" and value < 0:
                raise SpecValidationError("i_gain must be non-negative")
            object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, float]:
        return {
            "p_gain": self.p_gain,
            "i_gain": self.i_gain,
            "damping": self.damping,
        }


@dataclass(frozen=True)
class ObjectiveContract:
    target_force_n: float
    window_start_s: float
    window_end_s: float
    window_semantics: str
    bins: int
    parameter_semantics: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ObjectiveContract":
        window = value["window_s"]
        return cls(
            target_force_n=float(value["target_force_n"]),
            window_start_s=float(window[0]),
            window_end_s=float(window[1]),
            window_semantics=str(value["window_semantics"]),
            bins=int(value["bins"]),
            parameter_semantics=str(value["parameter_semantics"]),
        )


@dataclass(frozen=True)
class ExperimentSpec:
    """Immutable validated spec backed by canonical JSON bytes.

    Every mapping property returns a fresh document, so callers and adapters
    cannot mutate the identity-bearing state after validation.
    """

    _canonical_document: bytes = field(repr=False)
    fingerprint: str
    source_path: Path | None = None

    @property
    def document(self) -> dict[str, Any]:
        document = strict_json_loads(self._canonical_document)
        if not isinstance(document, dict):  # pragma: no cover - constructor guard
            raise SpecValidationError("ExperimentSpec document is not an object")
        return document

    def _section(self, name: str) -> dict[str, Any]:
        value = self.document[name]
        if not isinstance(value, dict):  # pragma: no cover - schema guard
            raise SpecValidationError(f"ExperimentSpec {name} is not an object")
        return value

    @property
    def experiment_id(self) -> str:
        return str(self.document["experiment_id"])

    @property
    def stage(self) -> Mapping[str, Any]:
        return self._section("stage")

    @property
    def components(self) -> Mapping[str, Any]:
        return self._section("components")

    @property
    def objective(self) -> Mapping[str, Any]:
        return self._section("objective")

    @property
    def bindings(self) -> Mapping[str, Any]:
        return self._section("bindings")

    @property
    def lanes(self) -> Mapping[str, Any]:
        return self._section("lanes")

    @property
    def readiness(self) -> Mapping[str, Any]:
        return self._section("readiness")

    def lane(self, lane_id: str) -> Mapping[str, Any]:
        lane = self.lanes.get(lane_id)
        if not isinstance(lane, dict):
            raise SpecValidationError(
                f"lane {lane_id!r} is not declared by {self.experiment_id}"
            )
        return lane


@dataclass(frozen=True)
class RunManifest:
    """Immutable validated final run manifest."""

    _canonical_document: bytes = field(repr=False)
    source_path: Path | None = None

    @property
    def document(self) -> dict[str, Any]:
        document = strict_json_loads(self._canonical_document)
        if not isinstance(document, dict):  # pragma: no cover - constructor guard
            raise SpecValidationError("RunManifest document is not an object")
        return document

    @property
    def run_uid(self) -> str:
        return str(self.document["run_uid"])

    @property
    def experiment_fingerprint(self) -> str:
        return str(self.document["experiment_fingerprint"])


@runtime_checkable
class TrajectoryProvider(Protocol):
    component_id: str
    version: str

    def sample(self, time_s: float, state: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return a reference without owning lifecycle or authorization."""


@runtime_checkable
class ControllerPolicy(Protocol):
    component_id: str
    version: str

    def compute(
        self,
        observation: Mapping[str, Any],
        reference: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Compute one command without writing to a robot or transport."""


@runtime_checkable
class StageAutotuneAdapter(Protocol):
    component_id: str
    version: str

    def validate_spec(self, document: Mapping[str, Any]) -> None:
        """Reject stage/component combinations that cannot be faithful."""

    def plan(self, spec: ExperimentSpec, lane_id: str) -> Mapping[str, Any]:
        """Return declarative stage details; never execute the plan."""


@runtime_checkable
class ExecutionBackend(Protocol):
    component_id: str
    version: str

    def plan(self, spec: ExperimentSpec, lane_id: str) -> Mapping[str, Any]:
        """Return backend planning metadata without starting a process."""


@runtime_checkable
class OutcomeClassifier(Protocol):
    component_id: str
    version: str

    def classify(self, evidence: Mapping[str, Any]) -> Mapping[str, Any]:
        """Classify evidence before it can reach an optimizer."""


@runtime_checkable
class SafetyEnvelope(Protocol):
    component_id: str
    version: str

    def blocked_reasons(self, spec: ExperimentSpec, lane_id: str) -> Sequence[str]:
        """Return every fail-closed reason for the selected lane."""


@runtime_checkable
class EvidenceSink(Protocol):
    component_id: str
    version: str

    def append_state(self, event: Mapping[str, Any]) -> None:
        """Append one immutable state event."""

    def finalize(self, manifest: RunManifest) -> None:
        """Publish one write-once final manifest."""
