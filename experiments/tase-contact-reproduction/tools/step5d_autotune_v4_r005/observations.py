"""Append-only r005 observations with a durable raw-force trust boundary.

The ledger never treats a caller supplied scalar, sufficient-statistics mapping,
or provenance string as evidence.  Non-qualification attempts first write one
immutable canonical raw artifact.  A fresh Python subprocess reads that file,
rebuilds the objective from every sample, and returns the receipt that is bound
into the ledger row.  Cold reads repeat the same operation before any row can
become optimizer eligible.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import copy
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from .contracts import Candidate, canonical_bytes
from step5d_force_objective import (
    FORCE_OBJECTIVE_RECEIPT_VERSION,
    FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
    ForceObjective,
    ForceObjectiveBuilder,
    ForceObjectiveError,
    ForcePathSample,
)


LEDGER_SCHEMA = "step5d.autotune-v4/r005-sealed-observations-v1"
GENESIS_SHA256 = "0" * 64
RAW_ARTIFACT_SCHEMA = "step5d.force-raw-evidence/r005-v1"
RAW_ARTIFACT_RECEIPT_VERSION = "r005-fresh-subprocess-receipt-v1"
RAW_ARTIFACT_VERIFICATION_STATE = "verified_fresh_subprocess"


class ObservationError(RuntimeError):
    """A sealed observation, raw artifact, or hash chain cannot be trusted."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ObservationError(f"{role} must be a lowercase SHA-256")
    return value


