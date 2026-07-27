"""Exact ten-row Step5d batch identity and crash-safe offline lifecycle."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .candidate_identity import (
    ControlCandidateUid,
    OccurrenceUid,
    TransportCandidateUid,
)
from .contracts import OutputPathError, SpecValidationError
from .identity import canonical_json_bytes, canonical_sha256, strict_json_loads
from .stage_adapters import (
    CONTROL_CANDIDATE_FIELDS,
    LEGACY_CONTROL_CANDIDATE_FIELDS,
    normalize_trial_overlay,
)


_ZERO_SHA256 = "0" * 64


class BatchFate(str, Enum):
    UNATTEMPTED = "unattempted"
    ATTEMPTED_INCOMPLETE = "attempted_incomplete"
    ACK_COMPLETED = "ack_completed"
    DIRECT_COMPLETED = "direct_completed"


class ReturnReferenceKind(str, Enum):
    NEAR_READY = "near_ready"
    CAMPAIGN_HOME = "campaign_home"


@dataclass(frozen=True)
class ExactAckReceipt:
    batch_uid: str
    row_index: int
    trial_uid: str
    control_candidate_uid: str
    immutable_bundle_sha256: str
    return_reference_uid: str
    controller_readback_sha256: str
    arm_command_seq: int
    ack_command_seq: int
    consumed_command_seq: int
    controller_readback_path: str | None = None

    def __post_init__(self) -> None:
        _sha256("batch_uid", self.batch_uid)
        _sha256("trial_uid", self.trial_uid)
        _typed_uid(
            "control_candidate_uid", self.control_candidate_uid, ControlCandidateUid
        )
        _sha256("immutable_bundle_sha256", self.immutable_bundle_sha256)
        _sha256("return_reference_uid", self.return_reference_uid)
        _sha256("controller_readback_sha256", self.controller_readback_sha256)
        if (
            isinstance(self.row_index, bool)
            or not isinstance(self.row_index, int)
            or not 1 <= self.row_index <= 10
        ):
            raise SpecValidationError("ACK row index must be in [1,10]")
        for name in ("arm_command_seq", "ack_command_seq", "consumed_command_seq"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SpecValidationError(f"{name} must be a positive integer")
        if self.ack_command_seq <= self.arm_command_seq:
            raise SpecValidationError("ACK sequence must be newer than ARM")
        if self.consumed_command_seq != self.ack_command_seq:
            raise SpecValidationError("ACK receipt must prove exact sequence consumption")
        if self.controller_readback_path is not None:
            path = PurePosixPath(self.controller_readback_path)
            if (
                not self.controller_readback_path
                or path.is_absolute()
                or path.as_posix() != self.controller_readback_path
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise SpecValidationError(
                    "controller_readback_path must be a normalized relative POSIX path"
                )

    def document(self) -> dict[str, Any]:
        document = {
            "schema": (
                "ur10e.exact_ack_receipt/v2"
                if self.controller_readback_path is not None
                else "ur10e.exact_ack_receipt/v1"
            ),
            "batch_uid": self.batch_uid,
            "row_index": self.row_index,
            "trial_uid": self.trial_uid,
            "control_candidate_uid": self.control_candidate_uid,
            "immutable_bundle_sha256": self.immutable_bundle_sha256,
            "return_reference_uid": self.return_reference_uid,
            "controller_readback_sha256": self.controller_readback_sha256,
            "arm_command_seq": self.arm_command_seq,
            "ack_command_seq": self.ack_command_seq,
            "consumed_command_seq": self.consumed_command_seq,
        }
        if self.controller_readback_path is not None:
            document["controller_readback_path"] = self.controller_readback_path
        return document

    @property
    def ack_uid(self) -> str:
        return canonical_sha256(self.document())


@dataclass(frozen=True)
class DirectReadyReceipt:
    """Durable r006 terminal-ready proof without a TP ACK command."""

    batch_uid: str
    row_index: int
    trial_uid: str
    control_candidate_uid: str
    immutable_bundle_sha256: str
    return_reference_uid: str
    controller_readback_sha256: str
    arm_command_seq: int
    consumed_command_seq: int
    tp_state: str
    controller_readback_path: str
    protocol: str = "v3_direct_arm_v1"
    logical_batch_sequence: int = 0
    occurrence_uid: str | None = None
    transport_candidate_uid: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "batch_uid",
            "trial_uid",
            "control_candidate_uid",
            "immutable_bundle_sha256",
            "return_reference_uid",
            "controller_readback_sha256",
        ):
            if name == "control_candidate_uid":
                _typed_uid(name, getattr(self, name), ControlCandidateUid)
            else:
                _sha256(name, getattr(self, name))
        rolling = self.protocol == "v3_full_home_rolling_arm_v1"
        if self.protocol not in {"v3_direct_arm_v1", "v3_full_home_rolling_arm_v1"}:
            raise SpecValidationError("direct-ready protocol is unsupported")
        if rolling and self.logical_batch_sequence < 1:
            raise SpecValidationError("rolling direct-ready receipt lacks batch identity")
        if rolling:
            _typed_uid("occurrence_uid", self.occurrence_uid, OccurrenceUid)
            _typed_uid(
                "transport_candidate_uid",
                self.transport_candidate_uid,
                TransportCandidateUid,
            )
        elif self.occurrence_uid is not None or self.transport_candidate_uid is not None:
            raise SpecValidationError("direct-arm receipt cannot claim rolling UID namespaces")
        expected_reference = (
            ReturnReferenceKind.CAMPAIGN_HOME
            if rolling
            else return_reference_for_row(self.row_index)
        )
        expected_state = "READY_HOME_NEXT" if rolling else (
            "READY_HOME_CLOSED"
            if expected_reference is ReturnReferenceKind.CAMPAIGN_HOME
            else "READY_NEAR"
        )
        if self.tp_state != expected_state:
            raise SpecValidationError("direct-ready TP state differs from batch row")
        for name in ("arm_command_seq", "consumed_command_seq"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SpecValidationError(f"{name} must be a positive integer")
        if self.consumed_command_seq != self.arm_command_seq:
            raise SpecValidationError(
                "direct-ready receipt must retain exact ARM consumption"
            )
        path = PurePosixPath(self.controller_readback_path)
        if (
            not self.controller_readback_path
            or path.is_absolute()
            or path.as_posix() != self.controller_readback_path
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise SpecValidationError(
                "controller_readback_path must be a normalized relative POSIX path"
            )

    def document(self) -> dict[str, Any]:
        document = {
            "schema": "ur10e.direct_ready_receipt/v2" if self.protocol == "v3_full_home_rolling_arm_v1" else "ur10e.direct_ready_receipt/v1",
            "batch_uid": self.batch_uid,
            "row_index": self.row_index,
            "trial_uid": self.trial_uid,
            "control_candidate_uid": self.control_candidate_uid,
            "immutable_bundle_sha256": self.immutable_bundle_sha256,
            "return_reference_uid": self.return_reference_uid,
            "controller_readback_sha256": self.controller_readback_sha256,
            "arm_command_seq": self.arm_command_seq,
            "consumed_command_seq": self.consumed_command_seq,
            "tp_state": self.tp_state,
            "controller_readback_path": self.controller_readback_path,
        }
        if self.protocol == "v3_full_home_rolling_arm_v1":
            document["protocol"] = self.protocol
            document["logical_batch_sequence"] = self.logical_batch_sequence
            document["occurrence_uid"] = self.occurrence_uid
            document["transport_candidate_uid"] = self.transport_candidate_uid
        return document

    @property
    def completion_uid(self) -> str:
        return canonical_sha256(self.document())


@dataclass(frozen=True)
class SafeClosureReceipt:
    batch_uid: str
    row_index: int
    trial_uid: str
    ack_uid: str
    return_reference_uid: str
    controller_readback_sha256: str
    return_reference: ReturnReferenceKind
    post_ack_verified: bool = True

    def __post_init__(self) -> None:
        _sha256("batch_uid", self.batch_uid)
        _sha256("trial_uid", self.trial_uid)
        _sha256("ack_uid", self.ack_uid)
        _sha256("return_reference_uid", self.return_reference_uid)
        _sha256("controller_readback_sha256", self.controller_readback_sha256)
        if self.return_reference is not return_reference_for_row(self.row_index):
            raise SpecValidationError("closure reference differs from exact batch row")
        if self.post_ack_verified is not True:
            raise SpecValidationError("safe closure receipt must be post-ACK verified")

    def document(self) -> dict[str, Any]:
        return {
            "schema": "ur10e.safe_closure_receipt/v1",
            "batch_uid": self.batch_uid,
            "row_index": self.row_index,
            "trial_uid": self.trial_uid,
            "ack_uid": self.ack_uid,
            "return_reference_uid": self.return_reference_uid,
            "controller_readback_sha256": self.controller_readback_sha256,
            "return_reference": self.return_reference.value,
            "post_ack_verified": self.post_ack_verified,
        }

    @property
    def receipt_sha256(self) -> str:
        return canonical_sha256(self.document())


def _sha256(name: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SpecValidationError(f"{name} must be a lowercase SHA256")
    return value


def _typed_uid(
    name: str,
    value: Any,
    uid_type: type[ControlCandidateUid]
    | type[OccurrenceUid]
    | type[TransportCandidateUid],
) -> str:
    """Validate active domain IDs while retaining explicit historical decoding."""

    try:
        return str(uid_type.parse(value, allow_legacy=True))
    except (TypeError, ValueError) as exc:
        raise SpecValidationError(f"{name} is not a valid {uid_type.__name__}") from exc


@dataclass(frozen=True)
class BatchRow:
    row_index: int
    control_candidate: Mapping[str, Any]
    trial_overlay: Mapping[str, Any]
    occurrence_uid: str | None = None
    transport_candidate_uid: str | None = None
    role: str | None = None
    replicate_ordinal: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.row_index, bool) or not isinstance(self.row_index, int):
            raise SpecValidationError("batch row index must be an integer")
        if self.row_index < 1 or self.row_index > 10:
            raise SpecValidationError("batch row index must be in [1,10]")
        overlay = normalize_trial_overlay(self.trial_overlay)
        candidate_fields = (
            CONTROL_CANDIDATE_FIELDS
            if "normal_filter_tau_s" in overlay
            else LEGACY_CONTROL_CANDIDATE_FIELDS
        )
        if not isinstance(self.control_candidate, Mapping) or set(
            self.control_candidate
        ) != set(candidate_fields):
            raise SpecValidationError("control candidate fields differ")
        candidate = {
            name: float(self.control_candidate[name])
            for name in candidate_fields
        }
        if any(candidate[name] != overlay[name] for name in candidate_fields):
            raise SpecValidationError("control candidate differs from actual trial overlay")
        object.__setattr__(
            self,
            "control_candidate",
            strict_json_loads(canonical_json_bytes(candidate)),
        )
        object.__setattr__(self, "trial_overlay", overlay)
        optional = (self.occurrence_uid, self.transport_candidate_uid)
        if any(value is not None for value in optional):
            if not all(value is not None for value in optional):
                raise SpecValidationError("rolling row occurrence identity is incomplete")
            _typed_uid("occurrence_uid", self.occurrence_uid, OccurrenceUid)
            _typed_uid(
                "transport_candidate_uid",
                self.transport_candidate_uid,
                TransportCandidateUid,
            )
            if not isinstance(self.role, str) or not self.role:
                raise SpecValidationError("rolling row role is missing")
            if (
                isinstance(self.replicate_ordinal, bool)
                or not isinstance(self.replicate_ordinal, int)
                or self.replicate_ordinal < 1
            ):
                raise SpecValidationError("rolling replicate ordinal must be positive")

    @property
    def control_candidate_uid(self) -> str:
        return str(self.trial_overlay["control_candidate_uid"])

    def to_dict(self) -> dict[str, Any]:
        document = {
            "row_index": self.row_index,
            "control_candidate_uid": self.control_candidate_uid,
            "control_candidate": dict(self.control_candidate),
            "trial_overlay": dict(self.trial_overlay),
        }
        if self.occurrence_uid is not None:
            document.update(
                occurrence_uid=self.occurrence_uid,
                transport_candidate_uid=self.transport_candidate_uid,
                role=self.role,
                replicate_ordinal=self.replicate_ordinal,
            )
        return document


@dataclass(frozen=True)
class BatchIdentity:
    campaign_uid: str
    experiment_fingerprint: str
    launch_fingerprint: str
    adapter_fingerprint: str
    physical_prior_fingerprint: str
    safety_envelope_fingerprint: str
    return_policy_fingerprint: str
    controller_readback_fingerprint: str
    authorization_ref_sha256: str
    plant_epoch: int
    rows: tuple[BatchRow, ...]
    protocol: str = "legacy_ack_bundle_v1"
    logical_batch_sequence: int = 0
    plan_revision: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.campaign_uid, str) or not self.campaign_uid.strip():
            raise SpecValidationError("campaign_uid must be a non-empty string")
        _sha256("experiment_fingerprint", self.experiment_fingerprint)
        _sha256("launch_fingerprint", self.launch_fingerprint)
        for name in (
            "adapter_fingerprint",
            "physical_prior_fingerprint",
            "safety_envelope_fingerprint",
            "return_policy_fingerprint",
            "controller_readback_fingerprint",
            "authorization_ref_sha256",
        ):
            _sha256(name, getattr(self, name))
        if (
            isinstance(self.plant_epoch, bool)
            or not isinstance(self.plant_epoch, int)
            or self.plant_epoch < 1
        ):
            raise SpecValidationError("plant_epoch must be a positive integer")
        rows = tuple(self.rows)
        rolling = self.protocol == "v3_full_home_rolling_arm_v1"
        if self.protocol not in {"legacy_ack_bundle_v1", "v3_direct_arm_v1", "v3_full_home_rolling_arm_v1"}:
            raise SpecValidationError("BatchIdentity protocol is unsupported")
        expected_count = 5 if rolling else 10
        if len(rows) != expected_count or tuple(row.row_index for row in rows) != tuple(
            range(1, expected_count + 1)
        ):
            raise SpecValidationError(
                f"BatchIdentity requires exact ordered rows 1..{expected_count}"
            )
        uids = tuple(row.control_candidate_uid for row in rows)
        if not rolling and len(set(uids)) != expected_count:
            raise SpecValidationError("BatchIdentity control candidates must be unique")
        if rolling:
            if self.logical_batch_sequence < 1 or self.plan_revision < 1:
                raise SpecValidationError("rolling BatchIdentity lacks plan generation")
            occurrences = tuple(row.occurrence_uid for row in rows)
            if None in occurrences or len(set(occurrences)) != expected_count:
                raise SpecValidationError("rolling BatchIdentity occurrences must be unique")
        object.__setattr__(self, "rows", rows)

    @property
    def batch_uid(self) -> str:
        return canonical_sha256(self.identity_document())

    def identity_document(self) -> dict[str, Any]:
        document = {
            "schema": "ur10e.batch_identity/v3" if self.protocol == "v3_full_home_rolling_arm_v1" else "ur10e.batch_identity/v2",
            "campaign_uid": self.campaign_uid,
            "experiment_fingerprint": self.experiment_fingerprint,
            "launch_fingerprint": self.launch_fingerprint,
            "adapter_fingerprint": self.adapter_fingerprint,
            "physical_prior_fingerprint": self.physical_prior_fingerprint,
            "safety_envelope_fingerprint": self.safety_envelope_fingerprint,
            "return_policy_fingerprint": self.return_policy_fingerprint,
            "controller_readback_fingerprint": self.controller_readback_fingerprint,
            "authorization_ref_sha256": self.authorization_ref_sha256,
            "plant_epoch": self.plant_epoch,
            "rows": [row.to_dict() for row in self.rows],
        }
        if self.protocol == "v3_full_home_rolling_arm_v1":
            document.update(
                protocol=self.protocol,
                logical_batch_sequence=self.logical_batch_sequence,
                plan_revision=self.plan_revision,
            )
        return document

    def document(self) -> dict[str, Any]:
        return {**self.identity_document(), "batch_uid": self.batch_uid}

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "BatchIdentity":
        if not isinstance(document, Mapping):
            raise SpecValidationError("BatchIdentity document fields differ")
        schema = document.get("schema")
        base_fields = {
            "schema",
            "campaign_uid",
            "experiment_fingerprint",
            "launch_fingerprint",
            "adapter_fingerprint",
            "physical_prior_fingerprint",
            "safety_envelope_fingerprint",
            "return_policy_fingerprint",
            "controller_readback_fingerprint",
            "authorization_ref_sha256",
            "plant_epoch",
            "rows",
            "batch_uid",
        }
        rolling_fields = {"protocol", "logical_batch_sequence", "plan_revision"}
        expected_fields = base_fields | (rolling_fields if schema == "ur10e.batch_identity/v3" else set())
        if set(document) != expected_fields:
            raise SpecValidationError("BatchIdentity document fields differ")
        if schema not in {"ur10e.batch_identity/v2", "ur10e.batch_identity/v3"}:
            raise SpecValidationError("BatchIdentity schema differs")
        raw_rows = document["rows"]
        if not isinstance(raw_rows, list):
            raise SpecValidationError("BatchIdentity rows must be an array")
        rows = []
        for raw in raw_rows:
            legacy_row_fields = {
                "row_index",
                "control_candidate_uid",
                "control_candidate",
                "trial_overlay",
            }
            rolling_row_fields = legacy_row_fields | {
                "occurrence_uid",
                "transport_candidate_uid",
                "role",
                "replicate_ordinal",
            }
            if not isinstance(raw, Mapping) or set(raw) != (
                rolling_row_fields if schema == "ur10e.batch_identity/v3" else legacy_row_fields
            ):
                raise SpecValidationError("BatchIdentity row fields differ")
            row = BatchRow(
                row_index=raw["row_index"],
                control_candidate=raw["control_candidate"],
                trial_overlay=raw["trial_overlay"],
                occurrence_uid=raw.get("occurrence_uid"),
                transport_candidate_uid=raw.get("transport_candidate_uid"),
                role=raw.get("role"),
                replicate_ordinal=raw.get("replicate_ordinal"),
            )
            if raw["control_candidate_uid"] != row.control_candidate_uid:
                raise SpecValidationError("BatchIdentity row UID differs")
            rows.append(row)
        identity = cls(
            campaign_uid=document["campaign_uid"],
            experiment_fingerprint=document["experiment_fingerprint"],
            launch_fingerprint=document["launch_fingerprint"],
            adapter_fingerprint=document["adapter_fingerprint"],
            physical_prior_fingerprint=document["physical_prior_fingerprint"],
            safety_envelope_fingerprint=document["safety_envelope_fingerprint"],
            return_policy_fingerprint=document["return_policy_fingerprint"],
            controller_readback_fingerprint=document[
                "controller_readback_fingerprint"
            ],
            authorization_ref_sha256=document["authorization_ref_sha256"],
            plant_epoch=document["plant_epoch"],
            rows=tuple(rows),
            protocol=document.get("protocol", "legacy_ack_bundle_v1"),
            logical_batch_sequence=document.get("logical_batch_sequence", 0),
            plan_revision=document.get("plan_revision", 0),
        )
        if document["batch_uid"] != identity.batch_uid:
            raise SpecValidationError("BatchIdentity digest differs")
        return identity


@dataclass(frozen=True)
class BatchRowState:
    row_index: int
    fate: BatchFate
    trial_uid: str | None
    immutable_bundle_sha256: str | None
    ack_uid: str | None
    ack_receipt_sha256: str | None
    ack_receipt_document: Mapping[str, Any] | None
    direct_ready_uid: str | None
    direct_ready_receipt_sha256: str | None
    direct_ready_receipt_document: Mapping[str, Any] | None
    return_reference_uid: str | None
    controller_readback_sha256: str | None
    closure_receipt_sha256: str | None
    closure_receipt_document: Mapping[str, Any] | None
    trial_brief_publication_uid: str | None
    trial_brief_document_sha256: str | None
    optimizer_eligible: bool | None
    return_reference: ReturnReferenceKind

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_index": self.row_index,
            "fate": self.fate.value,
            "trial_uid": self.trial_uid,
            "immutable_bundle_sha256": self.immutable_bundle_sha256,
            "ack_uid": self.ack_uid,
            "ack_receipt_sha256": self.ack_receipt_sha256,
            "ack_receipt_document": self.ack_receipt_document,
            "direct_ready_uid": self.direct_ready_uid,
            "direct_ready_receipt_sha256": self.direct_ready_receipt_sha256,
            "direct_ready_receipt_document": self.direct_ready_receipt_document,
            "return_reference_uid": self.return_reference_uid,
            "controller_readback_sha256": self.controller_readback_sha256,
            "closure_receipt_sha256": self.closure_receipt_sha256,
            "closure_receipt_document": self.closure_receipt_document,
            "trial_brief_publication_uid": self.trial_brief_publication_uid,
            "trial_brief_document_sha256": self.trial_brief_document_sha256,
            "optimizer_eligible": self.optimizer_eligible,
            "return_reference": self.return_reference.value,
        }


@dataclass(frozen=True)
class BatchState:
    batch_uid: str
    rows: tuple[BatchRowState, ...]
    journal_record_count: int
    journal_head_sha256: str
    result_published: bool

    @property
    def resume_row_indices(self) -> tuple[int, ...]:
        return tuple(
            row.row_index
            for row in self.rows
            if row.fate
            not in {BatchFate.ACK_COMPLETED, BatchFate.DIRECT_COMPLETED}
        )

    @property
    def next_row_index(self) -> int | None:
        rows = self.resume_row_indices
        return rows[0] if rows else None

    @property
    def unpublished_trial_brief_row_indices(self) -> tuple[int, ...]:
        return tuple(
            row.row_index
            for row in self.rows
            if row.fate in {BatchFate.ACK_COMPLETED, BatchFate.DIRECT_COMPLETED}
            and row.trial_brief_document_sha256 is None
        )

    @property
    def complete(self) -> bool:
        return not self.resume_row_indices

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "ur10e.batch_state/v1",
            "batch_uid": self.batch_uid,
            "rows": [row.to_dict() for row in self.rows],
            "resume_row_indices": list(self.resume_row_indices),
            "next_row_index": self.next_row_index,
            "complete": self.complete,
            "unpublished_trial_brief_row_indices": list(
                self.unpublished_trial_brief_row_indices
            ),
            "journal_record_count": self.journal_record_count,
            "journal_head_sha256": self.journal_head_sha256,
            "result_published": self.result_published,
        }


def return_reference_for_row(row_index: int) -> ReturnReferenceKind:
    if (
        isinstance(row_index, bool)
        or not isinstance(row_index, int)
        or row_index < 1
        or row_index > 10
    ):
        raise SpecValidationError("batch row index must be in [1,10]")
    return (
        ReturnReferenceKind.CAMPAIGN_HOME
        if row_index == 10
        else ReturnReferenceKind.NEAR_READY
    )


def return_reference_for_batch(
    batch: BatchIdentity, row_index: int
) -> ReturnReferenceKind:
    if row_index < 1 or row_index > len(batch.rows):
        raise SpecValidationError("batch row index is outside this identity")
    if batch.protocol == "v3_full_home_rolling_arm_v1":
        return ReturnReferenceKind.CAMPAIGN_HOME
    return return_reference_for_row(row_index)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exclusive(path: Path, document: Mapping[str, Any]) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(canonical_json_bytes(document) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _read_regular_bytes(path: Path, role: str) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OutputPathError(f"{role} must be a no-follow regular file")
            chunks = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except OutputPathError:
        raise
    except OSError as exc:
        raise OutputPathError(f"cannot read {role}: {exc}") from exc


def _read_regular_json(path: Path, role: str) -> Any:
    return strict_json_loads(_read_regular_bytes(path, role))


class BatchJournal:
    """One append-only batch lifecycle store; no robot or process side effects."""

    def __init__(self, root: Path):
        self.root = root
        self.identity_path = root / "batch_identity.json"
        self.journal_path = root / "batch_state.jsonl"
        self.result_path = root / "batch_result.json"
        self.lock_path = root / ".batch.lock"

    @classmethod
    def create(cls, root: Path, identity: BatchIdentity) -> "BatchJournal":
        if not isinstance(root, Path) or not root.is_absolute() or root.is_symlink():
            raise OutputPathError("batch root must be an absolute non-symlink path")
        root.mkdir(parents=True, exist_ok=False, mode=0o700)
        _fsync_directory(root.parent)
        journal = cls(root)
        _write_exclusive(journal.identity_path, identity.document())
        return journal

    @classmethod
    def open(cls, root: Path) -> "BatchJournal":
        if not isinstance(root, Path) or not root.is_absolute() or root.is_symlink():
            raise OutputPathError("batch root must be an absolute non-symlink path")
        if not root.is_dir():
            raise OutputPathError("batch root does not exist")
        journal = cls(root)
        journal.identity()
        journal.state()
        return journal

    def identity(self) -> BatchIdentity:
        document = _read_regular_json(self.identity_path, "batch identity")
        if not isinstance(document, Mapping):
            raise SpecValidationError("batch identity must be an object")
        return BatchIdentity.from_document(document)

    def _lock(self) -> int:
        descriptor = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise OutputPathError("batch lock must be a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor

    def _records(self) -> tuple[dict[str, Any], ...]:
        if not self.journal_path.exists():
            return ()
        if self.journal_path.is_symlink() or not self.journal_path.is_file():
            raise OutputPathError("batch journal must be a regular file")
        records = []
        previous = _ZERO_SHA256
        for line_number, line in enumerate(
            _read_regular_bytes(self.journal_path, "batch journal").splitlines(),
            start=1,
        ):
            record = strict_json_loads(line)
            if not isinstance(record, dict) or set(record) != {
                "schema",
                "sequence",
                "previous_sha256",
                "event",
                "record_sha256",
            }:
                raise OutputPathError(f"batch journal fields differ at line {line_number}")
            if (
                record["schema"] != "ur10e.batch_state_record/v1"
                or record["sequence"] != line_number - 1
                or record["previous_sha256"] != previous
                or not isinstance(record["event"], dict)
            ):
                raise OutputPathError(f"batch journal chain differs at line {line_number}")
            body = {key: value for key, value in record.items() if key != "record_sha256"}
            digest = canonical_sha256(body)
            if record["record_sha256"] != digest:
                raise OutputPathError(f"batch journal hash differs at line {line_number}")
            previous = digest
            records.append(record)
        return tuple(records)

    def _append(
        self,
        event: Mapping[str, Any],
        *,
        validator: Callable[[BatchState], None] | None = None,
    ) -> None:
        descriptor = self._lock()
        try:
            if self.result_path.exists():
                raise OutputPathError("batch result is already final")
            if validator is not None:
                validator(self.state())
            records = self._records()
            body = {
                "schema": "ur10e.batch_state_record/v1",
                "sequence": len(records),
                "previous_sha256": (
                    records[-1]["record_sha256"] if records else _ZERO_SHA256
                ),
                "event": dict(event),
            }
            record = {**body, "record_sha256": canonical_sha256(body)}
            fd = os.open(
                self.journal_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_APPEND
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                with os.fdopen(fd, "ab", closefd=False) as stream:
                    stream.write(canonical_json_bytes(record) + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(fd)
            _fsync_directory(self.root)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def state(self) -> BatchState:
        identity = self.identity()
        records = self._records()
        mutable = [
            {
                "trial_uid": None,
                "bundle": None,
                "ack_uid": None,
                "ack_receipt": None,
                "ack_receipt_document": None,
                "direct_ready_uid": None,
                "direct_ready_receipt": None,
                "direct_ready_receipt_document": None,
                "return_reference_uid": None,
                "controller_readback_sha256": None,
                "closure_receipt": None,
                "closure_receipt_document": None,
                "trial_brief_publication_uid": None,
                "trial_brief_document_sha256": None,
                "optimizer_eligible": None,
            }
            for _ in identity.rows
        ]
        for record in records:
            event = record["event"]
            if event.get("batch_uid") != identity.batch_uid:
                raise OutputPathError("batch journal event identity differs")
            row_index = event.get("row_index")
            if isinstance(row_index, bool) or not isinstance(row_index, int) or not 1 <= row_index <= len(identity.rows):
                raise OutputPathError("batch journal row index differs")
            state = mutable[row_index - 1]
            kind = event.get("kind")
            trial_uid = _sha256("trial_uid", event.get("trial_uid"))
            if kind == "attempt_started":
                if any(
                    prior["closure_receipt"] is None for prior in mutable[: row_index - 1]
                ):
                    raise OutputPathError("batch journal skipped an incomplete prior row")
                if state["closure_receipt"] is not None:
                    raise OutputPathError("batch journal reattempted a completed row")
                if state["bundle"] is not None or state["ack_receipt"] is not None:
                    raise OutputPathError(
                        "batch journal reattempted a durable post-execution row"
                    )
                state.update(
                    trial_uid=trial_uid,
                    bundle=None,
                    ack_uid=None,
                    ack_receipt=None,
                    ack_receipt_document=None,
                    direct_ready_uid=None,
                    direct_ready_receipt=None,
                    direct_ready_receipt_document=None,
                    return_reference_uid=None,
                    controller_readback_sha256=None,
                    closure_receipt=None,
                    closure_receipt_document=None,
                    trial_brief_publication_uid=None,
                    trial_brief_document_sha256=None,
                    optimizer_eligible=None,
                )
            elif state["trial_uid"] != trial_uid:
                raise OutputPathError("batch journal trial identity differs")
            elif kind == "bundle_written":
                state["bundle"] = _sha256(
                    "immutable_bundle_sha256", event.get("immutable_bundle_sha256")
                )
            elif kind == "ack_consumed":
                if state["bundle"] is None:
                    raise OutputPathError("batch ACK precedes immutable bundle")
                state["ack_uid"] = _sha256("ack_uid", event.get("ack_uid"))
                state["ack_receipt"] = _sha256(
                    "ack_receipt_sha256", event.get("ack_receipt_sha256")
                )
                document = event.get("ack_receipt_document")
                if document is not None:
                    if not isinstance(document, Mapping):
                        raise OutputPathError("batch ACK receipt document is invalid")
                    candidate_document = dict(document)
                    schema = candidate_document.pop("schema", None)
                    if schema not in {
                        "ur10e.exact_ack_receipt/v1",
                        "ur10e.exact_ack_receipt/v2",
                    }:
                        raise OutputPathError("batch ACK receipt schema differs")
                    if schema == "ur10e.exact_ack_receipt/v1":
                        candidate_document["controller_readback_path"] = None
                    elif "controller_readback_path" not in candidate_document:
                        raise OutputPathError("batch ACK receipt path is missing")
                    receipt = ExactAckReceipt(**candidate_document)
                    if (
                        receipt.ack_uid != state["ack_uid"]
                        or canonical_sha256(receipt.document())
                        != state["ack_receipt"]
                    ):
                        raise OutputPathError("batch ACK receipt document differs")
                    state["ack_receipt_document"] = receipt.document()
                state["return_reference_uid"] = _sha256(
                    "return_reference_uid", event.get("return_reference_uid")
                )
                state["controller_readback_sha256"] = _sha256(
                    "controller_readback_sha256",
                    event.get("controller_readback_sha256"),
                )
            elif kind == "direct_ready_completed":
                if state["bundle"] is None or state["ack_receipt"] is not None:
                    raise OutputPathError(
                        "batch direct-ready completion requires bundle and no ACK"
                    )
                document = event.get("direct_ready_receipt_document")
                if not isinstance(document, Mapping):
                    raise OutputPathError("batch direct-ready receipt is invalid")
                candidate_document = dict(document)
                schema = candidate_document.pop("schema", None)
                if schema not in {
                    "ur10e.direct_ready_receipt/v1",
                    "ur10e.direct_ready_receipt/v2",
                }:
                    raise OutputPathError("batch direct-ready receipt schema differs")
                if schema == "ur10e.direct_ready_receipt/v1":
                    candidate_document.setdefault("protocol", "v3_direct_arm_v1")
                    candidate_document.setdefault("logical_batch_sequence", 0)
                try:
                    receipt = DirectReadyReceipt(**candidate_document)
                except (KeyError, TypeError, ValueError) as exc:
                    raise OutputPathError(
                        "batch direct-ready receipt document differs"
                    ) from exc
                receipt_sha = canonical_sha256(receipt.document())
                if any(
                    (
                        receipt.batch_uid != identity.batch_uid,
                        receipt.row_index != row_index,
                        receipt.trial_uid != trial_uid,
                        receipt.immutable_bundle_sha256 != state["bundle"],
                        event.get("direct_ready_uid") != receipt.completion_uid,
                        event.get("direct_ready_receipt_sha256") != receipt_sha,
                    )
                ):
                    raise OutputPathError("batch direct-ready identity differs")
                state["direct_ready_uid"] = receipt.completion_uid
                state["direct_ready_receipt"] = receipt_sha
                state["direct_ready_receipt_document"] = receipt.document()
                state["return_reference_uid"] = receipt.return_reference_uid
                state["controller_readback_sha256"] = (
                    receipt.controller_readback_sha256
                )
                state["closure_receipt"] = receipt.completion_uid
                state["closure_receipt_document"] = receipt.document()
            elif kind == "safe_closure_completed":
                if state["ack_receipt"] is None:
                    raise OutputPathError("batch safe closure precedes exact ACK")
                expected_reference = return_reference_for_row(row_index).value
                if event.get("return_reference") != expected_reference:
                    raise OutputPathError("batch closure return reference differs")
                if event.get("return_reference_uid") != state["return_reference_uid"]:
                    raise OutputPathError("batch closure reference UID differs")
                if (
                    event.get("controller_readback_sha256")
                    != state["controller_readback_sha256"]
                ):
                    raise OutputPathError("batch closure controller readback differs")
                state["closure_receipt"] = _sha256(
                    "closure_receipt_sha256", event.get("closure_receipt_sha256")
                )
                document = event.get("closure_receipt_document")
                if document is not None:
                    if not isinstance(document, Mapping):
                        raise OutputPathError("batch closure receipt document is invalid")
                    candidate_document = dict(document)
                    if candidate_document.pop("schema", None) != (
                        "ur10e.safe_closure_receipt/v1"
                    ):
                        raise OutputPathError("batch closure receipt schema differs")
                    try:
                        candidate_document["return_reference"] = ReturnReferenceKind(
                            candidate_document["return_reference"]
                        )
                        receipt = SafeClosureReceipt(**candidate_document)
                    except (KeyError, TypeError, ValueError) as exc:
                        raise OutputPathError(
                            "batch closure receipt document differs"
                        ) from exc
                    if receipt.receipt_sha256 != state["closure_receipt"]:
                        raise OutputPathError("batch closure receipt document differs")
                    state["closure_receipt_document"] = receipt.document()
            elif kind == "trial_brief_published":
                if state["closure_receipt"] is None:
                    raise OutputPathError("TrialBrief publication precedes safe closure")
                if state["trial_brief_document_sha256"] is not None:
                    raise OutputPathError("TrialBrief publication is duplicated")
                optimizer_eligible = event.get("optimizer_eligible")
                if not isinstance(optimizer_eligible, bool):
                    raise OutputPathError("TrialBrief optimizer eligibility is invalid")
                state["trial_brief_publication_uid"] = _sha256(
                    "trial_brief_publication_uid",
                    event.get("trial_brief_publication_uid"),
                )
                state["trial_brief_document_sha256"] = _sha256(
                    "trial_brief_document_sha256",
                    event.get("trial_brief_document_sha256"),
                )
                state["optimizer_eligible"] = optimizer_eligible
            else:
                raise OutputPathError("unknown batch journal event")
        rows = []
        for index, state in enumerate(mutable, start=1):
            if state["trial_uid"] is None:
                fate = BatchFate.UNATTEMPTED
            elif state["closure_receipt"] is None:
                fate = BatchFate.ATTEMPTED_INCOMPLETE
            elif state["direct_ready_receipt"] is not None:
                fate = BatchFate.DIRECT_COMPLETED
            else:
                fate = BatchFate.ACK_COMPLETED
            rows.append(
                BatchRowState(
                    row_index=index,
                    fate=fate,
                    trial_uid=state["trial_uid"],
                    immutable_bundle_sha256=state["bundle"],
                    ack_uid=state["ack_uid"],
                    ack_receipt_sha256=state["ack_receipt"],
                    ack_receipt_document=state["ack_receipt_document"],
                    direct_ready_uid=state["direct_ready_uid"],
                    direct_ready_receipt_sha256=state["direct_ready_receipt"],
                    direct_ready_receipt_document=state[
                        "direct_ready_receipt_document"
                    ],
                    return_reference_uid=state["return_reference_uid"],
                    controller_readback_sha256=state["controller_readback_sha256"],
                    closure_receipt_sha256=state["closure_receipt"],
                    closure_receipt_document=state["closure_receipt_document"],
                    trial_brief_publication_uid=state[
                        "trial_brief_publication_uid"
                    ],
                    trial_brief_document_sha256=state[
                        "trial_brief_document_sha256"
                    ],
                    optimizer_eligible=state["optimizer_eligible"],
                    return_reference=return_reference_for_batch(identity, index),
                )
            )
        return BatchState(
            batch_uid=identity.batch_uid,
            rows=tuple(rows),
            journal_record_count=len(records),
            journal_head_sha256=(
                records[-1]["record_sha256"] if records else _ZERO_SHA256
            ),
            result_published=self.result_path.is_file(),
        )

    def start_attempt(self, row_index: int, trial_uid: str) -> None:
        identity = self.identity()

        def validate(current: BatchState) -> None:
            if current.next_row_index != row_index:
                raise SpecValidationError(
                    "attempt must target the exact next incomplete row"
                )
            row = current.rows[row_index - 1]
            if (
                row.immutable_bundle_sha256 is not None
                or row.ack_receipt_sha256 is not None
            ):
                raise SpecValidationError(
                    "durable post-execution row requires reconciliation, not re-ARM"
                )

        self._append(
            {
                "kind": "attempt_started",
                "batch_uid": identity.batch_uid,
                "row_index": row_index,
                "trial_uid": _sha256("trial_uid", trial_uid),
            },
            validator=validate,
        )

    def record_bundle(self, row_index: int, trial_uid: str, bundle_sha256: str) -> None:
        identity = self.identity()
        return_reference_for_batch(identity, row_index)

        def validate(current: BatchState) -> None:
            row = current.rows[row_index - 1]
            if (
                current.next_row_index != row_index
                or row.fate is not BatchFate.ATTEMPTED_INCOMPLETE
                or row.trial_uid != trial_uid
                or row.immutable_bundle_sha256 is not None
                or row.ack_receipt_sha256 is not None
            ):
                raise SpecValidationError(
                    "bundle does not bind the active incomplete row"
                )

        self._append(
            {
                "kind": "bundle_written",
                "batch_uid": identity.batch_uid,
                "row_index": row_index,
                "trial_uid": _sha256("trial_uid", trial_uid),
                "immutable_bundle_sha256": _sha256(
                    "immutable_bundle_sha256", bundle_sha256
                ),
            },
            validator=validate,
        )

    def record_ack_consumed(
        self,
        receipt: ExactAckReceipt,
    ) -> None:
        identity = self.identity()

        def validate(current: BatchState) -> None:
            row = current.rows[receipt.row_index - 1]
            identity_row = identity.rows[receipt.row_index - 1]
            if (
                receipt.batch_uid != identity.batch_uid
                or current.next_row_index != receipt.row_index
                or row.fate is not BatchFate.ATTEMPTED_INCOMPLETE
                or row.trial_uid != receipt.trial_uid
                or row.immutable_bundle_sha256 is None
                or receipt.immutable_bundle_sha256 != row.immutable_bundle_sha256
                or row.ack_receipt_sha256 is not None
                or receipt.control_candidate_uid != identity_row.control_candidate_uid
            ):
                raise SpecValidationError(
                    "ACK receipt does not bind the active bundle row"
                )

        self._append(
            {
                "kind": "ack_consumed",
                "batch_uid": identity.batch_uid,
                "row_index": receipt.row_index,
                "trial_uid": receipt.trial_uid,
                "ack_uid": receipt.ack_uid,
                "ack_receipt_sha256": canonical_sha256(receipt.document()),
                "ack_receipt_document": receipt.document(),
                "return_reference_uid": receipt.return_reference_uid,
                "controller_readback_sha256": receipt.controller_readback_sha256,
            },
            validator=validate,
        )

    def record_direct_ready(self, receipt: DirectReadyReceipt) -> None:
        identity = self.identity()

        def validate(current: BatchState) -> None:
            row = current.rows[receipt.row_index - 1]
            identity_row = identity.rows[receipt.row_index - 1]
            if any(
                (
                    receipt.batch_uid != identity.batch_uid,
                    current.next_row_index != receipt.row_index,
                    row.fate is not BatchFate.ATTEMPTED_INCOMPLETE,
                    row.trial_uid != receipt.trial_uid,
                    row.immutable_bundle_sha256 is None,
                    receipt.immutable_bundle_sha256
                    != row.immutable_bundle_sha256,
                    row.ack_receipt_sha256 is not None,
                    row.direct_ready_receipt_sha256 is not None,
                    receipt.control_candidate_uid
                    != identity_row.control_candidate_uid,
                )
            ):
                raise SpecValidationError(
                    "direct-ready receipt does not bind the active bundle row"
                )

        self._append(
            {
                "kind": "direct_ready_completed",
                "batch_uid": identity.batch_uid,
                "row_index": receipt.row_index,
                "trial_uid": receipt.trial_uid,
                "direct_ready_uid": receipt.completion_uid,
                "direct_ready_receipt_sha256": canonical_sha256(
                    receipt.document()
                ),
                "direct_ready_receipt_document": receipt.document(),
            },
            validator=validate,
        )

    def record_safe_closure(
        self,
        receipt: SafeClosureReceipt,
    ) -> None:
        identity = self.identity()

        def validate(current: BatchState) -> None:
            row = current.rows[receipt.row_index - 1]
            if (
                receipt.batch_uid != identity.batch_uid
                or current.next_row_index != receipt.row_index
                or row.fate is not BatchFate.ATTEMPTED_INCOMPLETE
                or row.trial_uid != receipt.trial_uid
                or row.ack_uid != receipt.ack_uid
                or row.ack_receipt_sha256 is None
                or row.return_reference_uid != receipt.return_reference_uid
                or row.controller_readback_sha256
                != receipt.controller_readback_sha256
                or row.closure_receipt_sha256 is not None
            ):
                raise SpecValidationError(
                    "safe closure receipt does not bind the consumed ACK"
                )

        self._append(
            {
                "kind": "safe_closure_completed",
                "batch_uid": identity.batch_uid,
                "row_index": receipt.row_index,
                "trial_uid": receipt.trial_uid,
                "return_reference": receipt.return_reference.value,
                "return_reference_uid": receipt.return_reference_uid,
                "controller_readback_sha256": receipt.controller_readback_sha256,
                "closure_receipt_sha256": receipt.receipt_sha256,
                "closure_receipt_document": receipt.document(),
            },
            validator=validate,
        )

    def record_trial_brief_published(
        self,
        *,
        row_index: int,
        trial_uid: str,
        publication_uid: str,
        document_sha256: str,
        optimizer_eligible: bool,
    ) -> None:
        identity = self.identity()
        return_reference_for_batch(identity, row_index)
        _sha256("trial_brief_publication_uid", publication_uid)
        _sha256("trial_brief_document_sha256", document_sha256)
        if not isinstance(optimizer_eligible, bool):
            raise SpecValidationError("optimizer_eligible must be boolean")

        def validate(current: BatchState) -> None:
            row = current.rows[row_index - 1]
            if (
                row.fate
                not in {BatchFate.ACK_COMPLETED, BatchFate.DIRECT_COMPLETED}
                or row.trial_uid != trial_uid
                or row.closure_receipt_sha256 is None
                or row.trial_brief_document_sha256 is not None
            ):
                raise SpecValidationError(
                    "TrialBrief publication requires exact ACK-completed row"
                )

        self._append(
            {
                "kind": "trial_brief_published",
                "batch_uid": identity.batch_uid,
                "row_index": row_index,
                "trial_uid": _sha256("trial_uid", trial_uid),
                "trial_brief_publication_uid": publication_uid,
                "trial_brief_document_sha256": document_sha256,
                "optimizer_eligible": optimizer_eligible,
            },
            validator=validate,
        )

    def finalize(self) -> dict[str, Any]:
        descriptor = self._lock()
        try:
            if self.result_path.exists():
                result = _read_regular_json(self.result_path, "BatchResult")
                if not isinstance(result, dict):
                    raise OutputPathError("BatchResult must be an object")
                return result
            state = self.state()
            if not state.complete:
                raise SpecValidationError("BatchResult requires all rows completed")
            if state.unpublished_trial_brief_row_indices:
                raise SpecValidationError(
                    "BatchResult requires TrialBrief publication for every row"
                )
            result = {
                "schema": "ur10e.batch_result/v1",
                "batch_uid": state.batch_uid,
                "row_count": len(state.rows),
                "rows": [row.to_dict() for row in state.rows],
                "final_home_closure_receipt_sha256": state.rows[-1].closure_receipt_sha256,
                "journal_record_count": state.journal_record_count,
                "journal_head_sha256": state.journal_head_sha256,
                "exit_code": 0,
            }
            result["batch_result_uid"] = canonical_sha256(result)
            _write_exclusive(self.result_path, result)
            return result
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def verified_exit_code(self) -> int:
        if not self.result_path.is_file():
            raise SpecValidationError("durable BatchResult is required before normal exit")
        result = _read_regular_json(self.result_path, "BatchResult")
        if not isinstance(result, dict):
            raise OutputPathError("BatchResult must be an object")
        unsigned = dict(result)
        uid = unsigned.pop("batch_result_uid", None)
        if uid != canonical_sha256(unsigned):
            raise OutputPathError("BatchResult digest differs")
        state = self.state()
        if not state.complete or result.get("journal_head_sha256") != state.journal_head_sha256:
            raise OutputPathError("BatchResult differs from current durable journal")
        if result.get("final_home_closure_receipt_sha256") != state.rows[-1].closure_receipt_sha256:
            raise OutputPathError("BatchResult lacks exact final-home closure")
        return 0
