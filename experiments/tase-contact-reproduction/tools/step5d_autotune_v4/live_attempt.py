"""Typed live-attempt identities and hash-verified baseline ledger restore."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .baseline_ledger import (
    SCHEMA as LEDGER_SCHEMA,
    BaselineLedgerError,
    BaselineQualificationLedger,
    BaselineSuccessReceipt,
)
from .contracts import (
    R003_CONTRACT,
    TARGET_FORCE_N,
    V4Candidate,
    V4Contract,
    V4ContractError,
)


class AttemptKind(str, Enum):
    QUALIFICATION = "QUALIFICATION"
    PD_TRIAL = "PD_TRIAL"
    RETEST = "RETEST"


def _digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise V4ContractError(f"{role} must be a lowercase SHA-256")
    return value


def _typed_bool(value: Any, role: str) -> bool:
    if not isinstance(value, bool):
        raise V4ContractError(f"{role} must be bool")
    return value


@dataclass(frozen=True)
class V4LiveAttemptSpec:
    """Immutable binding for one live attempt before the writer starts."""

    attempt_id: str
    kind: AttemptKind
    candidate: V4Candidate
    contract_sha256: str
    campaign_fingerprint: str
    eoat_sha256: str
    model_hashes: Mapping[str, str]
    triplet_sha256: Mapping[str, str]
    input_baseline_ledger_sha256: str
    path_requested: bool = False
    # The v4 contract path is an additive compatibility seam for the existing
    # live writer.  It defaults to r003 and is still hash-bound by
    # ``contract_sha256`` at the writer boundary.
    v4_contract_path: Path = R003_CONTRACT
    baseline_ledger_seal: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise V4ContractError("attempt_id is required")
        if not isinstance(self.kind, AttemptKind):
            raise V4ContractError("attempt kind must be typed AttemptKind")
        if not isinstance(self.candidate, V4Candidate):
            raise V4ContractError("attempt candidate must be V4Candidate")
        _digest(self.contract_sha256, "contract_sha256")
        _digest(self.campaign_fingerprint, "campaign_fingerprint")
        _digest(self.eoat_sha256, "eoat_sha256")
        _digest(self.input_baseline_ledger_sha256, "input_baseline_ledger_sha256")
        if not isinstance(self.path_requested, bool):
            raise V4ContractError("path_requested must be bool")
        contract_path = self.v4_contract_path
        if not isinstance(contract_path, Path):
            contract_path = Path(contract_path)
        if not contract_path.is_absolute():
            contract_path = R003_CONTRACT.parent.parent.parent / contract_path
        object.__setattr__(self, "v4_contract_path", contract_path)
        if self.baseline_ledger_seal is not None and not isinstance(
            self.baseline_ledger_seal, Mapping
        ):
            raise V4ContractError("baseline_ledger_seal must be a mapping")
        if self.baseline_ledger_seal is not None:
            expected = self.baseline_ledger_seal.get("ledger_sha256")
            if expected != self.input_baseline_ledger_sha256:
                raise V4ContractError("baseline ledger seal hash differs from input binding")
            object.__setattr__(
                self, "baseline_ledger_seal", dict(self.baseline_ledger_seal)
            )
        if self.kind is AttemptKind.QUALIFICATION and self.path_requested:
            raise V4ContractError("QUALIFICATION attempts cannot request PATH")
        for role, mapping in (
            ("model_hashes", self.model_hashes),
            ("triplet_sha256", self.triplet_sha256),
        ):
            if not isinstance(mapping, Mapping) or not mapping:
                raise V4ContractError(f"{role} must be a non-empty mapping")
            for key, value in mapping.items():
                _digest(value, f"{role}.{key}")
        object.__setattr__(self, "model_hashes", dict(self.model_hashes))
        object.__setattr__(self, "triplet_sha256", dict(self.triplet_sha256))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v4/live-attempt-spec-v1",
            "attempt_id": self.attempt_id,
            "kind": self.kind.value,
            "candidate": self.candidate.canonical_physical,
            "candidate_uid": self.candidate.candidate_uid,
            "contract_sha256": self.contract_sha256,
            "v4_contract_path": str(self.v4_contract_path),
            "v4_contract_sha256": self.contract_sha256,
            "campaign_fingerprint": self.campaign_fingerprint,
            "eoat_sha256": self.eoat_sha256,
            "model_hashes": dict(self.model_hashes),
            "triplet_sha256": dict(self.triplet_sha256),
            "input_baseline_ledger_sha256": self.input_baseline_ledger_sha256,
            "path_requested": self.path_requested,
            "baseline_ledger_seal": (
                dict(self.baseline_ledger_seal)
                if self.baseline_ledger_seal is not None
                else None
            ),
            "target_force_n": TARGET_FORCE_N,
        }

    @property
    def v4_contract_sha256(self) -> str:
        """Compatibility spelling used by the register live writer."""

        return self.contract_sha256


@dataclass(frozen=True)
class V4LiveAttemptResult:
    """Immutable sealed result after one writer attempt terminates."""

    attempt_id: str
    kind: AttemptKind
    ready: Mapping[str, Any]
    terminal: Mapping[str, Any]
    completion: Mapping[str, Any]
    replay: Mapping[str, Any]
    eligibility: Mapping[str, Any]
    output_baseline_ledger_sha256: str
    applied_candidate_uid: str
    applied_candidate: Mapping[str, float]

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or not self.attempt_id:
            raise V4ContractError("result attempt_id is required")
        if not isinstance(self.kind, AttemptKind):
            raise V4ContractError("result kind must be typed AttemptKind")
        for role, value in (
            ("ready", self.ready),
            ("terminal", self.terminal),
            ("completion", self.completion),
            ("replay", self.replay),
            ("eligibility", self.eligibility),
            ("applied_candidate", self.applied_candidate),
        ):
            if not isinstance(value, Mapping):
                raise V4ContractError(f"{role} must be a mapping")
        _digest(self.output_baseline_ledger_sha256, "output_baseline_ledger_sha256")
        _digest(self.applied_candidate_uid, "applied_candidate_uid")
        object.__setattr__(self, "ready", dict(self.ready))
        object.__setattr__(self, "terminal", dict(self.terminal))
        object.__setattr__(self, "completion", dict(self.completion))
        object.__setattr__(self, "replay", dict(self.replay))
        object.__setattr__(self, "eligibility", dict(self.eligibility))
        object.__setattr__(self, "applied_candidate", dict(self.applied_candidate))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v4/live-attempt-result-v1",
            "attempt_id": self.attempt_id,
            "kind": self.kind.value,
            "ready": dict(self.ready),
            "terminal": dict(self.terminal),
            "completion": dict(self.completion),
            "replay": dict(self.replay),
            "eligibility": dict(self.eligibility),
            "output_baseline_ledger_sha256": self.output_baseline_ledger_sha256,
            "applied_candidate_uid": self.applied_candidate_uid,
            "applied_candidate": dict(self.applied_candidate),
        }


def ledger_payload_sha256(payload: Mapping[str, Any]) -> str:
    material = {
        key: value
        for key, value in payload.items()
        if key != "ledger_sha256"
    }
    encoded = json.dumps(
        material, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def restore_baseline_ledger_from_seal(
    contract: V4Contract, sealed: Mapping[str, Any]
) -> BaselineQualificationLedger:
    """Canonical hash-verified restore equivalent to BaselineQualificationLedger.from_seal."""
    if not isinstance(sealed, Mapping):
        raise BaselineLedgerError("sealed ledger must be a mapping")
    if sealed.get("schema") != LEDGER_SCHEMA:
        raise BaselineLedgerError("sealed ledger schema differs")
    expected = sealed.get("ledger_sha256")
    if not isinstance(expected, str):
        raise BaselineLedgerError("sealed ledger hash is missing")
    actual = ledger_payload_sha256(sealed)
    if actual != expected:
        raise BaselineLedgerError("sealed ledger hash tamper detected")
    if (
        sealed.get("campaign_fingerprint") != contract.campaign_fingerprint
        or sealed.get("eoat_sha256") != contract.eoat_sha256
    ):
        raise BaselineLedgerError("sealed ledger binding differs")
    ledger = BaselineQualificationLedger(contract)
    receipts = sealed.get("receipts")
    if not isinstance(receipts, list):
        raise BaselineLedgerError("sealed receipts are invalid")
    by_id: dict[str, BaselineSuccessReceipt] = {}
    for row in receipts:
        if not isinstance(row, Mapping):
            raise BaselineLedgerError("sealed receipt row is invalid")
        receipt = BaselineSuccessReceipt(
            attempt_id=str(row["attempt_id"]),
            terminal_stage=int(row["terminal_stage"]),
            campaign_fingerprint=str(row["campaign_fingerprint"]),
            eoat_sha256=str(row["eoat_sha256"]),
            target_force_n=float(row["target_force_n"]),
            sensor_authority=str(row["sensor_authority"]),
            completion_sha256=str(row["completion_sha256"]),
        )
        by_id[receipt.attempt_id] = receipt
        ledger.record_success(receipt)
    streak = sealed.get("consecutive_success_attempt_ids")
    if not isinstance(streak, list):
        raise BaselineLedgerError("sealed streak is invalid")
    # Rebuild exact streak order after full receipt set.
    ledger._streak = []  # noqa: SLF001 - restore is the sole external mutator path
    for attempt_id in streak:
        if attempt_id not in by_id:
            raise BaselineLedgerError("sealed streak references unknown receipt")
        ledger._streak.append(str(attempt_id))  # noqa: SLF001
    frozen = sealed.get("frozen")
    freeze_reason = sealed.get("freeze_reason")
    if not isinstance(frozen, bool) or not isinstance(freeze_reason, str):
        raise BaselineLedgerError("sealed freeze state is invalid")
    ledger._frozen = frozen  # noqa: SLF001
    ledger._freeze_reason = freeze_reason  # noqa: SLF001
    resealed = ledger.seal()
    if resealed["ledger_sha256"] != expected:
        raise BaselineLedgerError("restored ledger does not re-seal")
    return ledger


# Compatibility alias matching the plan's from_seal naming.
BaselineQualificationLedger_from_seal = restore_baseline_ledger_from_seal


def verify_ledger_hash_chain(
    previous_output_sha256: str, next_input_sha256: str
) -> None:
    if previous_output_sha256 != next_input_sha256:
        raise BaselineLedgerError("baseline ledger hash chain broken between attempts")


def _from_seal(
    cls: type[BaselineQualificationLedger],
    contract: V4Contract,
    sealed: Mapping[str, Any],
) -> BaselineQualificationLedger:
    del cls
    return restore_baseline_ledger_from_seal(contract, sealed)


# Equivalent to BaselineQualificationLedger.from_seal without editing the
# hash-bound r002 ledger source file bytes.
BaselineQualificationLedger.from_seal = classmethod(_from_seal)  # type: ignore[attr-defined]


def load_attempt_spec(
    path_or_mapping: Any, *, contract: V4Contract | None = None
) -> V4LiveAttemptSpec:
    if isinstance(path_or_mapping, V4LiveAttemptSpec):
        return path_or_mapping
    if isinstance(path_or_mapping, Mapping):
        document = dict(path_or_mapping)
    else:
        path = path_or_mapping
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema") != "step5d.autotune-v4/live-attempt-spec-v1":
        raise V4ContractError("live attempt spec schema differs")
    candidate = V4Candidate(**document["candidate"])
    kind = AttemptKind(str(document["kind"]))
    spec = V4LiveAttemptSpec(
        attempt_id=str(document["attempt_id"]),
        kind=kind,
        candidate=candidate,
        contract_sha256=str(document["contract_sha256"]),
        campaign_fingerprint=str(document["campaign_fingerprint"]),
        eoat_sha256=str(document["eoat_sha256"]),
        model_hashes=dict(document["model_hashes"]),
        triplet_sha256=dict(document["triplet_sha256"]),
        input_baseline_ledger_sha256=str(document["input_baseline_ledger_sha256"]),
        path_requested=_typed_bool(
            document.get("path_requested", False), "path_requested"
        ),
        v4_contract_path=Path(document.get("v4_contract_path", R003_CONTRACT)),
        baseline_ledger_seal=document.get("baseline_ledger_seal"),
    )
    if contract is not None:
        if (
            spec.contract_sha256 != contract.sha256
            or spec.campaign_fingerprint != contract.campaign_fingerprint
            or spec.eoat_sha256 != contract.eoat_sha256
            or dict(spec.model_hashes) != dict(contract.model_hashes)
        ):
            raise V4ContractError("live attempt spec binding differs from contract")
    return spec


__all__ = [
    "AttemptKind",
    "BaselineQualificationLedger_from_seal",
    "V4LiveAttemptResult",
    "V4LiveAttemptSpec",
    "ledger_payload_sha256",
    "load_attempt_spec",
    "restore_baseline_ledger_from_seal",
    "verify_ledger_hash_chain",
]