def _finite(value: Any, role: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ObservationError(f"{role} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (nonnegative and number < 0.0):
        raise ObservationError(f"{role} is invalid")
    return number


def _safe_relative_path(value: Any, role: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ObservationError(f"{role} is not a safe relative POSIX path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or not relative.parts
        or "." in relative.parts
        or ".." in relative.parts
    ):
        raise ObservationError(f"{role} escapes its artifact root")
    return value


def _canonical(value: Any, role: str) -> bytes:
    try:
        return canonical_bytes(value)
    except (TypeError, ValueError) as exc:
        raise ObservationError(f"{role} is not canonical") from exc


def _expected_receipt(
    *,
    byte_sha256: str,
    byte_size: int,
    objective_sha256: str,
    campaign_fingerprint: str,
    epoch: int,
    attempt_sequence: int,
    execution_id: str,
    candidate_uid: str,
    sample_count: int,
) -> dict[str, Any]:
    return {
        "schema": RAW_ARTIFACT_SCHEMA,
        "receipt_version": RAW_ARTIFACT_RECEIPT_VERSION,
        "artifact_byte_sha256": byte_sha256,
        "artifact_byte_size": byte_size,
        "objective_sha256": objective_sha256,
        "campaign_fingerprint": campaign_fingerprint,
        "epoch": epoch,
        "attempt_sequence": attempt_sequence,
        "execution_id": execution_id,
        "candidate_uid": candidate_uid,
        "semantic_fingerprint": FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
        "sample_count": sample_count,
    }


@dataclass(frozen=True)
class RawArtifactBinding:
    """The only durable proof that a row was rebuilt from raw PATH samples."""

    relative_path: str
    byte_sha256: str
    byte_size: int
    objective_sha256: str
    receipt_sha256: str
    sample_count: int
    campaign_fingerprint: str
    epoch: int
    attempt_sequence: int
    execution_id: str
    candidate_uid: str
    semantic_fingerprint: str = FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT
    verification_state: str = RAW_ARTIFACT_VERIFICATION_STATE
    verified_receipt: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _safe_relative_path(self.relative_path, "raw artifact relative_path")
        for name in ("byte_sha256", "objective_sha256", "receipt_sha256", "campaign_fingerprint"):
            _digest(getattr(self, name), f"raw artifact {name}")
        if (
            isinstance(self.byte_size, bool)
            or not isinstance(self.byte_size, int)
            or self.byte_size <= 0
        ):
            raise ObservationError("raw artifact byte_size is invalid")
        if (
            isinstance(self.sample_count, bool)
            or not isinstance(self.sample_count, int)
            or self.sample_count < 0
        ):
            raise ObservationError("raw artifact sample_count is invalid")
        for name in ("epoch", "attempt_sequence"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ObservationError(f"raw artifact {name} is invalid")
        for name in ("execution_id", "candidate_uid"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ObservationError(f"raw artifact {name} is invalid")
        if self.semantic_fingerprint != FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT:
            raise ObservationError("raw artifact semantic fingerprint differs")
        if self.verification_state != RAW_ARTIFACT_VERIFICATION_STATE:
            raise ObservationError("raw artifact is not fresh-subprocess verified")
        if not isinstance(self.verified_receipt, Mapping):
            raise ObservationError("raw artifact verified receipt is not an object")
        receipt = dict(self.verified_receipt)
        expected_keys = set(
            _expected_receipt(
                byte_sha256=self.byte_sha256,
                byte_size=self.byte_size,
                objective_sha256=self.objective_sha256,
                campaign_fingerprint=self.campaign_fingerprint,
                epoch=self.epoch,
                attempt_sequence=self.attempt_sequence,
                execution_id=self.execution_id,
                candidate_uid=self.candidate_uid,
                sample_count=self.sample_count,
            )
        )
        if set(receipt) != expected_keys:
            raise ObservationError("raw artifact verified receipt fields differ")
        expected = _expected_receipt(
            byte_sha256=self.byte_sha256,
            byte_size=self.byte_size,
            objective_sha256=self.objective_sha256,
            campaign_fingerprint=self.campaign_fingerprint,
            epoch=self.epoch,
            attempt_sequence=self.attempt_sequence,
            execution_id=self.execution_id,
            candidate_uid=self.candidate_uid,
            sample_count=self.sample_count,
        )
        if receipt != expected:
            raise ObservationError("raw artifact verified receipt differs")
        if _sha256(_canonical(receipt, "raw artifact receipt")) != self.receipt_sha256:
            raise ObservationError("raw artifact receipt hash differs")
        object.__setattr__(self, "verified_receipt", receipt)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": RAW_ARTIFACT_SCHEMA,
            "verification_state": self.verification_state,
            "receipt_version": RAW_ARTIFACT_RECEIPT_VERSION,
            "relative_path": self.relative_path,
            "byte_sha256": self.byte_sha256,
            "byte_size": self.byte_size,
            "objective_sha256": self.objective_sha256,
            "receipt_sha256": self.receipt_sha256,
            "sample_count": self.sample_count,
            "campaign_fingerprint": self.campaign_fingerprint,
            "epoch": self.epoch,
            "attempt_sequence": self.attempt_sequence,
            "execution_id": self.execution_id,
            "candidate_uid": self.candidate_uid,
            "semantic_fingerprint": self.semantic_fingerprint,
            "verified_receipt": dict(self.verified_receipt),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RawArtifactBinding":
        if not isinstance(value, Mapping):
            raise ObservationError("raw artifact binding is not an object")
        required = {
            "schema",
            "verification_state",
            "receipt_version",
            "relative_path",
            "byte_sha256",
            "byte_size",
            "objective_sha256",
            "receipt_sha256",
            "sample_count",
            "campaign_fingerprint",
            "epoch",
            "attempt_sequence",
            "execution_id",
            "candidate_uid",
            "semantic_fingerprint",
            "verified_receipt",
        }
        if set(value) != required:
            raise ObservationError("raw artifact binding fields differ")
        if value.get("schema") != RAW_ARTIFACT_SCHEMA:
            raise ObservationError("raw artifact binding schema differs")
        if value.get("receipt_version") != RAW_ARTIFACT_RECEIPT_VERSION:
            raise ObservationError("raw artifact binding receipt version differs")
        try:
            return cls(
                relative_path=str(value["relative_path"]),
                byte_sha256=str(value["byte_sha256"]),
                byte_size=int(value["byte_size"]),
                objective_sha256=str(value["objective_sha256"]),
                receipt_sha256=str(value["receipt_sha256"]),
                sample_count=int(value["sample_count"]),
                campaign_fingerprint=str(value["campaign_fingerprint"]),
                epoch=int(value["epoch"]),
                attempt_sequence=int(value["attempt_sequence"]),
                execution_id=str(value["execution_id"]),
                candidate_uid=str(value["candidate_uid"]),
                semantic_fingerprint=str(value["semantic_fingerprint"]),
                verification_state=str(value["verification_state"]),
                verified_receipt=value["verified_receipt"],
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ObservationError("raw artifact binding is invalid") from exc


@dataclass(frozen=True, init=False)
class VerifiedObservationEvidence:
    """Ledger-derived optimizer capability bound to one raw artifact.

    The public constructor is deliberately disabled.  A ``ForceObjective``
    and a structurally valid ``RawArtifactBinding`` are not enough to create
    this type: the r005 ledger creates it only after the fresh subprocess has
    read the artifact and returned the matching objective/receipt.  The
    optimizer sees this type, never a verification string on the objective.
    """

    objective: ForceObjective
    raw_artifact: RawArtifactBinding

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise ObservationError(
            "verified observation evidence can only be created by the ledger seam"
        )

    @classmethod
    def _from_fresh_subprocess(
        cls,
        objective: ForceObjective,
        raw_artifact: RawArtifactBinding,
    ) -> "VerifiedObservationEvidence":
        if not isinstance(objective, ForceObjective) or not isinstance(
            raw_artifact, RawArtifactBinding
        ):
            raise ObservationError("fresh verified evidence types differ")
        objective.validate_sealed()
        if _sha256(_canonical(objective.as_dict(), "verified objective")) != raw_artifact.objective_sha256:
            raise ObservationError("fresh objective is not bound to the raw artifact receipt")
        evidence = object.__new__(cls)
        object.__setattr__(evidence, "objective", objective)
        object.__setattr__(evidence, "raw_artifact", raw_artifact)
        return evidence

    @property
    def trainable(self) -> bool:
        return bool(
            self.objective.provenance == "raw_path_evidence"
            and self.objective.v2_mae_n is not None
            and self.objective.complete_bins == 550
        )

    @property
    def mae_n(self) -> float | None:
        return self.objective.v2_mae_n if self.trainable else None

    @property
    def objective_value(self) -> float | None:
        return self.mae_n

    def validate(self) -> None:
        self.objective.validate_sealed()
        if _sha256(_canonical(self.objective.as_dict(), "verified objective")) != self.raw_artifact.objective_sha256:
            raise ObservationError("verified objective/raw artifact binding differs")


@dataclass(frozen=True)
class ObservationRecord:
    """One physically returned attempt, eligible or durably excluded."""

    campaign_fingerprint: str
    epoch: int
    attempt_sequence: int
    kind: str
    candidate: Candidate
    safe_return: bool
    binding_ok: bool
    safety_gate: bool
    contact_gate: bool
    return_gate: bool
    motion_gate: bool
    timing_gate: bool
    identity_gate: bool
    qualification_passed: bool
    duration_s: float = 60.0
    force_objective: ForceObjective | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)
    sealed: bool = False
    raw_path_samples: tuple[ForcePathSample, ...] = ()
    raw_artifact: RawArtifactBinding | None = None
    verified_evidence: VerifiedObservationEvidence | None = None

    def __post_init__(self) -> None:
        _digest(self.campaign_fingerprint, "campaign_fingerprint")
        for name in ("epoch", "attempt_sequence"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ObservationError(f"{name} must be a positive integer")
        if not isinstance(self.kind, str) or not self.kind:
            raise ObservationError("observation kind must be non-empty")
        if not isinstance(self.candidate, Candidate):
            raise ObservationError("observation candidate must be typed")
        for name in (
            "safe_return",
            "binding_ok",
            "safety_gate",
            "contact_gate",
            "return_gate",
            "motion_gate",
            "timing_gate",
            "identity_gate",
            "qualification_passed",
            "sealed",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ObservationError(f"{name} must be bool")
        duration = _finite(self.duration_s, "duration_s", nonnegative=True)
        if self.force_objective is not None and not isinstance(self.force_objective, ForceObjective):
            raise ObservationError("force_objective must be the typed force objective")
        if self.verified_evidence is not None and not isinstance(
            self.verified_evidence, VerifiedObservationEvidence
        ):
            raise ObservationError("verified_evidence must be the ledger-derived wrapper")
        if not isinstance(self.metrics, Mapping):
            raise ObservationError("metrics must be a mapping")
        metrics = dict(self.metrics)
        if any(
            key in metrics
            for key in ("raw_path_samples", "raw_samples", "sample_identity_ids")
        ):
            raise ObservationError("raw sample arrays/identity IDs do not belong in bounded metrics")
        samples = tuple(self.raw_path_samples)
        if any(not isinstance(sample, ForcePathSample) for sample in samples):
            raise ObservationError("raw_path_samples must contain typed ForcePathSample values")
        if self.kind == "QUALIFICATION":
            if (
                samples
                or self.raw_artifact is not None
                or self.force_objective is not None
                or self.verified_evidence is not None
            ):
                raise ObservationError("qualification cannot carry a force artifact/objective")
        if self.raw_artifact is not None and not isinstance(self.raw_artifact, RawArtifactBinding):
            raise ObservationError("raw_artifact must be the typed verified binding")
        if self.verified_evidence is not None:
            self.verified_evidence.validate()
            if (
                self.force_objective is not None
                and self.force_objective.as_dict() != self.verified_evidence.objective.as_dict()
            ):
                raise ObservationError("verified objective differs from observation objective")
            if (
                self.raw_artifact is not None
                and self.raw_artifact != self.verified_evidence.raw_artifact
            ):
                raise ObservationError("verified artifact differs from observation artifact")
            object.__setattr__(self, "force_objective", self.verified_evidence.objective)
            object.__setattr__(self, "raw_artifact", self.verified_evidence.raw_artifact)
        if self.raw_artifact is not None:
            execution_id = metrics.get("execution_id")
            if not isinstance(execution_id, str) or not execution_id:
                raise ObservationError("verified raw artifact requires execution_id")
            if (
                self.raw_artifact.campaign_fingerprint != self.campaign_fingerprint
                or self.raw_artifact.epoch != self.epoch
                or self.raw_artifact.attempt_sequence != self.attempt_sequence
                or self.raw_artifact.candidate_uid != self.candidate_uid
                or self.raw_artifact.execution_id != execution_id
            ):
                raise ObservationError("raw artifact identity differs from observation")
            if self.force_objective is None:
                raise ObservationError("verified raw artifact requires a recomputed objective")
            if samples:
                raise ObservationError("durable observations must not retain raw sample arrays")
            if self.verified_evidence is not None:
                self.verified_evidence.validate()
        object.__setattr__(self, "duration_s", duration)
        object.__setattr__(self, "metrics", MappingProxyType(metrics))
        object.__setattr__(self, "raw_path_samples", samples)

    @property
    def mae_n(self) -> float | None:
        return self.verified_evidence.mae_n if self.verified_evidence is not None else None

    @property
    def objective(self) -> float | None:
        return (
            self.verified_evidence.objective_value
            if self.verified_evidence is not None
            else None
        )

    @property
    def eligible(self) -> bool:
        """Only a verified raw artifact can become trainable; motion is final-only."""

        if self.verified_evidence is None:
            return False
        try:
            self.validate_verified_evidence()
        except (ForceObjectiveError, ObservationError):
            return False
        return bool(
            self.verified_evidence is not None
            and self.sealed
            and self.safe_return
            and self.binding_ok
            and self.safety_gate
            and self.contact_gate
            and self.return_gate
            and self.timing_gate
            and self.identity_gate
            and self.duration_s >= 60.0
            and self.verified_evidence.trainable
        )

    def validate_for_optimizer(self) -> None:
        if not self.eligible or self.verified_evidence is None:
            raise ObservationError("observation is not backed by an eligible sealed raw objective")
        self.validate_verified_evidence()

    def validate_verified_evidence(self) -> None:
        """Validate the raw-artifact state even for safe nontrainable rows."""

        if self.kind == "QUALIFICATION":
            if not self.qualification_eligible:
                raise ObservationError("qualification evidence is not sealed")
            return
        if (
            self.verified_evidence is None
            or self.force_objective is not self.verified_evidence.objective
            or self.raw_artifact is not self.verified_evidence.raw_artifact
        ):
            raise ObservationError("observation evidence is not ledger-derived fresh raw-artifact verified")
        self.verified_evidence.validate()

    @property
    def qualification_eligible(self) -> bool:
        return bool(
            self.sealed
            and self.kind == "QUALIFICATION"
            and self.qualification_passed
            and self.safe_return
            and self.binding_ok
            and self.safety_gate
            and self.contact_gate
            and self.return_gate
            and self.timing_gate
            and self.identity_gate
            and self.force_objective is None
            and self.raw_artifact is None
        )

    @property
    def candidate_uid(self) -> str:
        return self.candidate.candidate_uid

    @property
    def observation_uid(self) -> str:
        return _sha256(canonical_bytes(self.payload()))

    def payload(self) -> dict[str, Any]:
        return {
            "schema": LEDGER_SCHEMA,
            "record_type": "observation",
            "campaign_fingerprint": self.campaign_fingerprint,
            "epoch": self.epoch,
            "attempt_sequence": self.attempt_sequence,
            "kind": self.kind,
            "candidate": self.candidate.canonical,
            "candidate_uid": self.candidate_uid,
            "safe_return": self.safe_return,
            "binding_ok": self.binding_ok,
            "safety_gate": self.safety_gate,
            "contact_gate": self.contact_gate,
            "return_gate": self.return_gate,
            "motion_gate": self.motion_gate,
            "timing_gate": self.timing_gate,
            "identity_gate": self.identity_gate,
            "qualification_passed": self.qualification_passed,
            "duration_s": self.duration_s,
            "force_objective": None if self.force_objective is None else self.force_objective.as_dict(),
            "raw_artifact": None if self.raw_artifact is None else self.raw_artifact.as_dict(),
            "verified_evidence": (
                None
                if self.verified_evidence is None
                else {
                    "objective": self.verified_evidence.objective.as_dict(),
                    "raw_artifact": self.verified_evidence.raw_artifact.as_dict(),
                }
            ),
            # Derived audit fields only; raw samples never enter the ledger row.
            "mae_n": self.mae_n,
            "objective": self.objective,
            "metrics": dict(self.metrics),
            "eligible": self.eligible,
            "sealed": self.sealed,
        }

    def seal(self) -> "ObservationRecord":
        return replace(self, sealed=True)


def _row_hash(row: Mapping[str, Any]) -> str:
    return _sha256(canonical_bytes({key: value for key, value in row.items() if key != "row_sha256"}))


def _strict_rows(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ObservationError(f"observation ledger must be a regular file: {path}")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ObservationError(f"invalid observation ledger row {number}") from exc
        if not isinstance(value, dict):
            raise ObservationError(f"observation ledger row {number} is not an object")
        rows.append(value)
    return rows


def verify_hash_chain(path: Path) -> tuple[dict[str, Any], ...]:
    rows = _strict_rows(Path(path))
    if not rows:
        raise ObservationError("observation ledger has no genesis header")
    header = rows[0]
    if header.get("schema") != LEDGER_SCHEMA or header.get("record_type") != "header":
        raise ObservationError("observation ledger header differs")
    previous = GENESIS_SHA256
    seen_attempts: set[int] = set()
    for index, row in enumerate(rows[1:], start=2):
        if row.get("schema") != LEDGER_SCHEMA or row.get("record_type") != "observation":
            raise ObservationError(f"observation row {index} schema differs")
        _digest(row.get("previous_sha256"), f"row {index} previous_sha256")
        _digest(row.get("row_sha256"), f"row {index} row_sha256")
        if row["previous_sha256"] != previous:
            raise ObservationError(f"observation row {index} chain differs")
        if row["row_sha256"] != _row_hash(row):
            raise ObservationError(f"observation row {index} hash differs")
        sequence = row.get("attempt_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise ObservationError(f"observation row {index} attempt sequence is invalid")
        if sequence in seen_attempts:
            raise ObservationError(f"observation row {index} reuses attempt sequence")
        seen_attempts.add(sequence)
        previous = row["row_sha256"]
    return tuple(rows[1:])


# This child intentionally imports only the raw objective primitive.  It does
# not import the host loop, optimizer, or any caller-side record constructor.
_FRESH_CHILD_CODE = r'''
import hashlib, json, pathlib, sys
sys.path.insert(0, sys.argv[1])
from step5d_force_objective import (
    FORCE_OBJECTIVE_RECEIPT_VERSION,
    FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
    ForceObjective,
    ForceObjectiveBuilder,
    ForcePathSample,
)

LEDGER_SCHEMA = %r
GENESIS = '0' * 64
ARTIFACT_SCHEMA = %r
ARTIFACT_RECEIPT_VERSION = %r
VERIFICATION_STATE = %r
SEMANTIC = %r

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')

def digest(value):
    return hashlib.sha256(value).hexdigest()

def safe_relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('unsafe relative path')
    relative = pathlib.PurePosixPath(value)
    if relative.is_absolute() or relative.as_posix() != value or not relative.parts:
        raise ValueError('unsafe relative path')
    if any(part in ('.', '..') for part in relative.parts):
        raise ValueError('path escape')
    return relative

def regular_under(root, relative):
    if not isinstance(relative, pathlib.PurePosixPath):
        relative = safe_relative(relative)
    candidate = root.joinpath(relative)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('artifact path contains symlink')
    resolved_root = root.resolve()
    resolved = candidate.resolve(strict=True)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError('artifact path escapes ledger root')
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError('artifact is not a regular file')
    return candidate

def expected_receipt(byte_sha, byte_size, objective_sha, campaign, epoch, sequence, execution, candidate, sample_count):
    return {
        'schema': ARTIFACT_SCHEMA,
        'receipt_version': ARTIFACT_RECEIPT_VERSION,
        'artifact_byte_sha256': byte_sha,
        'artifact_byte_size': byte_size,
        'objective_sha256': objective_sha,
        'campaign_fingerprint': campaign,
        'epoch': epoch,
        'attempt_sequence': sequence,
        'execution_id': execution,
        'candidate_uid': candidate,
        'semantic_fingerprint': SEMANTIC,
        'sample_count': sample_count,
    }

def verify_artifact(path, expected=None):
    root = path.parent.parent
    raw_bytes = path.read_bytes()
    byte_sha = digest(raw_bytes)
    byte_size = len(raw_bytes)
    artifact = json.loads(raw_bytes.decode('utf-8'))
    if not isinstance(artifact, dict):
        raise ValueError('raw artifact is not an object')
    required = {
        'schema', 'receipt_version', 'semantic_fingerprint',
        'campaign_fingerprint', 'epoch', 'attempt_sequence', 'kind',
        'execution_id', 'candidate_uid', 'samples'
    }
    if set(artifact) != required or artifact['schema'] != ARTIFACT_SCHEMA:
        raise ValueError('raw artifact fields differ')
    if artifact['receipt_version'] != ARTIFACT_RECEIPT_VERSION or artifact['semantic_fingerprint'] != SEMANTIC:
        raise ValueError('raw artifact semantic/version differs')
    if not isinstance(artifact['campaign_fingerprint'], str) or not artifact['campaign_fingerprint']:
        raise ValueError('raw artifact campaign is invalid')
    if not isinstance(artifact['epoch'], int) or isinstance(artifact['epoch'], bool) or artifact['epoch'] <= 0:
        raise ValueError('raw artifact epoch is invalid')
    if not isinstance(artifact['attempt_sequence'], int) or isinstance(artifact['attempt_sequence'], bool) or artifact['attempt_sequence'] <= 0:
        raise ValueError('raw artifact sequence is invalid')
    for key in ('kind', 'execution_id', 'candidate_uid'):
        if not isinstance(artifact[key], str) or not artifact[key]:
            raise ValueError('raw artifact identity is invalid')
    samples = artifact['samples']
    if not isinstance(samples, list):
        raise ValueError('raw artifact samples are not a list')
    builder = ForceObjectiveBuilder()
    for payload in samples:
        builder.add(ForcePathSample.from_mapping(payload))
    recomputed = builder.finalize(provenance='raw_path_evidence')
    objective_payload = recomputed.as_dict()
    objective_sha = digest(canonical(objective_payload))
    receipt = expected_receipt(
        byte_sha, byte_size, objective_sha,
        artifact['campaign_fingerprint'], artifact['epoch'], artifact['attempt_sequence'],
        artifact['execution_id'], artifact['candidate_uid'], len(samples)
    )
    return {
        'artifact': artifact,
        'objective': objective_payload,
        'objective_sha256': objective_sha,
        'receipt': receipt,
        'receipt_sha256': digest(canonical(receipt)),
        'byte_sha256': byte_sha,
        'byte_size': byte_size,
    }

def verify_ledger(path):
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    if not rows or rows[0].get('schema') != LEDGER_SCHEMA or rows[0].get('record_type') != 'header':
        raise ValueError('ledger header differs')
    previous = GENESIS
    seen = set()
    details = {}
    for row in rows[1:]:
        if row.get('schema') != LEDGER_SCHEMA or row.get('record_type') != 'observation':
            raise ValueError('ledger row schema differs')
        supplied = row.get('row_sha256')
        if row.get('previous_sha256') != previous:
            raise ValueError('ledger previous hash differs')
        body = {key: value for key, value in row.items() if key != 'row_sha256'}
        if digest(canonical(body)) != supplied:
            raise ValueError('ledger row hash differs')
        sequence = row.get('attempt_sequence')
        if sequence in seen:
            raise ValueError('ledger attempt sequence repeats')
        seen.add(sequence)
        if row.get('kind') == 'QUALIFICATION':
            if (
                row.get('raw_artifact') is not None
                or row.get('force_objective') is not None
                or row.get('verified_evidence') is not None
            ):
                raise ValueError('qualification carries force evidence')
        else:
            binding = row.get('raw_artifact')
            if not isinstance(binding, dict):
                raise ValueError('non-qualification row lacks raw artifact binding')
            metrics = row.get('metrics')
            if not isinstance(metrics, dict) or not isinstance(metrics.get('execution_id'), str) or not metrics.get('execution_id'):
                raise ValueError('non-qualification row lacks execution identity')
            relative = safe_relative(binding.get('relative_path'))
            artifact_path = regular_under(path.parent, relative)
            info = verify_artifact(artifact_path)
            artifact = info['artifact']
            expected_identity = {
                'campaign_fingerprint': row.get('campaign_fingerprint'),
                'epoch': row.get('epoch'),
                'attempt_sequence': row.get('attempt_sequence'),
                'kind': row.get('kind'),
                'execution_id': metrics.get('execution_id'),
                'candidate_uid': row.get('candidate_uid'),
            }
            for key, value in expected_identity.items():
                if artifact.get(key) != value:
                    raise ValueError('raw artifact identity differs from ledger row')
            if row.get('force_objective') is None:
                raise ValueError('non-qualification row lacks recomputed objective')
            expected_objective = ForceObjective.from_mapping(row['force_objective']).as_dict()
            if expected_objective != info['objective']:
                raise ValueError('ledger objective differs from raw artifact recomputation')
            expected_mae = info['objective'].get('v2_mae_n')
            if row.get('mae_n') != expected_mae or row.get('objective') != expected_mae:
                raise ValueError('ledger scalar differs from raw artifact recomputation')
            expected_eligible = bool(
                row.get('sealed') is True
                and row.get('safe_return') is True
                and row.get('binding_ok') is True
                and row.get('safety_gate') is True
                and row.get('contact_gate') is True
                and row.get('return_gate') is True
                and row.get('timing_gate') is True
                and row.get('identity_gate') is True
                and isinstance(row.get('duration_s'), (int, float))
                and not isinstance(row.get('duration_s'), bool)
                and row.get('duration_s') >= 60.0
                and expected_mae is not None
                and info['objective'].get('complete_bins') == 550
                and info['objective'].get('provenance') == 'raw_path_evidence'
            )
            if row.get('eligible') != expected_eligible:
                raise ValueError('ledger eligibility differs from raw artifact verification')
            expected_binding = {
                'schema': ARTIFACT_SCHEMA,
                'verification_state': VERIFICATION_STATE,
                'receipt_version': ARTIFACT_RECEIPT_VERSION,
                'relative_path': str(relative),
                'byte_sha256': info['byte_sha256'],
                'byte_size': info['byte_size'],
                'objective_sha256': info['objective_sha256'],
                'receipt_sha256': info['receipt_sha256'],
                'sample_count': len(artifact['samples']),
                'campaign_fingerprint': row['campaign_fingerprint'],
                'epoch': row['epoch'],
                'attempt_sequence': row['attempt_sequence'],
                'execution_id': metrics['execution_id'],
                'candidate_uid': row['candidate_uid'],
                'semantic_fingerprint': SEMANTIC,
                'verified_receipt': info['receipt'],
            }
            if binding != expected_binding:
                raise ValueError('raw artifact receipt/binding differs')
            expected_evidence = {
                'objective': info['objective'],
                'raw_artifact': expected_binding,
            }
            if row.get('verified_evidence') != expected_evidence:
                raise ValueError('ledger verified wrapper differs from fresh artifact verification')
            details[str(sequence)] = {'objective': info['objective'], 'raw_artifact': binding}
        previous = supplied
    return details

mode = sys.argv[2]
ledger_path = pathlib.Path(sys.argv[3])
if mode == 'artifact':
    result = verify_artifact(ledger_path)
    result.pop('artifact', None)
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))
elif mode == 'ledger':
    print(json.dumps({'ledger_sha256': digest(ledger_path.read_bytes()), 'details': verify_ledger(ledger_path)}, sort_keys=True, separators=(',', ':')))
else:
    raise ValueError('unknown verifier mode')
''' % (LEDGER_SCHEMA, RAW_ARTIFACT_SCHEMA, RAW_ARTIFACT_RECEIPT_VERSION, RAW_ARTIFACT_VERIFICATION_STATE, FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT)


def _run_fresh(mode: str, path: Path) -> dict[str, Any]:
    tools_root = str(Path(__file__).resolve().parents[1])
    try:
        result = subprocess.run(
            [sys.executable, "-c", _FRESH_CHILD_CODE, tools_root, mode, str(path.resolve())],
            check=True,
            capture_output=True,
            text=True,
            timeout=15.0,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()[-1:] or ["child exited nonzero"]
        raise ObservationError(
            f"fresh-process {mode} raw-evidence verification failed: {detail[0]}"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ObservationError(f"fresh-process {mode} raw-evidence verification failed") from exc
    try:
        value = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ObservationError(f"fresh-process {mode} returned invalid receipt") from exc
    if not isinstance(value, dict):
        raise ObservationError(f"fresh-process {mode} returned a non-object receipt")
    return value


def _fresh_process_verify(path: Path) -> str:
    result = _run_fresh("ledger", Path(path))
    digest = result.get("ledger_sha256")
    return _digest(digest, "fresh ledger digest")


class ObservationLedger:
    """Durable sealed observation store with fresh raw-artifact cold reads."""

    def __init__(
        self,
        path: Path,
        *,
        campaign_fingerprint: str,
        eoat_sha256: str,
        artifact_root: Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.campaign_fingerprint = _digest(campaign_fingerprint, "campaign_fingerprint")
        self.eoat_sha256 = _digest(eoat_sha256, "eoat_sha256")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ledger_parent = self.path.parent.resolve()
        artifact_root_input = (
            Path(artifact_root)
            if artifact_root is not None
            else self.path.parent / "raw_force_evidence"
        )
        if artifact_root_input.is_symlink():
            raise ObservationError("raw artifact root must not be a symlink")
        self.artifact_root = artifact_root_input.resolve()
        self._ensure_artifact_root()
        self._artifact_prefix = self.artifact_root.relative_to(self._ledger_parent).as_posix()
        self._cached_rows: tuple[Mapping[str, Any], ...] = ()
        self._cached_records: tuple[ObservationRecord, ...] = ()
        self._cached_attempt_sequences: frozenset[int] = frozenset()
        self._cached_head_sha256 = GENESIS_SHA256
        if self.path.exists() or self.path.is_symlink():
            verify_hash_chain(self.path)
            header = _strict_rows(self.path)[0]
            if header.get("campaign_fingerprint") != self.campaign_fingerprint:
                raise ObservationError("existing ledger campaign fingerprint differs")
            if header.get("eoat_sha256") != self.eoat_sha256:
                raise ObservationError("existing ledger EOAT binding differs")
        else:
            header = {
                "schema": LEDGER_SCHEMA,
                "record_type": "header",
                "campaign_fingerprint": self.campaign_fingerprint,
                "eoat_sha256": self.eoat_sha256,
                "r004_qualifications": "audit_only_not_imported",
                "old_r004_ledger_read": False,
                "raw_artifact_schema": RAW_ARTIFACT_SCHEMA,
                "raw_artifact_receipt_version": RAW_ARTIFACT_RECEIPT_VERSION,
            }
            with self.path.open("xb") as handle:
                handle.write(canonical_bytes(header) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._fsync_dir(self.path.parent)
        # Exactly one full fresh subprocess replay establishes the immutable
        # in-memory view for both a new ledger and a restart.  Appends below
        # extend this view after verifying only their new artifact.
        self._fresh_audit_and_cache()

    def _ensure_artifact_root(self) -> None:
        if self.artifact_root.exists() or self.artifact_root.is_symlink():
            if self.artifact_root.is_symlink() or not self.artifact_root.is_dir():
                raise ObservationError("raw artifact root must be a real directory")
        else:
            self.artifact_root.mkdir(parents=True, exist_ok=False)
        try:
            self.artifact_root.relative_to(self._ledger_parent)
        except ValueError as exc:
            raise ObservationError("raw artifact root escapes ledger directory") from exc
        current = self._ledger_parent
        for part in self.artifact_root.relative_to(self._ledger_parent).parts:
            current = current / part
            if current.is_symlink():
                raise ObservationError("raw artifact root contains a symlink")

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def read_only_identity(self) -> dict[str, str]:
        """Return the absolute ledger identity without changing any state."""

        if self.path.is_symlink() or not self.path.is_file():
            raise ObservationError("observation ledger must be a regular non-symlink file")
        resolved = self.path.resolve(strict=True)
        if resolved.is_symlink() or not resolved.is_file():
            raise ObservationError("resolved observation ledger is not a regular file")
        return {
            "ledger_path": str(resolved),
            "ledger_byte_sha256": _sha256(resolved.read_bytes()),
            "campaign_fingerprint": self.campaign_fingerprint,
            "eoat_sha256": self.eoat_sha256,
        }

    def _artifact_path(self, relative_path: str) -> Path:
        relative_path = _safe_relative_path(relative_path, "raw artifact relative_path")
        relative = PurePosixPath(relative_path)
        prefix = PurePosixPath(self._artifact_prefix)
        try:
            relative.relative_to(prefix)
        except ValueError as exc:
            raise ObservationError("raw artifact path is outside the configured store") from exc
        candidate = self._ledger_parent.joinpath(*relative.parts)
        current = self._ledger_parent
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ObservationError("raw artifact path contains a symlink")
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self._ledger_parent)
        except ValueError as exc:
            raise ObservationError("raw artifact path escapes ledger directory") from exc
        return candidate

    def _set_cache(
        self,
        rows: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
        records: tuple[ObservationRecord, ...] | list[ObservationRecord],
    ) -> None:
        if len(rows) != len(records):
            raise ObservationError("verified row/record cache lengths differ")
        immutable_rows = tuple(
            MappingProxyType(copy.deepcopy(dict(row))) for row in rows
        )
        self._cached_rows = immutable_rows
        self._cached_records = tuple(records)
        self._cached_attempt_sequences = frozenset(
            int(row["attempt_sequence"]) for row in immutable_rows
        )
        self._cached_head_sha256 = (
            str(immutable_rows[-1]["row_sha256"])
            if immutable_rows
            else GENESIS_SHA256
        )

    def _fresh_audit_and_cache(self) -> dict[str, Any]:
        rows = verify_hash_chain(self.path)
        result = _run_fresh("ledger", self.path)
        if not isinstance(result.get("details"), Mapping):
            raise ObservationError("fresh ledger verifier returned no row details")
        fresh_digest = _digest(result.get("ledger_sha256"), "fresh ledger digest")
        if fresh_digest != _sha256(self.path.read_bytes()):
            raise ObservationError("fresh ledger digest differs from local bytes")
        details = result["details"]
        records = tuple(self._record_from_row(row, details) for row in rows)
        self._set_cache(rows, records)
        return result

    @property
    def rows(self) -> tuple[dict[str, Any], ...]:
        # Return detached dictionaries for compatibility; the cache itself is
        # mapping-proxy backed and never exposed for mutation.
        return tuple(copy.deepcopy(dict(row)) for row in self._cached_rows)

    def _record_from_row(
        self,
        row: Mapping[str, Any],
        details: Mapping[str, Any],
    ) -> ObservationRecord:
        candidate_payload = dict(row["candidate"])
        candidate_payload.pop("i_off", None)
        try:
            force_objective = None
            raw_artifact = None
            verified_evidence = None
            if row.get("kind") != "QUALIFICATION":
                objective_payload = details[str(row["attempt_sequence"])]["objective"]
                force_objective = ForceObjective.from_mapping(objective_payload)
                raw_artifact = RawArtifactBinding.from_mapping(
                    details[str(row["attempt_sequence"])]["raw_artifact"]
                )
                verified_evidence = VerifiedObservationEvidence._from_fresh_subprocess(
                    force_objective,
                    raw_artifact,
                )
            return ObservationRecord(
                campaign_fingerprint=str(row["campaign_fingerprint"]),
                epoch=int(row["epoch"]),
                attempt_sequence=int(row["attempt_sequence"]),
                kind=str(row["kind"]),
                candidate=Candidate(**candidate_payload),
                safe_return=bool(row["safe_return"]),
                binding_ok=bool(row["binding_ok"]),
                safety_gate=bool(row["safety_gate"]),
                contact_gate=bool(row["contact_gate"]),
                return_gate=bool(row["return_gate"]),
                motion_gate=bool(row["motion_gate"]),
                timing_gate=bool(row["timing_gate"]),
                identity_gate=bool(row["identity_gate"]),
                qualification_passed=bool(row["qualification_passed"]),
                duration_s=float(row["duration_s"]),
                force_objective=force_objective,
                metrics=row.get("metrics", {}),
                sealed=bool(row.get("sealed")),
                raw_artifact=raw_artifact,
                verified_evidence=verified_evidence,
            )
        except (KeyError, TypeError, ValueError, ForceObjectiveError, ObservationError) as exc:
            raise ObservationError("sealed observation reconstruction failed") from exc

    @property
    def records(self) -> tuple[ObservationRecord, ...]:
        return self._cached_records

    def _write_artifact(self, record: ObservationRecord, execution_id: str) -> tuple[str, bytes, str, int]:
        identity = {
            "schema": RAW_ARTIFACT_SCHEMA,
            "campaign_fingerprint": record.campaign_fingerprint,
            "epoch": record.epoch,
            "attempt_sequence": record.attempt_sequence,
            "execution_id": execution_id,
            "candidate_uid": record.candidate_uid,
        }
        filename = _sha256(_canonical(identity, "raw artifact identity")) + ".json"
        relative_path = f"{self._artifact_prefix}/{filename}"
        target = self._artifact_path(relative_path)
        artifact = {
            "schema": RAW_ARTIFACT_SCHEMA,
            "receipt_version": RAW_ARTIFACT_RECEIPT_VERSION,
            "semantic_fingerprint": FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
            "campaign_fingerprint": record.campaign_fingerprint,
            "epoch": record.epoch,
            "attempt_sequence": record.attempt_sequence,
            "kind": record.kind,
            "execution_id": execution_id,
            "candidate_uid": record.candidate_uid,
            "samples": [sample.as_dict() for sample in record.raw_path_samples],
        }
        encoded = _canonical(artifact, "raw artifact") + b"\n"
        byte_sha = _sha256(encoded)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not target.is_file():
                raise ObservationError("raw artifact target is not a regular immutable file")
            if target.read_bytes() != encoded:
                raise ObservationError("raw artifact identity already has different bytes")
            return relative_path, encoded, byte_sha, len(encoded)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".raw-", suffix=".tmp", dir=self.artifact_root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            self._fsync_dir(self.artifact_root)
            self._fsync_dir(self.path.parent)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return relative_path, encoded, byte_sha, len(encoded)

    def append(self, record: ObservationRecord) -> ObservationRecord:
        if not isinstance(record, ObservationRecord):
            raise ObservationError("append requires a typed observation")
        if record.campaign_fingerprint != self.campaign_fingerprint:
            raise ObservationError("observation campaign fingerprint differs")
        if not record.sealed:
            record = record.seal()
        if record.attempt_sequence in self._cached_attempt_sequences:
            raise ObservationError("attempt sequence already sealed")
        previous = self._cached_head_sha256
        if record.kind == "QUALIFICATION":
            if (
                record.raw_path_samples
                or record.raw_artifact is not None
                or record.force_objective is not None
                or record.verified_evidence is not None
            ):
                raise ObservationError("qualification cannot be appended with force evidence")
        else:
            if record.raw_artifact is not None or record.verified_evidence is not None:
                raise ObservationError("append accepts only unverified attempt evidence")
            execution_id = record.metrics.get("execution_id")
            if not isinstance(execution_id, str) or not execution_id:
                raise ObservationError("non-qualification observation requires execution_id")
            relative_path, _encoded, byte_sha, byte_size = self._write_artifact(record, execution_id)
            artifact_info = _run_fresh("artifact", self._artifact_path(relative_path))
            objective_payload = artifact_info.get("objective")
            receipt = artifact_info.get("receipt")
            if not isinstance(objective_payload, Mapping) or not isinstance(receipt, Mapping):
                raise ObservationError("fresh artifact verifier returned an incomplete receipt")
            if (
                artifact_info.get("byte_sha256") != byte_sha
                or artifact_info.get("byte_size") != byte_size
            ):
                raise ObservationError("fresh artifact byte binding differs from local write")
            try:
                verified_objective = ForceObjective.from_mapping(objective_payload)
            except ForceObjectiveError as exc:
                raise ObservationError("fresh artifact objective is invalid") from exc
            if record.force_objective is not None:
                try:
                    advisory = record.force_objective
                    if advisory.as_dict() != verified_objective.as_dict():
                        raise ForceObjectiveError("caller objective differs from raw artifact recomputation")
                except ForceObjectiveError as exc:
                    raise ObservationError("caller force objective is not raw-artifact bound") from exc
            objective_sha = _digest(artifact_info.get("objective_sha256"), "fresh objective digest")
            receipt_sha = _digest(artifact_info.get("receipt_sha256"), "fresh receipt digest")
            binding = RawArtifactBinding(
                relative_path=relative_path,
                byte_sha256=byte_sha,
                byte_size=byte_size,
                objective_sha256=objective_sha,
                receipt_sha256=receipt_sha,
                sample_count=int(receipt["sample_count"]),
                campaign_fingerprint=record.campaign_fingerprint,
                epoch=record.epoch,
                attempt_sequence=record.attempt_sequence,
                execution_id=execution_id,
                candidate_uid=record.candidate_uid,
                verified_receipt=receipt,
            )
            verified_evidence = VerifiedObservationEvidence._from_fresh_subprocess(
                verified_objective,
                binding,
            )
            record = replace(
                record,
                force_objective=verified_objective,
                raw_artifact=binding,
                verified_evidence=verified_evidence,
                raw_path_samples=(),
                sealed=True,
            )
        row = record.payload()
        row["previous_sha256"] = previous
        row["row_sha256"] = _row_hash(row)
        with self.path.open("ab") as handle:
            handle.write(canonical_bytes(row) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._fsync_dir(self.path.parent)
        self._set_cache(
            self._cached_rows + (row,),
            self._cached_records + (record,),
        )
        return record

    def fresh_process_verify_details(self) -> dict[str, Any]:
        return self._fresh_audit_and_cache()

    def fresh_process_verify(self) -> str:
        result = self.fresh_process_verify_details()
        return _digest(result.get("ledger_sha256"), "fresh ledger digest")

    def observed_uids(self) -> frozenset[str]:
        return frozenset(record.candidate_uid for record in self.records)

    def trainable(self) -> tuple[ObservationRecord, ...]:
        return tuple(record for record in self.records if record.eligible)

    def bootstrap_objectives(self) -> tuple[float, ...]:
        return tuple(
            float(record.objective)
            for record in self.records
            if record.kind == "BOOTSTRAP_PD" and record.eligible and record.objective is not None
        )

    def bootstrap_anchor_objectives(self) -> tuple[float, ...]:
        anchor = Candidate()
        return tuple(
            float(record.objective)
            for record in self.records
            if (
                record.kind == "BOOTSTRAP_PD"
                and record.candidate == anchor
                and record.eligible
                and record.objective is not None
            )
        )


__all__ = [
    "GENESIS_SHA256",
    "LEDGER_SCHEMA",
    "RAW_ARTIFACT_RECEIPT_VERSION",
    "RAW_ARTIFACT_SCHEMA",
    "RAW_ARTIFACT_VERIFICATION_STATE",
    "ObservationError",
    "ObservationLedger",
    "ObservationRecord",
    "RawArtifactBinding",
    "VerifiedObservationEvidence",
    "verify_hash_chain",
]
