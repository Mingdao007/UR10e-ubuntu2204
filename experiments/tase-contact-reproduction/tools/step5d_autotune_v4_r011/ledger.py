"""Append-only R011 ledger bound to exactly one new release identity."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .common import R011ValueError, canonical_bytes, digest, strict_json_object
from .censor import CensoredObservation, ExactObservation, Observation as TypedObservation, validate_observation
from .identity import ReleaseIdentity, validate_release_identity
from .safety_filter import SafetyIntervention, validate_safety_intervention


LEDGER_SCHEMA = "step5d.autotune-v4/r011-ledger-v1"
HEADER_RECORD_TYPE = "r011_header"
OBSERVATION_RECORD_TYPE = "r011_typed_observation"
SAFETY_INTERVENTION_RECORD_TYPE = "r011_safety_intervention"
GENESIS_SHA256 = "0" * 64


class R011LedgerError(RuntimeError):
    """R011 ledger is malformed or cannot be admitted."""


class R011IdentityAdmissionError(R011LedgerError):
    """Ledger identity is absent or differs."""


class HistoricalLedgerResumeError(R011LedgerError):
    """An R008/R009/R010 ledger was presented to the R011 resume seam."""


def _record_hash(record: Mapping[str, Any]) -> str:
    return digest({key: value for key, value in record.items() if key != "record_sha256"})


def header_record(identity: ReleaseIdentity) -> dict[str, Any]:
    if not isinstance(identity, ReleaseIdentity):
        raise R011IdentityAdmissionError("R011 ledger requires a typed release identity")
    record = {
        "schema": LEDGER_SCHEMA,
        "record_type": HEADER_RECORD_TYPE,
        "release_identity": identity.as_dict(),
        "release_identity_sha256": identity.release_identity_sha256,
        "campaign_fingerprint": identity.campaign_fingerprint,
        "previous_record_sha256": GENESIS_SHA256,
    }
    record["record_sha256"] = _record_hash(record)
    return record


def _strict_records(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R011LedgerError(f"R011 ledger must be a regular file: {path}")
    rows: list[dict[str, Any]] = []

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise R011LedgerError(f"R011 ledger repeats key {key!r}")
            result[key] = value
        return result

    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise R011LedgerError(f"R011 ledger contains blank line {index}")
        try:
            value = json.loads(
                line,
                object_pairs_hook=unique_pairs,
                parse_constant=lambda token: (_ for _ in ()).throw(R011LedgerError(f"R011 ledger contains {token}")),
            )
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise R011LedgerError(f"R011 ledger line {index} is invalid") from exc
        if not isinstance(value, dict):
            raise R011LedgerError(f"R011 ledger line {index} is not an object")
        rows.append(value)
    if not rows:
        raise R011LedgerError("R011 ledger is empty")
    return rows


def _atomic(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise R011LedgerError(f"unsafe R011 ledger path: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _verify(records: list[dict[str, Any]], expected: ReleaseIdentity | None) -> tuple[ReleaseIdentity, tuple[dict[str, Any], ...]]:
    header = records[0]
    schema = str(header.get("schema", "")).lower()
    if schema in {"step5d.autotune-v4/r008-ledger-v1", "step5d.autotune-v4/r009-ledger-v1", "step5d.autotune-v4/r010-ledger-v1"} or any(token in schema for token in ("r008", "r009", "r010")):
        raise HistoricalLedgerResumeError("R008/R009/R010 ledgers are historical-only and cannot resume R011")
    if header.get("schema") != LEDGER_SCHEMA or header.get("record_type") != HEADER_RECORD_TYPE:
        raise R011IdentityAdmissionError("R011 ledger header schema/type differs")
    required = {"schema", "record_type", "release_identity", "release_identity_sha256", "campaign_fingerprint", "previous_record_sha256", "record_sha256"}
    if set(header) != required:
        raise R011IdentityAdmissionError("R011 ledger header omits full release identity")
    identity = validate_release_identity(header["release_identity"])
    if header["release_identity_sha256"] != identity.release_identity_sha256 or header["campaign_fingerprint"] != identity.campaign_fingerprint:
        raise R011IdentityAdmissionError("R011 ledger identity digest differs")
    if expected is not None and expected.as_dict() != identity.as_dict():
        raise R011IdentityAdmissionError("R011 ledger identity mismatches resume admission")
    if header["previous_record_sha256"] != GENESIS_SHA256 or header["record_sha256"] != _record_hash(header):
        raise R011LedgerError("R011 ledger header hash differs")
    previous = header["record_sha256"]
    observations: list[dict[str, Any]] = []
    for index, record in enumerate(records[1:], 2):
        if record.get("schema") != LEDGER_SCHEMA or record.get("record_type") not in {OBSERVATION_RECORD_TYPE, SAFETY_INTERVENTION_RECORD_TYPE}:
            raise R011LedgerError(f"R011 ledger record {index} type differs")
        if record.get("release_identity_sha256") != identity.release_identity_sha256 or record.get("campaign_fingerprint") != identity.campaign_fingerprint:
            raise R011IdentityAdmissionError(f"R011 observation {index} identity differs")
        if record.get("previous_record_sha256") != previous or record.get("record_sha256") != _record_hash(record):
            raise R011LedgerError(f"R011 ledger chain/hash breaks at record {index}")
        if record["record_type"] == OBSERVATION_RECORD_TYPE:
            if set(record) != {"schema", "record_type", "release_identity_sha256", "campaign_fingerprint", "previous_record_sha256", "observation", "record_sha256"}:
                raise R011LedgerError(f"R011 typed observation {index} fields differ")
            try:
                row = validate_observation(record["observation"])
            except Exception as exc:
                raise R011LedgerError(f"R011 typed observation {index} is invalid") from exc
            if row.release_identity_sha256 != identity.release_identity_sha256:
                raise R011IdentityAdmissionError(f"R011 nested observation {index} identity differs")
        else:
            if set(record) != {"schema", "record_type", "release_identity_sha256", "campaign_fingerprint", "previous_record_sha256", "intervention", "record_sha256"}:
                raise R011LedgerError(f"R011 safety intervention {index} fields differ")
            try:
                intervention = validate_safety_intervention(record["intervention"])
            except Exception as exc:
                raise R011LedgerError(f"R011 safety intervention {index} is invalid") from exc
            if intervention.release_identity_sha256 != identity.release_identity_sha256:
                raise R011IdentityAdmissionError(f"R011 safety intervention {index} identity differs")
        previous = record["record_sha256"]
        observations.append(record)
    return identity, tuple(observations)


class Ledger:
    def __init__(self, path: Path, identity: ReleaseIdentity) -> None:
        self.path = Path(path)
        self.release_identity = identity

    @classmethod
    def create(cls, path: Path, identity: ReleaseIdentity | Mapping[str, Any]) -> "Ledger":
        path = Path(path)
        if path.exists():
            raise R011LedgerError(f"R011 ledger already exists: {path}")
        typed = identity if isinstance(identity, ReleaseIdentity) else validate_release_identity(identity)
        _atomic(path, canonical_bytes(header_record(typed)) + b"\n")
        return cls(path, typed)

    @classmethod
    def load(cls, path: Path, *, expected_release_identity: ReleaseIdentity | Mapping[str, Any] | None = None) -> "Ledger":
        expected = None if expected_release_identity is None else expected_release_identity if isinstance(expected_release_identity, ReleaseIdentity) else validate_release_identity(expected_release_identity)
        identity, _ = _verify(_strict_records(path), expected)
        return cls(path, identity)

    @property
    def observations(self) -> tuple[dict[str, Any], ...]:
        return _verify(_strict_records(self.path), self.release_identity)[1]

    @property
    def typed_observations(self) -> tuple[TypedObservation, ...]:
        rows: list[TypedObservation] = []
        for record in self.observations:
            if record["record_type"] == OBSERVATION_RECORD_TYPE:
                rows.append(validate_observation(record["observation"]))
        return tuple(rows)

    def append_observation(self, observation: TypedObservation) -> dict[str, Any]:
        if not isinstance(observation, (ExactObservation, CensoredObservation)):
            raise R011LedgerError("R011 observation append requires ExactObservation or CensoredObservation")
        if observation.release_identity_sha256 != self.release_identity.release_identity_sha256:
            raise R011IdentityAdmissionError("nested observation release identity differs from ledger")
        records = _strict_records(self.path)
        _verify(records, self.release_identity)
        record = {
            "schema": LEDGER_SCHEMA,
            "record_type": OBSERVATION_RECORD_TYPE,
            "release_identity_sha256": self.release_identity.release_identity_sha256,
            "campaign_fingerprint": self.release_identity.campaign_fingerprint,
            "previous_record_sha256": records[-1]["record_sha256"],
            "observation": observation.as_dict(),
        }
        record["record_sha256"] = _record_hash(record)
        # Append is an explicit persistence operation, so replace the complete
        # sealed byte stream atomically instead of risking a partial JSONL row.
        _atomic(self.path, self.path.read_bytes() + canonical_bytes(record) + b"\n")
        return record

    def append_safety_intervention(self, intervention: SafetyIntervention) -> dict[str, Any]:
        if not isinstance(intervention, SafetyIntervention):
            raise R011LedgerError("safety intervention append requires a typed event")
        if intervention.release_identity_sha256 != self.release_identity.release_identity_sha256:
            raise R011IdentityAdmissionError("safety intervention release identity differs from ledger")
        records = _strict_records(self.path)
        _verify(records, self.release_identity)
        record = {"schema": LEDGER_SCHEMA, "record_type": SAFETY_INTERVENTION_RECORD_TYPE, "release_identity_sha256": self.release_identity.release_identity_sha256, "campaign_fingerprint": self.release_identity.campaign_fingerprint, "previous_record_sha256": records[-1]["record_sha256"], "intervention": intervention.as_dict()}
        record["record_sha256"] = _record_hash(record)
        _atomic(self.path, self.path.read_bytes() + canonical_bytes(record) + b"\n")
        return record

    def append(self, observation: Any) -> dict[str, Any]:
        """Compatibility-shaped entrypoint that intentionally accepts typed rows only."""
        return self.append_observation(observation)


def reject_historical_resume(path: Path) -> None:
    _verify(_strict_records(path), None)


__all__ = [
    "GENESIS_SHA256", "HEADER_RECORD_TYPE", "HistoricalLedgerResumeError", "LEDGER_SCHEMA", "Ledger",
    "OBSERVATION_RECORD_TYPE", "SAFETY_INTERVENTION_RECORD_TYPE", "R011IdentityAdmissionError", "R011LedgerError",
    "header_record", "reject_historical_resume",
]
