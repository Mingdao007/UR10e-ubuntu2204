"""R010 append-only ledger bound to one full release identity."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .identity import (
    ReleaseIdentity,
    canonical_bytes,
    sha256_bytes,
    validate_release_identity,
)


LEDGER_SCHEMA = "step5d.autotune-v4/r010-ledger-v1"
HEADER_RECORD_TYPE = "r010_header"
OBSERVATION_RECORD_TYPE = "r010_observation"
GENESIS_SHA256 = "0" * 64


class R010LedgerError(RuntimeError):
    """A ledger cannot be admitted as R010 state."""


class R010IdentityAdmissionError(R010LedgerError):
    """A ledger identity is absent or differs."""


class HistoricalLedgerResumeError(R010LedgerError):
    """An R008/R009 ledger was presented to the R010 resume seam."""


def _record_hash(record: Mapping[str, Any]) -> str:
    return sha256_bytes(
        canonical_bytes({key: value for key, value in record.items() if key != "record_sha256"})
    )


def header_record(identity: ReleaseIdentity) -> dict[str, Any]:
    if not isinstance(identity, ReleaseIdentity):
        raise R010IdentityAdmissionError("R010 ledger requires a typed release identity")
    record: dict[str, Any] = {
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
        raise R010LedgerError(f"R010 ledger must be a regular file: {path}")
    records: list[dict[str, Any]] = []

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise R010LedgerError(f"R010 ledger repeats key {key!r}")
            result[key] = value
        return result

    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise R010LedgerError(f"R010 ledger contains blank line {index}")
        try:
            value = json.loads(
                line,
                object_pairs_hook=unique_pairs,
                parse_constant=lambda token: (_ for _ in ()).throw(
                    R010LedgerError(f"R010 ledger contains {token}")
                ),
            )
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise R010LedgerError(f"R010 ledger line {index} is invalid") from exc
        if not isinstance(value, dict):
            raise R010LedgerError(f"R010 ledger line {index} is not an object")
        records.append(value)
    if not records:
        raise R010LedgerError("R010 ledger is empty")
    return records


def _atomic_bytes(path: Path, encoded: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise R010LedgerError(f"unsafe R010 ledger path: {path}")
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


def _append_bytes(path: Path, encoded: bytes) -> None:
    try:
        with path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise R010LedgerError(f"R010 ledger append failed: {exc}") from exc


def _verify(
    records: list[dict[str, Any]],
    *,
    expected: ReleaseIdentity | None,
) -> tuple[ReleaseIdentity, tuple[dict[str, Any], ...]]:
    header = records[0]
    observed_schema = str(header.get("schema", ""))
    if observed_schema != LEDGER_SCHEMA or header.get("record_type") != HEADER_RECORD_TYPE:
        if "r008" in observed_schema.lower() or "r009" in observed_schema.lower():
            raise HistoricalLedgerResumeError("R008/R009 ledger is historical-only and cannot resume R010")
        raise R010IdentityAdmissionError("R010 ledger header schema/type differs")
    required_header = {
        "schema",
        "record_type",
        "release_identity",
        "release_identity_sha256",
        "campaign_fingerprint",
        "previous_record_sha256",
        "record_sha256",
    }
    if set(header) != required_header:
        raise R010IdentityAdmissionError("R010 ledger header omits the full release identity")
    identity = validate_release_identity(header["release_identity"])
    if header["release_identity_sha256"] != identity.release_identity_sha256:
        raise R010IdentityAdmissionError("R010 ledger identity digest differs")
    if header["campaign_fingerprint"] != identity.campaign_fingerprint:
        raise R010IdentityAdmissionError("R010 ledger campaign differs")
    if expected is not None and expected.as_dict() != identity.as_dict():
        raise R010IdentityAdmissionError("R010 ledger identity mismatches resume admission")
    if header["previous_record_sha256"] != GENESIS_SHA256 or header["record_sha256"] != _record_hash(header):
        raise R010LedgerError("R010 ledger header hash differs")
    previous = header["record_sha256"]
    observations: list[dict[str, Any]] = []
    for index, record in enumerate(records[1:], 2):
        if record.get("schema") != LEDGER_SCHEMA or record.get("record_type") != OBSERVATION_RECORD_TYPE:
            raise R010LedgerError(f"R010 ledger record {index} type differs")
        if record.get("release_identity_sha256") != identity.release_identity_sha256:
            raise R010IdentityAdmissionError(f"R010 observation {index} identity differs")
        if record.get("campaign_fingerprint") != identity.campaign_fingerprint:
            raise R010IdentityAdmissionError(f"R010 observation {index} campaign differs")
        if record.get("previous_record_sha256") != previous:
            raise R010LedgerError(f"R010 ledger chain breaks before record {index}")
        if record.get("record_sha256") != _record_hash(record):
            raise R010LedgerError(f"R010 observation {index} hash differs")
        previous = record["record_sha256"]
        observations.append(record)
    return identity, tuple(observations)


@dataclass(frozen=True)
class Observation:
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise R010LedgerError("R010 observation payload must be an object")
        reserved = {
            "schema",
            "record_type",
            "release_identity_sha256",
            "campaign_fingerprint",
            "previous_record_sha256",
            "record_sha256",
        }
        if any(key in self.payload for key in reserved):
            raise R010LedgerError("R010 observation payload contains a reserved field")
        object.__setattr__(self, "payload", copy.deepcopy(dict(self.payload)))

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.payload))


class Ledger:
    def __init__(self, path: Path, identity: ReleaseIdentity) -> None:
        self.path = Path(path)
        self.release_identity = identity

    @classmethod
    def create(cls, path: Path, identity: ReleaseIdentity | Mapping[str, Any]) -> "Ledger":
        path = Path(path)
        if path.exists():
            raise R010LedgerError(f"R010 ledger already exists: {path}")
        typed = identity if isinstance(identity, ReleaseIdentity) else validate_release_identity(identity)
        _atomic_bytes(path, canonical_bytes(header_record(typed)) + b"\n")
        return cls(path, typed)

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        expected_release_identity: ReleaseIdentity | Mapping[str, Any] | None = None,
    ) -> "Ledger":
        expected = None
        if expected_release_identity is not None:
            expected = (
                expected_release_identity
                if isinstance(expected_release_identity, ReleaseIdentity)
                else validate_release_identity(expected_release_identity)
            )
        identity, _ = _verify(_strict_records(path), expected=expected)
        return cls(path, identity)

    @property
    def observations(self) -> tuple[dict[str, Any], ...]:
        _, observations = _verify(_strict_records(self.path), expected=self.release_identity)
        return observations

    def append(self, observation: Observation | Mapping[str, Any]) -> dict[str, Any]:
        typed = observation if isinstance(observation, Observation) else Observation(observation)
        records = _strict_records(self.path)
        _verify(records, expected=self.release_identity)
        record: dict[str, Any] = {
            "schema": LEDGER_SCHEMA,
            "record_type": OBSERVATION_RECORD_TYPE,
            "release_identity_sha256": self.release_identity.release_identity_sha256,
            "campaign_fingerprint": self.release_identity.campaign_fingerprint,
            "previous_record_sha256": records[-1]["record_sha256"],
            **typed.as_dict(),
        }
        record["record_sha256"] = _record_hash(record)
        _append_bytes(self.path, canonical_bytes(record) + b"\n")
        return record


def reject_historical_resume(path: Path) -> None:
    """Explicit gate used by launch/resume code and tests."""

    records = _strict_records(path)
    _verify(records, expected=None)


__all__ = [
    "GENESIS_SHA256",
    "HEADER_RECORD_TYPE",
    "HistoricalLedgerResumeError",
    "LEDGER_SCHEMA",
    "Ledger",
    "OBSERVATION_RECORD_TYPE",
    "Observation",
    "R010IdentityAdmissionError",
    "R010LedgerError",
    "header_record",
    "reject_historical_resume",
]
