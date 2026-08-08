"""R009 ledger admission and release-identity binding.

This ledger is intentionally smaller than the V3/R008 observation runtime. It
owns one composition seam only: a typed release identity in the header and the
same identity digest on every observation. Historical R008 files are exposed
through an explicitly named read-only reader and are never adapted into R009
optimizer or resume input.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .identity import (
    R009IdentityError,
    R009ReleaseIdentity,
    canonical_bytes,
    sha256_bytes,
    validate_release_identity,
)


LEDGER_SCHEMA = "step5d.autotune-v4/r009-ledger-v1"
HEADER_RECORD_TYPE = "r009_header"
OBSERVATION_RECORD_TYPE = "r009_observation"
GENESIS_SHA256 = "0" * 64


class R009LedgerError(RuntimeError):
    """A ledger cannot be admitted as an R009 source of truth."""


class R009IdentityAdmissionError(R009LedgerError):
    """A header or observation lacks or mismatches the expected identity."""


class R009HistoricalLineageError(R009LedgerError):
    """Historical R008 data is readable but cannot cross the R009 seam."""


def _strict_object(encoded: bytes, role: str) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise R009LedgerError(f"{role} repeats JSON key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                R009LedgerError(f"{role} contains non-finite JSON constant {token}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise R009LedgerError(f"{role} is not strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise R009LedgerError(f"{role} must be a JSON object")
    return value


def _record_hash(record: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in record.items() if key != "record_sha256"}
    return sha256_bytes(canonical_bytes(payload))


def _read_records(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R009LedgerError(f"R009 ledger must be a regular file: {path}")
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise R009LedgerError(f"R009 ledger is unreadable: {exc}") from exc
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            raise R009LedgerError(f"R009 ledger contains a blank line at {index}")
        records.append(_strict_object(line.encode("utf-8"), f"R009 ledger record {index}"))
    if not records:
        raise R009LedgerError("R009 ledger is empty")
    return records


def _append_bytes(path: Path, encoded: bytes) -> None:
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise R009LedgerError(f"refusing unsafe R009 ledger path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise R009LedgerError(f"R009 ledger append/fsync failed: {exc}") from exc


def _atomic_bytes(path: Path, encoded: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise R009LedgerError(f"refusing unsafe R009 ledger path: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary_path.unlink(missing_ok=True)


def _identity_from_header(header: Mapping[str, Any]) -> R009ReleaseIdentity:
    if "release_identity" not in header or "release_identity_sha256" not in header:
        raise R009IdentityAdmissionError(
            "R009 ledger header omits the full release identity"
        )
    if header.get("schema") != LEDGER_SCHEMA or header.get("record_type") != HEADER_RECORD_TYPE:
        if "r008" in str(header.get("schema", "")).lower():
            raise R009IdentityAdmissionError(
                "historical R008 ledger has no R009 release identity"
            )
        raise R009LedgerError("R009 ledger header schema/type differs")
    required = {
        "schema",
        "record_type",
        "release_identity",
        "release_identity_sha256",
        "campaign_fingerprint",
        "previous_record_sha256",
        "record_sha256",
    }
    if set(header) != required:
        raise R009IdentityAdmissionError(
            "R009 ledger header must store the full release identity"
        )
    try:
        identity = validate_release_identity(header["release_identity"])
        if header["release_identity_sha256"] != identity.release_identity_sha256:
            raise R009IdentityAdmissionError("R009 header release identity digest differs")
        if header["campaign_fingerprint"] != identity.campaign_fingerprint:
            raise R009IdentityAdmissionError("R009 header campaign fingerprint differs")
    except (R009IdentityError, KeyError, TypeError) as exc:
        if isinstance(exc, R009IdentityAdmissionError):
            raise
        raise R009IdentityAdmissionError(f"R009 header identity is invalid: {exc}") from exc
    if header["previous_record_sha256"] != GENESIS_SHA256:
        raise R009LedgerError("R009 header must start at the genesis hash")
    if header["record_sha256"] != _record_hash(header):
        raise R009LedgerError("R009 header record hash differs")
    return identity


def _verify_records(
    records: Iterable[Mapping[str, Any]],
    *,
    expected_release_identity: R009ReleaseIdentity | None = None,
) -> tuple[R009ReleaseIdentity, tuple[dict[str, Any], ...]]:
    rows = [dict(record) for record in records]
    if not rows:
        raise R009LedgerError("R009 ledger is empty")
    actual_identity = _identity_from_header(rows[0])
    if expected_release_identity is not None and actual_identity.as_dict() != expected_release_identity.as_dict():
        raise R009IdentityAdmissionError("R009 ledger header release identity mismatches admission")
    previous = rows[0]["record_sha256"]
    observations: list[dict[str, Any]] = []
    for index, row in enumerate(rows[1:], start=2):
        if row.get("schema") != LEDGER_SCHEMA or row.get("record_type") != OBSERVATION_RECORD_TYPE:
            raise R009LedgerError(f"R009 ledger record {index} is not an observation")
        if "release_identity_sha256" not in row or "campaign_fingerprint" not in row:
            raise R009IdentityAdmissionError(
                f"R009 observation {index} omits release identity binding"
            )
        if row["release_identity_sha256"] != actual_identity.release_identity_sha256:
            raise R009IdentityAdmissionError(
                f"R009 observation {index} release identity mismatches header"
            )
        if row["campaign_fingerprint"] != actual_identity.campaign_fingerprint:
            raise R009IdentityAdmissionError(
                f"R009 observation {index} campaign fingerprint mismatches header"
            )
        if row.get("previous_record_sha256") != previous:
            raise R009LedgerError(f"R009 ledger hash chain breaks before record {index}")
        if row.get("record_sha256") != _record_hash(row):
            raise R009LedgerError(f"R009 observation {index} record hash differs")
        previous = row["record_sha256"]
        observations.append(row)
    return actual_identity, tuple(observations)


@dataclass(frozen=True)
class R009Observation:
    """Small producer/consumer contract for one admitted observation."""

    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.payload, Mapping):
            raise R009LedgerError("R009 observation payload must be a mapping")
        row = copy.deepcopy(dict(self.payload))
        if row.get("record_type") not in (None, OBSERVATION_RECORD_TYPE):
            raise R009LedgerError("R009 observation record_type differs")
        if "release_identity_sha256" not in row or "campaign_fingerprint" not in row:
            raise R009IdentityAdmissionError(
                "R009 observation requires release_identity_sha256 and campaign_fingerprint"
            )
        object.__setattr__(self, "payload", row)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "R009Observation":
        return cls(payload=value)

    @property
    def release_identity_sha256(self) -> str:
        return str(self.payload["release_identity_sha256"])

    @property
    def campaign_fingerprint(self) -> str:
        return str(self.payload["campaign_fingerprint"])

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.payload))


@dataclass(frozen=True)
class HistoricalR008Ledger:
    """Explicitly named, read-only diagnostic view of a historical R008 file."""

    path: Path
    records: tuple[Mapping[str, Any], ...]

    @property
    def rows(self) -> tuple[Mapping[str, Any], ...]:
        return self.records

    def optimizer_input(self) -> tuple[Mapping[str, Any], ...]:
        raise R009HistoricalLineageError(
            "historical R008 ledger is read-only and cannot enter the R009 optimizer flow"
        )

    def resume_input(self) -> tuple[Mapping[str, Any], ...]:
        raise R009HistoricalLineageError(
            "historical R008 ledger is read-only and cannot enter the R009 resume flow"
        )

    def append(self, _observation: Any) -> None:
        raise R009HistoricalLineageError(
            "historical R008 ledger is read-only; append is not an R009 operation"
        )


def load_historical_r008_read_only(path: Path) -> HistoricalR008Ledger:
    """Read R008 bytes for diagnosis only; no R009 adaptation is attempted."""

    path = Path(path)
    records = tuple(dict(record) for record in _read_records(path))
    return HistoricalR008Ledger(path=path, records=records)


# A descriptive alias makes the one-way historical boundary explicit at call
# sites and in audit searches.
read_historical_r008_read_only = load_historical_r008_read_only


class R009Ledger:
    """Append-only R009 ledger admitted against one immutable identity."""

    def __init__(self, path: Path, release_identity: R009ReleaseIdentity) -> None:
        self.path = Path(path)
        if not isinstance(release_identity, R009ReleaseIdentity):
            raise R009IdentityAdmissionError("R009 ledger requires a typed release identity")
        self.release_identity = release_identity
        if self.path.exists() and (self.path.is_symlink() or not self.path.is_file()):
            raise R009LedgerError("R009 ledger path must be a regular file")

    @classmethod
    def create(
        cls,
        path: Path,
        release_identity: R009ReleaseIdentity | Mapping[str, Any],
    ) -> "R009Ledger":
        path = Path(path)
        if path.exists():
            raise R009LedgerError(f"R009 ledger path already exists: {path}")
        identity = (
            release_identity
            if isinstance(release_identity, R009ReleaseIdentity)
            else validate_release_identity(release_identity)
        )
        header: dict[str, Any] = {
            "schema": LEDGER_SCHEMA,
            "record_type": HEADER_RECORD_TYPE,
            "release_identity": identity.as_dict(),
            "release_identity_sha256": identity.release_identity_sha256,
            "campaign_fingerprint": identity.campaign_fingerprint,
            "previous_record_sha256": GENESIS_SHA256,
        }
        header["record_sha256"] = _record_hash(header)
        _atomic_bytes(path, canonical_bytes(header) + b"\n")
        return cls(path, identity)

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        expected_release_identity: R009ReleaseIdentity | Mapping[str, Any] | None = None,
        release_identity: R009ReleaseIdentity | Mapping[str, Any] | None = None,
    ) -> "R009Ledger":
        expected_value = expected_release_identity if expected_release_identity is not None else release_identity
        expected = None
        if expected_value is not None:
            expected = (
                expected_value
                if isinstance(expected_value, R009ReleaseIdentity)
                else validate_release_identity(expected_value)
            )
        try:
            actual, _rows = _verify_records(_read_records(Path(path)), expected_release_identity=expected)
        except R009IdentityAdmissionError:
            raise
        except (R009LedgerError, R009IdentityError) as exc:
            raise R009LedgerError(str(exc)) from exc
        return cls(Path(path), actual)

    @property
    def header(self) -> Mapping[str, Any]:
        records = _read_records(self.path)
        _verify_records(records, expected_release_identity=self.release_identity)
        return copy.deepcopy(records[0])

    @property
    def observations(self) -> tuple[Mapping[str, Any], ...]:
        _identity, rows = _verify_records(
            _read_records(self.path), expected_release_identity=self.release_identity
        )
        return rows

    def append(self, observation: R009Observation | Mapping[str, Any]) -> Mapping[str, Any]:
        # Load and verify before opening the file. A missing/mismatched identity
        # therefore cannot create even a partial append.
        _identity, rows = _verify_records(
            _read_records(self.path), expected_release_identity=self.release_identity
        )
        item = observation if isinstance(observation, R009Observation) else R009Observation.from_mapping(observation)
        row = item.as_dict()
        if row.get("release_identity_sha256") != self.release_identity.release_identity_sha256:
            raise R009IdentityAdmissionError("R009 observation release identity mismatches ledger")
        if row.get("campaign_fingerprint") != self.release_identity.campaign_fingerprint:
            raise R009IdentityAdmissionError("R009 observation campaign fingerprint mismatches ledger")
        if row.get("schema") not in (None, LEDGER_SCHEMA):
            raise R009LedgerError("R009 observation schema differs")
        if row.get("record_type") not in (None, OBSERVATION_RECORD_TYPE):
            raise R009LedgerError("R009 observation record_type differs")
        for field in ("previous_record_sha256", "record_sha256"):
            row.pop(field, None)
        row["schema"] = LEDGER_SCHEMA
        row["record_type"] = OBSERVATION_RECORD_TYPE
        row["previous_record_sha256"] = (
            _read_records(self.path)[-1]["record_sha256"]
        )
        row["record_sha256"] = _record_hash(row)
        _append_bytes(self.path, canonical_bytes(row) + b"\n")
        # Cold read after fsync is the smallest useful durability seam.
        _verify_records(_read_records(self.path), expected_release_identity=self.release_identity)
        return row

    def optimizer_input(self) -> tuple[Mapping[str, Any], ...]:
        """The only optimizer-facing entry point; it rechecks admission."""

        return self.observations

    def resume_input(self) -> tuple[Mapping[str, Any], ...]:
        """The only resume-facing entry point; it rechecks admission."""

        return self.observations


R009ObservationLedger = R009Ledger
load_r009_ledger = R009Ledger.load
new_r009_ledger = R009Ledger.create


__all__ = [
    "GENESIS_SHA256",
    "HEADER_RECORD_TYPE",
    "HistoricalR008Ledger",
    "LEDGER_SCHEMA",
    "OBSERVATION_RECORD_TYPE",
    "R009HistoricalLineageError",
    "R009IdentityAdmissionError",
    "R009Ledger",
    "R009LedgerError",
    "R009Observation",
    "R009ObservationLedger",
    "load_historical_r008_read_only",
    "load_r009_ledger",
    "new_r009_ledger",
    "read_historical_r008_read_only",
]
