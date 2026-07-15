#!/usr/bin/env python3
"""Durable, fail-closed supervisor journal for Step5d-native autotune.

This module is intentionally offline: it never opens RTDE, controller, bridge,
or robot endpoints.  Every command which may later be sent to the TP must first
exist in a fsync-complete journal revision.  Recovery reconciles that durable
revision with one read-only TP register snapshot and refuses ambiguous progress.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Sequence


JOURNAL_SCHEMA = "step5d_autotune_supervisor_journal_v1"
HEAD_SCHEMA = "step5d_autotune_supervisor_journal_head_v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECORD_NAME = re.compile(r"^(?P<revision>[0-9]{20})\.json$")
_MAX_JSON_BYTES = 16 * 1024 * 1024

PHASES = frozenset(
    {
        "home",
        "trial_active",
        "wait_ack",
        "wait_infra_ready",
        "paused_code_bug",
        "manual_recovery",
        "stopped_safety",
        "stopped_parameter",
        "stopped_operator",
        "stopped_fail_closed",
        "succeeded",
    }
)
POST_ACK_PHASES = PHASES - {"trial_active", "wait_ack"}
TRANSIENT_TP_STATES = frozenset(
    {"ARMED", "RUN", "TERMINAL", "RETRACT", "RETURN", "HOME_VERIFY"}
)
TP_STATES = TRANSIENT_TP_STATES | {
    "READY_HOME",
    "WAIT_ACK",
    "WAIT_INFRA_READY",
    "FAULT",
}
DISPATCH_COMMANDS = frozenset({"arm", "ack_bundle"})
TERMINAL_FATE_KINDS = frozenset(
    {"cancelled_unconsumed", "infra_aborted_consumed", "ack_consumed"}
)


class JournalError(RuntimeError):
    """Base class for journal failures."""


class JournalIntegrityError(JournalError):
    """Raised when durable journal bytes or invariants fail verification."""


class JournalConflictError(JournalError):
    """Raised on stale revision or non-monotonic state publication."""


def _strict_int(name: str, value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _strict_string(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty string without NUL")
    return value


def _strict_sha(name: str, value: Any) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return value


def _optional_sha(name: str, value: Any) -> str | None:
    return None if value is None else _strict_sha(name, value)


def _exact_object(name: str, payload: Any, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"{name} must be an object")
    actual = set(payload)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise ValueError(f"{name} keys differ; missing={missing}, extra={extra}")
    return payload


def _canonical(payload: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("journal payload is not strict finite JSON") from exc
    return (text + "\n").encode("utf-8")


def _sha(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key is forbidden: {key}")
        result[key] = value
    return result


def _strict_json(encoded: bytes, *, role: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise JournalIntegrityError(f"{role} is not strict JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise JournalIntegrityError(f"{role} must be a JSON object")
    try:
        canonical = _canonical(payload)
    except ValueError as exc:
        raise JournalIntegrityError(f"{role} is not canonical finite JSON") from exc
    if canonical != encoded:
        raise JournalIntegrityError(f"{role} bytes are not canonical")
    return payload


@dataclass(frozen=True)
class CampaignIdentity:
    campaign_id: str
    campaign_epoch: int
    campaign_fingerprint: str
    backend_id: str
    source_fingerprint: str
    config_fingerprint: str

    def __post_init__(self) -> None:
        _strict_string("campaign_id", self.campaign_id)
        _strict_int("campaign_epoch", self.campaign_epoch, minimum=1)
        _strict_sha("campaign_fingerprint", self.campaign_fingerprint)
        _strict_string("backend_id", self.backend_id)
        _strict_sha("source_fingerprint", self.source_fingerprint)
        _strict_sha("config_fingerprint", self.config_fingerprint)

    def payload(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "campaign_epoch": self.campaign_epoch,
            "campaign_fingerprint": self.campaign_fingerprint,
            "campaign_id": self.campaign_id,
            "config_fingerprint": self.config_fingerprint,
            "source_fingerprint": self.source_fingerprint,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "CampaignIdentity":
        row = _exact_object(
            "campaign identity",
            payload,
            {
                "campaign_id",
                "campaign_epoch",
                "campaign_fingerprint",
                "backend_id",
                "source_fingerprint",
                "config_fingerprint",
            },
        )
        return cls(**row)


@dataclass(frozen=True)
class HighWaterMarks:
    trial_id: int = 0
    command_seq: int = 0
    candidate_token: int = 0

    def __post_init__(self) -> None:
        _strict_int("trial_id high-water", self.trial_id)
        _strict_int("command_seq high-water", self.command_seq)
        _strict_int("candidate_token high-water", self.candidate_token)

    def payload(self) -> dict[str, int]:
        return {
            "candidate_token": self.candidate_token,
            "command_seq": self.command_seq,
            "trial_id": self.trial_id,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "HighWaterMarks":
        row = _exact_object(
            "high_water", payload, {"trial_id", "command_seq", "candidate_token"}
        )
        return cls(**row)


@dataclass(frozen=True)
class JournalReference:
    reference_id: str
    path: str
    sha256: str

    def __post_init__(self) -> None:
        _strict_sha("reference_id", self.reference_id)
        _strict_sha("reference sha256", self.sha256)
        _strict_string("reference path", self.path)
        path = Path(self.path)
        if not path.is_absolute() or os.path.normpath(self.path) != self.path:
            raise ValueError("reference path must be normalized and absolute")

    def payload(self) -> dict[str, str]:
        return {
            "path": self.path,
            "reference_id": self.reference_id,
            "sha256": self.sha256,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "JournalReference":
        row = _exact_object(
            "journal reference", payload, {"reference_id", "path", "sha256"}
        )
        return cls(**row)


@dataclass(frozen=True)
class RetryRelease:
    """Exact acknowledged TP identity from which a retry ARM is released."""

    origin_trial_uid: str
    trial_id: int
    candidate_token: int
    execution_profile_integer_id: int
    consumed_command_seq: int
    terminal_reason: int
    host_cause: str | None

    def __post_init__(self) -> None:
        _strict_sha("retry release origin_trial_uid", self.origin_trial_uid)
        _strict_int("retry release trial_id", self.trial_id, minimum=1)
        _strict_int("retry release candidate_token", self.candidate_token, minimum=1)
        _strict_int(
            "retry release execution_profile_integer_id",
            self.execution_profile_integer_id,
            minimum=1,
        )
        _strict_int(
            "retry release consumed_command_seq",
            self.consumed_command_seq,
            minimum=1,
        )
        _strict_int("retry release terminal_reason", self.terminal_reason, minimum=1)
        if self.host_cause is not None:
            _strict_string("retry release host_cause", self.host_cause)

    def payload(self) -> dict[str, Any]:
        return {
            "candidate_token": self.candidate_token,
            "consumed_command_seq": self.consumed_command_seq,
            "execution_profile_integer_id": self.execution_profile_integer_id,
            "host_cause": self.host_cause,
            "origin_trial_uid": self.origin_trial_uid,
            "terminal_reason": self.terminal_reason,
            "trial_id": self.trial_id,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "RetryRelease":
        row = _exact_object(
            "retry release",
            payload,
            {
                "origin_trial_uid",
                "trial_id",
                "candidate_token",
                "execution_profile_integer_id",
                "consumed_command_seq",
                "terminal_reason",
                "host_cause",
            },
        )
        return cls(**row)


@dataclass(frozen=True)
class TrialCursor:
    trial_uid: str
    candidate_uid: str
    trial_id: int
    candidate_token: int
    arm_command_seq: int
    execution_profile_integer_id: int
    trial_spec: JournalReference
    retry_release: RetryRelease | None = None

    def __post_init__(self) -> None:
        _strict_sha("trial_uid", self.trial_uid)
        _strict_sha("candidate_uid", self.candidate_uid)
        _strict_int("trial_id", self.trial_id, minimum=1)
        _strict_int("candidate_token", self.candidate_token, minimum=1)
        _strict_int("arm_command_seq", self.arm_command_seq, minimum=1)
        _strict_int(
            "execution_profile_integer_id",
            self.execution_profile_integer_id,
            minimum=1,
        )
        if not isinstance(self.trial_spec, JournalReference):
            raise ValueError("trial cursor requires a durable trial-spec reference")
        if self.trial_spec.reference_id != self.trial_uid:
            raise ValueError("trial-spec reference must identify the exact trial_uid")
        if self.retry_release is not None:
            if not isinstance(self.retry_release, RetryRelease):
                raise ValueError("retry_release must be RetryRelease or None")
            if self.retry_release.trial_id >= self.trial_id:
                raise ValueError("infrastructure release must precede the new trial")
            if self.retry_release.consumed_command_seq >= self.arm_command_seq:
                raise ValueError("new ARM sequence must follow the released TP sequence")

    def payload(self) -> dict[str, Any]:
        return {
            "arm_command_seq": self.arm_command_seq,
            "candidate_token": self.candidate_token,
            "candidate_uid": self.candidate_uid,
            "execution_profile_integer_id": self.execution_profile_integer_id,
            "retry_release": (
                None if self.retry_release is None else self.retry_release.payload()
            ),
            "trial_spec": self.trial_spec.payload(),
            "trial_id": self.trial_id,
            "trial_uid": self.trial_uid,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "TrialCursor":
        row = _exact_object(
            "trial cursor",
            payload,
            {
                "trial_uid",
                "candidate_uid",
                "trial_id",
                "candidate_token",
                "arm_command_seq",
                "execution_profile_integer_id",
                "retry_release",
                "trial_spec",
            },
        )
        return cls(
            trial_uid=row["trial_uid"],
            candidate_uid=row["candidate_uid"],
            trial_id=row["trial_id"],
            candidate_token=row["candidate_token"],
            arm_command_seq=row["arm_command_seq"],
            execution_profile_integer_id=row["execution_profile_integer_id"],
            retry_release=(
                None
                if row["retry_release"] is None
                else RetryRelease.from_payload(row["retry_release"])
            ),
            trial_spec=JournalReference.from_payload(row["trial_spec"]),
        )


@dataclass(frozen=True)
class PendingAck:
    trial: TrialCursor
    ack_command_seq: int
    immutable_bundle: JournalReference
    post_ack_phase: str
    terminal_reason: int = 1
    host_cause: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.trial, TrialCursor):
            raise ValueError("pending ACK trial must be a TrialCursor")
        if not isinstance(self.immutable_bundle, JournalReference):
            raise ValueError("pending ACK requires an immutable bundle reference")
        _strict_int("ack_command_seq", self.ack_command_seq, minimum=1)
        if self.ack_command_seq <= self.trial.arm_command_seq:
            raise ValueError("ACK command sequence must follow ARM command sequence")
        if self.post_ack_phase not in POST_ACK_PHASES:
            raise ValueError("post_ack_phase is not a valid post-ACK phase")
        _strict_int("pending ACK terminal_reason", self.terminal_reason, minimum=1)
        if self.host_cause is not None:
            _strict_string("pending ACK host_cause", self.host_cause)

    def payload(self) -> dict[str, Any]:
        return {
            "ack_command_seq": self.ack_command_seq,
            "immutable_bundle": self.immutable_bundle.payload(),
            "post_ack_phase": self.post_ack_phase,
            "terminal_reason": self.terminal_reason,
            "host_cause": self.host_cause,
            "trial": self.trial.payload(),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "PendingAck":
        row = _exact_object(
            "pending_ack",
            payload,
            {
                "trial",
                "ack_command_seq",
                "immutable_bundle",
                "post_ack_phase",
                "terminal_reason",
                "host_cause",
            },
        )
        return cls(
            trial=TrialCursor.from_payload(row["trial"]),
            ack_command_seq=row["ack_command_seq"],
            immutable_bundle=JournalReference.from_payload(row["immutable_bundle"]),
            post_ack_phase=row["post_ack_phase"],
            terminal_reason=row["terminal_reason"],
            host_cause=row["host_cause"],
        )


@dataclass(frozen=True)
class PendingRetry:
    origin_trial: TrialCursor
    kind: str
    last_consumed_command_seq: int
    terminal_reason: int = 1
    host_cause: str | None = None
    unlimited: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.origin_trial, TrialCursor):
            raise ValueError("pending retry origin must be a TrialCursor")
        if self.kind not in {"infrastructure", "evidence", "code_fix"}:
            raise ValueError("pending retry kind is invalid")
        _strict_int(
            "last_consumed_command_seq",
            self.last_consumed_command_seq,
            minimum=self.origin_trial.arm_command_seq,
        )
        if self.unlimited is not True:
            raise ValueError("non-parameter retries must remain unlimited")
        _strict_int("pending retry terminal_reason", self.terminal_reason, minimum=1)
        if self.host_cause is not None:
            _strict_string("pending retry host_cause", self.host_cause)

    def payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "last_consumed_command_seq": self.last_consumed_command_seq,
            "terminal_reason": self.terminal_reason,
            "host_cause": self.host_cause,
            "origin_trial": self.origin_trial.payload(),
            "unlimited": self.unlimited,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "PendingRetry":
        row = _exact_object(
            "pending_retry",
            payload,
            {
                "origin_trial",
                "kind",
                "last_consumed_command_seq",
                "terminal_reason",
                "host_cause",
                "unlimited",
            },
        )
        if type(row["unlimited"]) is not bool:
            raise ValueError("pending retry unlimited must be a boolean")
        return cls(
            origin_trial=TrialCursor.from_payload(row["origin_trial"]),
            kind=row["kind"],
            last_consumed_command_seq=row["last_consumed_command_seq"],
            terminal_reason=row["terminal_reason"],
            host_cause=row["host_cause"],
            unlimited=row["unlimited"],
        )


@dataclass(frozen=True)
class AdvisoryDispatchReceipt:
    """Read-back of one mailbox publication; never proof of TP consumption."""

    trial_uid: str
    campaign_epoch: int
    trial_id: int
    command: str
    candidate_token: int
    execution_profile_integer_id: int
    command_seq: int
    mailbox_sha256: str

    def __post_init__(self) -> None:
        _strict_sha("dispatch receipt trial_uid", self.trial_uid)
        _strict_int("dispatch receipt campaign_epoch", self.campaign_epoch, minimum=1)
        _strict_int("dispatch receipt trial_id", self.trial_id, minimum=1)
        if self.command not in DISPATCH_COMMANDS:
            raise ValueError("dispatch receipt command is invalid")
        _strict_int("dispatch receipt candidate_token", self.candidate_token, minimum=1)
        _strict_int(
            "dispatch receipt execution_profile_integer_id",
            self.execution_profile_integer_id,
            minimum=1,
        )
        _strict_int("dispatch receipt command_seq", self.command_seq, minimum=1)
        _strict_sha("dispatch receipt mailbox_sha256", self.mailbox_sha256)

    def payload(self) -> dict[str, Any]:
        return {
            "campaign_epoch": self.campaign_epoch,
            "candidate_token": self.candidate_token,
            "command": self.command,
            "command_seq": self.command_seq,
            "execution_profile_integer_id": self.execution_profile_integer_id,
            "mailbox_sha256": self.mailbox_sha256,
            "trial_id": self.trial_id,
            "trial_uid": self.trial_uid,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "AdvisoryDispatchReceipt":
        row = _exact_object(
            "dispatch receipt",
            payload,
            {
                "trial_uid",
                "campaign_epoch",
                "trial_id",
                "command",
                "candidate_token",
                "execution_profile_integer_id",
                "command_seq",
                "mailbox_sha256",
            },
        )
        return cls(**row)


@dataclass(frozen=True)
class TerminalFate:
    """Append-only terminal receipt for one durable TrialCursor."""

    kind: str
    trial: TrialCursor
    command: str
    command_seq: int
    tp_snapshot: "TpSnapshot"
    dispatch_receipt: AdvisoryDispatchReceipt | None = None
    evidence: JournalReference | None = None

    def __post_init__(self) -> None:
        if self.kind not in TERMINAL_FATE_KINDS:
            raise ValueError("terminal fate kind is invalid")
        if not isinstance(self.trial, TrialCursor):
            raise ValueError("terminal fate trial must be a TrialCursor")
        if self.command not in DISPATCH_COMMANDS:
            raise ValueError("terminal fate command is invalid")
        _strict_int("terminal fate command_seq", self.command_seq, minimum=1)
        if not isinstance(self.tp_snapshot, TpSnapshot):
            raise ValueError("terminal fate requires an exact TP snapshot")
        if self.dispatch_receipt is not None and not isinstance(
            self.dispatch_receipt, AdvisoryDispatchReceipt
        ):
            raise ValueError("terminal fate dispatch receipt is invalid")
        if self.evidence is not None and not isinstance(
            self.evidence, JournalReference
        ):
            raise ValueError("terminal fate evidence is invalid")
        if self.kind == "cancelled_unconsumed":
            if self.command != "arm" or self.command_seq != self.trial.arm_command_seq:
                raise ValueError("cancel fate must bind the exact ARM")
            if self.dispatch_receipt is not None or self.evidence is not None:
                raise ValueError("cancel fate cannot carry receipt/evidence")
        elif self.kind == "infra_aborted_consumed":
            if self.command != "arm" or self.command_seq != self.trial.arm_command_seq:
                raise ValueError("infra-abort fate must bind the exact ARM")
            if self.dispatch_receipt is None or self.evidence is None:
                raise ValueError("infra-abort fate requires dispatch and stop evidence")
        elif self.command != "ack_bundle" or self.command_seq <= self.trial.arm_command_seq:
            raise ValueError("ACK fate must bind a sequence newer than ARM")
        elif self.evidence is not None:
            raise ValueError("ACK fate does not accept external stop evidence")

    def payload(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "command_seq": self.command_seq,
            "dispatch_receipt": (
                None
                if self.dispatch_receipt is None
                else self.dispatch_receipt.payload()
            ),
            "evidence": None if self.evidence is None else self.evidence.payload(),
            "kind": self.kind,
            "tp_snapshot": self.tp_snapshot.payload(),
            "trial": self.trial.payload(),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "TerminalFate":
        required = {
            "kind",
            "trial",
            "command",
            "command_seq",
            "tp_snapshot",
            "dispatch_receipt",
        }
        if isinstance(payload, Mapping) and "evidence" in payload:
            required.add("evidence")
        row = _exact_object(
            "terminal fate",
            payload,
            required,
        )
        return cls(
            kind=row["kind"],
            trial=TrialCursor.from_payload(row["trial"]),
            command=row["command"],
            command_seq=row["command_seq"],
            tp_snapshot=TpSnapshot.from_payload(row["tp_snapshot"]),
            dispatch_receipt=(
                None
                if row["dispatch_receipt"] is None
                else AdvisoryDispatchReceipt.from_payload(row["dispatch_receipt"])
            ),
            evidence=(
                None
                if row.get("evidence") is None
                else JournalReference.from_payload(row["evidence"])
            ),
        )


@dataclass(frozen=True)
class GovernorProbe:
    layer: str
    profile_before_id: str
    profile_after_id: str
    stage: str
    force_candidate_uid: str
    plant_epoch: int
    trial_a_uid: str
    trial_b_uid: str | None = None
    trial_a_prime_uid: str | None = None

    def __post_init__(self) -> None:
        if self.layer not in {
            "normal_filter_rate",
            "host_qdot_slew",
            "tp_speedj_acceleration",
        }:
            raise ValueError("governor probe layer is invalid")
        _strict_string("profile_before_id", self.profile_before_id)
        _strict_string("profile_after_id", self.profile_after_id)
        if self.profile_before_id == self.profile_after_id:
            raise ValueError("governor probe profiles must differ")
        if self.stage not in {"a", "b", "a_prime"}:
            raise ValueError("governor probe stage is invalid")
        _strict_sha("governor force_candidate_uid", self.force_candidate_uid)
        _strict_int("governor plant_epoch", self.plant_epoch, minimum=1)
        _strict_sha("governor trial_a_uid", self.trial_a_uid)
        _optional_sha("governor trial_b_uid", self.trial_b_uid)
        _optional_sha("governor trial_a_prime_uid", self.trial_a_prime_uid)
        identities = [
            value
            for value in (
                self.trial_a_uid,
                self.trial_b_uid,
                self.trial_a_prime_uid,
            )
            if value is not None
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("governor A/B/A-prime trial UIDs must be distinct")
        if self.stage in {"a", "b"} and self.trial_a_prime_uid is not None:
            raise ValueError("governor A-prime identity is premature")
        if self.stage == "a_prime" and self.trial_b_uid is None:
            raise ValueError("governor A-prime stage requires a B identity")

    def payload(self) -> dict[str, Any]:
        return {
            "force_candidate_uid": self.force_candidate_uid,
            "layer": self.layer,
            "plant_epoch": self.plant_epoch,
            "profile_after_id": self.profile_after_id,
            "profile_before_id": self.profile_before_id,
            "stage": self.stage,
            "trial_a_prime_uid": self.trial_a_prime_uid,
            "trial_a_uid": self.trial_a_uid,
            "trial_b_uid": self.trial_b_uid,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "GovernorProbe":
        row = _exact_object(
            "governor probe",
            payload,
            {
                "layer",
                "profile_before_id",
                "profile_after_id",
                "stage",
                "force_candidate_uid",
                "plant_epoch",
                "trial_a_uid",
                "trial_b_uid",
                "trial_a_prime_uid",
            },
        )
        return cls(**row)


def _normalize_references(
    name: str, references: Sequence[JournalReference]
) -> tuple[JournalReference, ...]:
    if not isinstance(references, (tuple, list)):
        raise ValueError(f"{name} must be a sequence")
    rows = tuple(references)
    if any(not isinstance(row, JournalReference) for row in rows):
        raise ValueError(f"{name} entries must be JournalReference objects")
    by_id = {row.reference_id: row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError(f"{name} contains duplicate reference ids")
    return tuple(by_id[key] for key in sorted(by_id))


def _receipt_matches_cursor(
    campaign: CampaignIdentity,
    receipt: AdvisoryDispatchReceipt,
    cursor: TrialCursor,
    *,
    command: str,
    command_seq: int,
) -> bool:
    return (
        receipt.campaign_epoch == campaign.campaign_epoch
        and receipt.trial_uid == cursor.trial_uid
        and receipt.trial_id == cursor.trial_id
        and receipt.command == command
        and receipt.candidate_token == cursor.candidate_token
        and receipt.execution_profile_integer_id
        == cursor.execution_profile_integer_id
        and receipt.command_seq == command_seq
    )


def _validate_terminal_fate(
    campaign: CampaignIdentity, fate: TerminalFate
) -> None:
    snapshot = fate.tp_snapshot
    if fate.kind == "cancelled_unconsumed":
        if any(
            (
                snapshot.state != "READY_HOME",
                snapshot.campaign_epoch_echo != 0,
                snapshot.trial_id_echo != 0,
                snapshot.candidate_token_echo != 0,
                snapshot.terminal_reason != 0,
                snapshot.execution_profile_integer_id_echo != 0,
                snapshot.consumed_command_seq >= fate.command_seq,
            )
        ):
            raise ValueError(
                "cancel fate requires exact READY_HOME proof before ARM consumption"
            )
        return
    if fate.kind == "infra_aborted_consumed":
        receipt = fate.dispatch_receipt
        if any(
            (
                snapshot.state != "READY_HOME",
                snapshot.campaign_epoch_echo != 0,
                snapshot.trial_id_echo != 0,
                snapshot.candidate_token_echo != 0,
                snapshot.terminal_reason != 0,
                snapshot.execution_profile_integer_id_echo != 0,
                snapshot.consumed_command_seq != fate.command_seq,
                receipt is None,
                receipt is not None
                and (
                    receipt.trial_uid != fate.trial.trial_uid
                    or receipt.trial_id != fate.trial.trial_id
                    or receipt.command != "arm"
                    or receipt.candidate_token != fate.trial.candidate_token
                    or receipt.execution_profile_integer_id
                    != fate.trial.execution_profile_integer_id
                    or receipt.command_seq != fate.command_seq
                ),
                fate.evidence is None,
            )
        ):
            raise ValueError(
                "infra-abort fate requires exact consumed ARM, Home, and stop evidence"
            )
        return
    if snapshot.consumed_command_seq != fate.command_seq:
        raise ValueError("ACK fate must bind exact TP command consumption")
    if fate.dispatch_receipt is not None and not _receipt_matches_cursor(
        campaign,
        fate.dispatch_receipt,
        fate.trial,
        command="ack_bundle",
        command_seq=fate.command_seq,
    ):
        raise ValueError("ACK fate dispatch receipt differs from the terminal ACK")
    if snapshot.state == "READY_HOME":
        if any(
            (
                snapshot.campaign_epoch_echo != 0,
                snapshot.trial_id_echo != 0,
                snapshot.candidate_token_echo != 0,
                snapshot.execution_profile_integer_id_echo != 0,
            )
        ):
            raise ValueError("READY_HOME ACK fate retains non-zero identity echoes")
    elif snapshot.state in {"WAIT_INFRA_READY", "FAULT"}:
        if any(
            (
                snapshot.campaign_epoch_echo != campaign.campaign_epoch,
                snapshot.trial_id_echo != fate.trial.trial_id,
                snapshot.candidate_token_echo != fate.trial.candidate_token,
                snapshot.execution_profile_integer_id_echo
                != fate.trial.execution_profile_integer_id,
            )
        ):
            raise ValueError("terminal ACK fate TP identity differs from its trial")
    else:
        raise ValueError("ACK fate TP state is not terminal after ACK consumption")


@dataclass(frozen=True)
class JournalState:
    campaign: CampaignIdentity
    phase: str
    high_water: HighWaterMarks
    candidate_tokens: Mapping[str, int]
    plant_epoch: int
    execution_profile_id: str
    execution_profile_integer_id: int
    active_trial: TrialCursor | None = None
    pending_ack: PendingAck | None = None
    pending_retry: PendingRetry | None = None
    cooldown_remaining: int = 0
    governor_probe: GovernorProbe | None = None
    observation_references: tuple[JournalReference, ...] = ()
    history_references: tuple[JournalReference, ...] = ()
    dispatch_receipt: AdvisoryDispatchReceipt | None = None
    terminal_fates: tuple[TerminalFate, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.campaign, CampaignIdentity):
            raise ValueError("campaign must be a CampaignIdentity")
        if self.phase not in PHASES:
            raise ValueError("journal phase is invalid")
        if not isinstance(self.high_water, HighWaterMarks):
            raise ValueError("high_water must be HighWaterMarks")
        if self.active_trial is not None and not isinstance(
            self.active_trial, TrialCursor
        ):
            raise ValueError("active_trial must be a TrialCursor or None")
        if self.pending_ack is not None and not isinstance(self.pending_ack, PendingAck):
            raise ValueError("pending_ack must be a PendingAck or None")
        if self.pending_retry is not None and not isinstance(
            self.pending_retry, PendingRetry
        ):
            raise ValueError("pending_retry must be a PendingRetry or None")
        if self.governor_probe is not None and not isinstance(
            self.governor_probe, GovernorProbe
        ):
            raise ValueError("governor_probe must be a GovernorProbe or None")
        if self.dispatch_receipt is not None and not isinstance(
            self.dispatch_receipt, AdvisoryDispatchReceipt
        ):
            raise ValueError(
                "dispatch_receipt must be an AdvisoryDispatchReceipt or None"
            )
        if not isinstance(self.terminal_fates, (tuple, list)) or any(
            not isinstance(fate, TerminalFate) for fate in self.terminal_fates
        ):
            raise ValueError("terminal_fates must be a sequence of TerminalFate")
        object.__setattr__(self, "terminal_fates", tuple(self.terminal_fates))
        if len({fate.trial.trial_uid for fate in self.terminal_fates}) != len(
            self.terminal_fates
        ):
            raise ValueError("terminal_fates contain duplicate trial identities")
        if not isinstance(self.candidate_tokens, Mapping):
            raise ValueError("candidate_tokens must be a mapping")
        normalized_tokens: dict[str, int] = {}
        for candidate_uid, token in self.candidate_tokens.items():
            _strict_sha("candidate token map key", candidate_uid)
            normalized_tokens[candidate_uid] = _strict_int(
                "candidate token map value", token, minimum=1
            )
        if len(set(normalized_tokens.values())) != len(normalized_tokens):
            raise ValueError("candidate token map reuses a token")
        expected_tokens = set(range(1, self.high_water.candidate_token + 1))
        if set(normalized_tokens.values()) != expected_tokens:
            raise ValueError("candidate token map must exactly cover its high-water")
        object.__setattr__(
            self,
            "candidate_tokens",
            MappingProxyType(dict(sorted(normalized_tokens.items()))),
        )
        _strict_int("plant_epoch", self.plant_epoch, minimum=1)
        _strict_string("execution_profile_id", self.execution_profile_id)
        _strict_int(
            "execution_profile_integer_id",
            self.execution_profile_integer_id,
            minimum=1,
        )
        _strict_int("cooldown_remaining", self.cooldown_remaining)
        object.__setattr__(
            self,
            "observation_references",
            _normalize_references("observation_references", self.observation_references),
        )
        object.__setattr__(
            self,
            "history_references",
            _normalize_references("history_references", self.history_references),
        )
        self._validate_phase_and_cursors()

    def _validate_phase_and_cursors(self) -> None:
        if (self.phase == "trial_active") != (self.active_trial is not None):
            raise ValueError("trial_active phase must have exactly one active trial")
        if (self.phase == "wait_ack") != (self.pending_ack is not None):
            raise ValueError("wait_ack phase must have exactly one pending ACK")
        if self.active_trial is not None and self.pending_ack is not None:
            raise ValueError("active trial and pending ACK cannot coexist")
        current_cursor: TrialCursor | None = None
        current_command: str | None = None
        current_command_seq: int | None = None
        if self.active_trial is not None:
            current_cursor = self.active_trial
            current_command = "arm"
            current_command_seq = self.active_trial.arm_command_seq
        elif self.pending_ack is not None:
            current_cursor = self.pending_ack.trial
            current_command = "ack_bundle"
            current_command_seq = self.pending_ack.ack_command_seq
        if self.dispatch_receipt is not None and (
            current_cursor is None
            or current_command is None
            or current_command_seq is None
            or not _receipt_matches_cursor(
                self.campaign,
                self.dispatch_receipt,
                current_cursor,
                command=current_command,
                command_seq=current_command_seq,
            )
        ):
            raise ValueError("dispatch receipt differs from the materialized command")
        terminal_trial_uids = {fate.trial.trial_uid for fate in self.terminal_fates}
        if current_cursor is not None and current_cursor.trial_uid in terminal_trial_uids:
            raise ValueError("a terminal trial cannot remain materialized as current")
        for fate in self.terminal_fates:
            _validate_terminal_fate(self.campaign, fate)
        if self.phase == "wait_infra_ready":
            if self.pending_retry is None or self.pending_retry.kind != "infrastructure":
                raise ValueError("wait_infra_ready requires an infrastructure retry")
        probe = self.governor_probe
        if probe is not None:
            if (
                self.execution_profile_id != probe.profile_before_id
                or self.plant_epoch != probe.plant_epoch
            ):
                raise ValueError(
                    "governor probe differs from retained profile/plant epoch"
                )
            if probe.force_candidate_uid not in self.candidate_tokens:
                raise ValueError(
                    "governor frozen candidate is absent from the candidate token map"
                )
            stage_uid = (
                probe.trial_b_uid
                if probe.stage in {"a", "b"}
                else probe.trial_a_prime_uid
            )
            transitional = (
                self.active_trial
                if self.active_trial is not None
                else self.pending_ack.trial
                if self.pending_ack is not None
                else None
            )
            if transitional is not None:
                if transitional.candidate_uid != probe.force_candidate_uid:
                    raise ValueError(
                        "governor transitional trial changed the force candidate"
                    )
                if self.active_trial is not None and stage_uid is not None:
                    raise ValueError(
                        "governor active trial cannot already have a closed identity"
                    )
                if (
                    self.pending_ack is not None
                    and stage_uid is not None
                    and stage_uid != transitional.trial_uid
                ):
                    raise ValueError(
                        "governor closed identity differs from the pending ACK trial"
                    )
            if (
                self.pending_retry is not None
                and self.pending_retry.origin_trial.candidate_uid
                != probe.force_candidate_uid
            ):
                raise ValueError("governor retry changed the force candidate")
        for cursor in self._cursors():
            if cursor.trial_id > self.high_water.trial_id:
                raise ValueError("trial cursor exceeds trial high-water")
            if cursor.arm_command_seq > self.high_water.command_seq:
                raise ValueError("trial cursor exceeds command high-water")
            if cursor.candidate_token > self.high_water.candidate_token:
                raise ValueError("trial cursor exceeds candidate token high-water")
            if self.candidate_tokens.get(cursor.candidate_uid) != cursor.candidate_token:
                raise ValueError("trial cursor disagrees with candidate token map")
        if self.pending_ack is not None:
            if self.pending_ack.ack_command_seq > self.high_water.command_seq:
                raise ValueError("pending ACK exceeds command high-water")
        if self.pending_retry is not None:
            if self.pending_retry.last_consumed_command_seq > self.high_water.command_seq:
                raise ValueError("pending retry exceeds command high-water")

    def _cursors(self) -> tuple[TrialCursor, ...]:
        rows: list[TrialCursor] = []
        if self.active_trial is not None:
            rows.append(self.active_trial)
        if self.pending_ack is not None:
            rows.append(self.pending_ack.trial)
        if self.pending_retry is not None:
            rows.append(self.pending_retry.origin_trial)
        rows.extend(fate.trial for fate in self.terminal_fates)
        return tuple(rows)

    def payload(self) -> dict[str, Any]:
        return {
            "active_trial": None if self.active_trial is None else self.active_trial.payload(),
            "candidate_tokens": dict(self.candidate_tokens),
            "execution_profile_id": self.execution_profile_id,
            "execution_profile_integer_id": self.execution_profile_integer_id,
            "governor": {
                "cooldown_remaining": self.cooldown_remaining,
                "probe": None if self.governor_probe is None else self.governor_probe.payload(),
            },
            "high_water": self.high_water.payload(),
            "history_references": [row.payload() for row in self.history_references],
            "observation_references": [row.payload() for row in self.observation_references],
            "pending_ack": None if self.pending_ack is None else self.pending_ack.payload(),
            "pending_retry": None if self.pending_retry is None else self.pending_retry.payload(),
            "phase": self.phase,
            "plant_epoch": self.plant_epoch,
            "dispatch_receipt": (
                None
                if self.dispatch_receipt is None
                else self.dispatch_receipt.payload()
            ),
            "terminal_fates": [fate.payload() for fate in self.terminal_fates],
        }

    @classmethod
    def from_payload(
        cls, campaign: CampaignIdentity, payload: Any
    ) -> "JournalState":
        legacy_keys = {
                "phase",
                "high_water",
                "candidate_tokens",
                "plant_epoch",
                "execution_profile_id",
                "execution_profile_integer_id",
                "active_trial",
                "pending_ack",
                "pending_retry",
                "governor",
                "observation_references",
                "history_references",
        }
        if not isinstance(payload, dict):
            raise ValueError("journal state must be an object")
        actual_keys = set(payload)
        current_keys = legacy_keys | {"dispatch_receipt", "terminal_fates"}
        if actual_keys != legacy_keys and actual_keys != current_keys:
            missing = sorted(current_keys - actual_keys)
            extra = sorted(actual_keys - current_keys)
            raise ValueError(
                f"journal state keys differ; missing={missing}, extra={extra}"
            )
        row = payload
        governor = _exact_object(
            "governor", row["governor"], {"cooldown_remaining", "probe"}
        )
        tokens = row["candidate_tokens"]
        if not isinstance(tokens, dict):
            raise ValueError("candidate_tokens must be an object")
        observations = row["observation_references"]
        histories = row["history_references"]
        if not isinstance(observations, list) or not isinstance(histories, list):
            raise ValueError("journal references must be arrays")
        fates = row.get("terminal_fates", [])
        if not isinstance(fates, list):
            raise ValueError("terminal_fates must be an array")
        return cls(
            campaign=campaign,
            phase=row["phase"],
            high_water=HighWaterMarks.from_payload(row["high_water"]),
            candidate_tokens=tokens,
            plant_epoch=row["plant_epoch"],
            execution_profile_id=row["execution_profile_id"],
            execution_profile_integer_id=row["execution_profile_integer_id"],
            active_trial=(
                None
                if row["active_trial"] is None
                else TrialCursor.from_payload(row["active_trial"])
            ),
            pending_ack=(
                None
                if row["pending_ack"] is None
                else PendingAck.from_payload(row["pending_ack"])
            ),
            pending_retry=(
                None
                if row["pending_retry"] is None
                else PendingRetry.from_payload(row["pending_retry"])
            ),
            cooldown_remaining=governor["cooldown_remaining"],
            governor_probe=(
                None if governor["probe"] is None else GovernorProbe.from_payload(governor["probe"])
            ),
            observation_references=tuple(
                JournalReference.from_payload(item) for item in observations
            ),
            history_references=tuple(
                JournalReference.from_payload(item) for item in histories
            ),
            dispatch_receipt=(
                None
                if row.get("dispatch_receipt") is None
                else AdvisoryDispatchReceipt.from_payload(row["dispatch_receipt"])
            ),
            terminal_fates=tuple(TerminalFate.from_payload(item) for item in fates),
        )


@dataclass(frozen=True)
class JournalEntry:
    revision: int
    previous_record_sha256: str | None
    record_sha256: str
    state: JournalState


def _reference_map(rows: Sequence[JournalReference]) -> dict[str, JournalReference]:
    return {row.reference_id: row for row in rows}


def _validate_transition(previous: JournalState, current: JournalState) -> None:
    if current.campaign != previous.campaign:
        raise JournalConflictError("campaign identity cannot change within a journal")
    for name in ("trial_id", "command_seq", "candidate_token"):
        if getattr(current.high_water, name) < getattr(previous.high_water, name):
            raise JournalConflictError(f"{name} high-water regressed")
    for candidate_uid, token in previous.candidate_tokens.items():
        if current.candidate_tokens.get(candidate_uid) != token:
            raise JournalConflictError("candidate token mapping changed or disappeared")
    old_fates = previous.terminal_fates
    new_fates = current.terminal_fates
    if len(new_fates) < len(old_fates) or new_fates[: len(old_fates)] != old_fates:
        raise JournalConflictError("terminal fate history changed or disappeared")
    if len(new_fates) > len(old_fates) + 1:
        raise JournalConflictError("only one terminal fate may be appended per revision")
    appended_fate = new_fates[-1] if len(new_fates) > len(old_fates) else None
    if appended_fate is not None:
        if appended_fate.kind in {"cancelled_unconsumed", "infra_aborted_consumed"}:
            infra_abort = appended_fate.kind == "infra_aborted_consumed"
            if any(
                (
                    previous.phase != "trial_active",
                    previous.active_trial != appended_fate.trial,
                    (previous.dispatch_receipt is not None) != infra_abort,
                    infra_abort
                    and appended_fate.dispatch_receipt != previous.dispatch_receipt,
                    current.phase != "home",
                    current.active_trial is not None,
                    current.pending_ack is not None,
                    current.dispatch_receipt is not None,
                    current.high_water != previous.high_water,
                    dict(current.candidate_tokens)
                    != dict(previous.candidate_tokens),
                )
            ):
                if infra_abort:
                    raise JournalConflictError(
                        "infra-abort fate must bind the exact persisted ARM dispatch"
                    )
                raise JournalConflictError(
                    "cancel fate must terminalize one persisted, undispatched ARM"
                )
        else:
            pending = previous.pending_ack
            if any(
                (
                    previous.phase != "wait_ack",
                    pending is None,
                    pending is not None and pending.trial != appended_fate.trial,
                    pending is not None
                    and pending.ack_command_seq != appended_fate.command_seq,
                    current.phase
                    != (None if pending is None else pending.post_ack_phase),
                    current.active_trial is not None,
                    current.pending_ack is not None,
                    current.dispatch_receipt is not None,
                    appended_fate.dispatch_receipt
                    != previous.dispatch_receipt,
                    current.high_water != previous.high_water,
                    dict(current.candidate_tokens)
                    != dict(previous.candidate_tokens),
                )
            ):
                raise JournalConflictError(
                    "ACK fate must terminalize the exact pending durable ACK"
                )
    if previous.active_trial is not None and current.active_trial != previous.active_trial:
        carried_into_ack = bool(
            current.pending_ack is not None
            and current.pending_ack.trial == previous.active_trial
        )
        terminalized = bool(
            appended_fate is not None
            and appended_fate.kind
            in {"cancelled_unconsumed", "infra_aborted_consumed"}
            and appended_fate.trial == previous.active_trial
        )
        if not carried_into_ack and not terminalized:
            raise JournalConflictError(
                "materialized ARM disappeared without ACK handoff or cancel fate"
            )
    if previous.pending_ack is not None and current.pending_ack != previous.pending_ack:
        ack_terminalized = bool(
            appended_fate is not None
            and appended_fate.kind == "ack_consumed"
            and appended_fate.trial == previous.pending_ack.trial
            and appended_fate.command_seq == previous.pending_ack.ack_command_seq
        )
        if not ack_terminalized:
            raise JournalConflictError(
                "materialized ACK disappeared without terminal fate"
            )

    def command_identity(
        state: JournalState,
    ) -> tuple[str, str, int] | None:
        if state.active_trial is not None:
            return (
                state.active_trial.trial_uid,
                "arm",
                state.active_trial.arm_command_seq,
            )
        if state.pending_ack is not None:
            return (
                state.pending_ack.trial.trial_uid,
                "ack_bundle",
                state.pending_ack.ack_command_seq,
            )
        return None

    previous_command = command_identity(previous)
    current_command = command_identity(current)
    if previous_command != current_command and current.dispatch_receipt is not None:
        raise JournalConflictError(
            "a new materialized command cannot begin with a dispatch receipt"
        )
    if previous_command == current_command:
        if (
            previous.dispatch_receipt is not None
            and current.dispatch_receipt != previous.dispatch_receipt
        ):
            raise JournalConflictError(
                "dispatch receipt changed or disappeared for the current command"
            )
    if current.plant_epoch < previous.plant_epoch:
        raise JournalConflictError("plant epoch regressed")
    profile_changed = (
        current.execution_profile_id != previous.execution_profile_id
        or current.execution_profile_integer_id
        != previous.execution_profile_integer_id
    )
    if profile_changed and current.plant_epoch == previous.plant_epoch:
        raise JournalConflictError("retained profile changed without a new plant epoch")
    old_probe = previous.governor_probe
    new_probe = current.governor_probe
    if old_probe is None and new_probe is not None and current.phase != "home":
        raise JournalConflictError("a governor probe may begin only at durable home")
    success_confirmation_abort = bool(
        old_probe is not None
        and old_probe.stage == "a_prime"
        and new_probe is None
        and current.phase == "wait_ack"
        and current.pending_ack is not None
        and current.pending_ack.post_ack_phase == "succeeded"
    )
    if (
        old_probe is not None
        and new_probe is None
        and current.phase != "home"
        and not success_confirmation_abort
    ):
        raise JournalConflictError("a governor probe may complete only at durable home")
    if old_probe is not None and new_probe is not None:
        old_static = (
            old_probe.layer,
            old_probe.profile_before_id,
            old_probe.profile_after_id,
            old_probe.force_candidate_uid,
            old_probe.plant_epoch,
            old_probe.trial_a_uid,
        )
        new_static = (
            new_probe.layer,
            new_probe.profile_before_id,
            new_probe.profile_after_id,
            new_probe.force_candidate_uid,
            new_probe.plant_epoch,
            new_probe.trial_a_uid,
        )
        if old_static != new_static:
            raise JournalConflictError("governor probe provenance changed")
        if (old_probe.stage, new_probe.stage) not in {
            ("a", "a"),
            ("b", "b"),
            ("b", "a_prime"),
            ("a_prime", "a_prime"),
        }:
            raise JournalConflictError("governor probe stage regressed")
        for label, old_uid, new_uid in (
            ("B", old_probe.trial_b_uid, new_probe.trial_b_uid),
            (
                "A-prime",
                old_probe.trial_a_prime_uid,
                new_probe.trial_a_prime_uid,
            ),
        ):
            if old_uid is not None and old_uid != new_uid:
                raise JournalConflictError(
                    f"governor {label} trial identity changed or disappeared"
                )
    for role in ("observation_references", "history_references"):
        old = _reference_map(getattr(previous, role))
        new = _reference_map(getattr(current, role))
        for reference_id, reference in old.items():
            if new.get(reference_id) != reference:
                raise JournalConflictError(f"durable {role} changed or disappeared")


def _validate_directory(path: Path, *, role: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError as exc:
        raise JournalIntegrityError(f"{role} is missing: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise JournalIntegrityError(f"{role} must not be a symlink: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise JournalIntegrityError(f"{role} must be a directory: {path}")


def _validate_regular_lstat(path: Path, *, role: str) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise JournalIntegrityError(f"{role} must not be a symlink: {path}")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise JournalIntegrityError(f"{role} must be a singly-linked regular file")


def _read_regular(path: Path, *, role: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise JournalIntegrityError(f"cannot open {role} without following links") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise JournalIntegrityError(f"{role} must be a singly-linked regular file")
        if info.st_size > _MAX_JSON_BYTES:
            raise JournalIntegrityError(f"{role} exceeds the journal size limit")
        chunks: list[bytes] = []
        remaining = _MAX_JSON_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > _MAX_JSON_BYTES:
            raise JournalIntegrityError(f"{role} exceeds the journal size limit")
        return encoded
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_publish(path: Path, encoded: bytes, *, replace: bool) -> None:
    _validate_directory(path.parent, role="journal publish directory")
    _validate_regular_lstat(path, role="journal publish target")
    if not replace and path.exists():
        raise JournalConflictError(f"immutable journal revision already exists: {path.name}")
    temporary = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if not replace and path.exists():
            raise JournalConflictError(
                f"immutable journal revision raced publication: {path.name}"
            )
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class SupervisorJournal:
    """Hash-chained, atomic, single-writer campaign journal."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("journal root must be an absolute pathlib.Path")
        self.root = root
        self.entries_dir = root / "entries"
        self.head_path = root / "latest.json"
        self.lock_path = root / ".journal.lock"

    def _initialize(self) -> None:
        if self.root.exists() or self.root.is_symlink():
            _validate_directory(self.root, role="journal root")
        else:
            self.root.mkdir(parents=True, mode=0o700)
            _fsync_directory(self.root.parent)
        if self.entries_dir.exists() or self.entries_dir.is_symlink():
            _validate_directory(self.entries_dir, role="journal entries directory")
        else:
            self.entries_dir.mkdir(mode=0o700)
            _fsync_directory(self.root)

    @contextlib.contextmanager
    def _lock(self, *, exclusive: bool, create: bool) -> Iterator[None]:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        if create:
            flags |= os.O_CREAT
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise JournalIntegrityError("journal lock is missing, invalid, or a symlink") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise JournalIntegrityError("journal lock must be a singly-linked regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            if create:
                os.fsync(descriptor)
                _fsync_directory(self.root)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def append(
        self, state: JournalState, *, expected_revision: int | None = None
    ) -> JournalEntry:
        if not isinstance(state, JournalState):
            raise ValueError("state must be a JournalState")
        if expected_revision is not None:
            _strict_int("expected_revision", expected_revision)
        self._initialize()
        with self._lock(exclusive=True, create=True):
            previous = self._load_latest_unlocked(allow_empty=True)
            current_revision = 0 if previous is None else previous.revision
            if expected_revision is not None and expected_revision != current_revision:
                raise JournalConflictError(
                    f"stale journal revision: expected {expected_revision}, found {current_revision}"
                )
            if previous is not None:
                _validate_transition(previous.state, state)
            revision = current_revision + 1
            prior_digest = None if previous is None else previous.record_sha256
            envelope: dict[str, Any] = {
                "campaign": state.campaign.payload(),
                "previous_record_sha256": prior_digest,
                "revision": revision,
                "schema_version": JOURNAL_SCHEMA,
                "state": state.payload(),
            }
            digest = _sha(_canonical(envelope))
            record = {**envelope, "record_sha256": digest}
            record_path = self.entries_dir / f"{revision:020d}.json"
            _atomic_publish(record_path, _canonical(record), replace=False)
            head = {
                "campaign": state.campaign.payload(),
                "record_sha256": digest,
                "revision": revision,
                "schema_version": HEAD_SCHEMA,
            }
            _atomic_publish(self.head_path, _canonical(head), replace=True)
            _fsync_directory(self.root)
            return JournalEntry(revision, prior_digest, digest, state)

    def load_latest(
        self, *, expected_campaign: CampaignIdentity | None = None
    ) -> JournalEntry:
        if expected_campaign is not None and not isinstance(
            expected_campaign, CampaignIdentity
        ):
            raise ValueError("expected_campaign must be a CampaignIdentity or None")
        _validate_directory(self.root, role="journal root")
        _validate_directory(self.entries_dir, role="journal entries directory")
        # Exclusive because a verified single orphan may require republishing
        # latest.json after a crash between record and head publication.
        with self._lock(exclusive=True, create=False):
            latest = self._load_latest_unlocked(allow_empty=False)
        assert latest is not None
        if expected_campaign is not None and latest.state.campaign != expected_campaign:
            raise JournalIntegrityError("journal campaign identity differs from expected identity")
        return latest

    def _record_paths(self) -> tuple[Path, ...]:
        _validate_directory(self.entries_dir, role="journal entries directory")
        rows: list[tuple[int, Path]] = []
        with os.scandir(self.entries_dir) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise JournalIntegrityError("journal entry must not be a symlink")
                match = _RECORD_NAME.fullmatch(entry.name)
                if match is None or not entry.is_file(follow_symlinks=False):
                    raise JournalIntegrityError(
                        f"unexpected or incomplete journal entry: {entry.name}"
                    )
                rows.append((int(match.group("revision")), Path(entry.path)))
        rows.sort()
        return tuple(path for _, path in rows)

    def _load_latest_unlocked(self, *, allow_empty: bool) -> JournalEntry | None:
        paths = self._record_paths()
        revisions = [int(path.stem) for path in paths]
        head_exists = self.head_path.exists() or self.head_path.is_symlink()
        head_revision = 0
        head_digest: str | None = None
        head_campaign: CampaignIdentity | None = None
        if not head_exists:
            if not paths:
                if allow_empty:
                    return None
                raise JournalIntegrityError("journal has no durable revision")
            if revisions != [1]:
                raise JournalIntegrityError(
                    "journal has multiple or non-initial orphan revisions without a head"
                )
        else:
            head_payload = _strict_json(
                _read_regular(self.head_path, role="journal head"), role="journal head"
            )
            head = _exact_object(
                "journal head",
                head_payload,
                {"schema_version", "campaign", "revision", "record_sha256"},
            )
            if head["schema_version"] != HEAD_SCHEMA:
                raise JournalIntegrityError("journal head schema differs")
            try:
                head_revision = _strict_int(
                    "head revision", head["revision"], minimum=1
                )
                head_digest = _strict_sha(
                    "head record sha256", head["record_sha256"]
                )
                head_campaign = CampaignIdentity.from_payload(head["campaign"])
            except ValueError as exc:
                raise JournalIntegrityError(f"invalid journal head: {exc}") from exc
        normal_revisions = list(range(1, head_revision + 1))
        repair_single_orphan = (
            revisions == [1]
            if not head_exists
            else revisions == list(range(1, head_revision + 2))
        )
        if head_exists and tuple(revisions) not in {
            tuple(normal_revisions),
            tuple(range(1, head_revision + 2)),
        }:
            raise JournalIntegrityError(
                "journal revisions are missing, duplicated, or contain multiple orphans"
            )

        previous: JournalEntry | None = None
        entries: list[JournalEntry] = []
        for revision, path in zip(revisions, paths):
            role = f"journal revision {revision}"
            payload = _strict_json(_read_regular(path, role=role), role=role)
            row = _exact_object(
                role,
                payload,
                {
                    "schema_version",
                    "campaign",
                    "revision",
                    "previous_record_sha256",
                    "state",
                    "record_sha256",
                },
            )
            if row["schema_version"] != JOURNAL_SCHEMA:
                raise JournalIntegrityError(f"{role} schema differs")
            envelope = {key: value for key, value in row.items() if key != "record_sha256"}
            expected_digest = _sha(_canonical(envelope))
            try:
                record_digest = _strict_sha("record sha256", row["record_sha256"])
                row_revision = _strict_int("record revision", row["revision"], minimum=1)
                campaign = CampaignIdentity.from_payload(row["campaign"])
                state = JournalState.from_payload(campaign, row["state"])
            except (ValueError, JournalConflictError) as exc:
                raise JournalIntegrityError(f"invalid {role}: {exc}") from exc
            if row_revision != revision:
                raise JournalIntegrityError(f"{role} filename/revision mismatch")
            if record_digest != expected_digest:
                raise JournalIntegrityError(f"{role} digest mismatch")
            prior_digest = None if previous is None else previous.record_sha256
            if row["previous_record_sha256"] != prior_digest:
                raise JournalIntegrityError(f"{role} hash chain is broken")
            if previous is not None:
                try:
                    _validate_transition(previous.state, state)
                except JournalConflictError as exc:
                    raise JournalIntegrityError(f"invalid {role} transition: {exc}") from exc
            previous = JournalEntry(revision, prior_digest, record_digest, state)
            entries.append(previous)

        assert previous is not None
        if head_exists:
            head_entry = entries[head_revision - 1]
            if head_entry.record_sha256 != head_digest:
                raise JournalIntegrityError("journal head does not bind its revision")
            if head_entry.state.campaign != head_campaign:
                raise JournalIntegrityError("journal head campaign identity differs")
        if repair_single_orphan:
            expected_orphan_revision = head_revision + 1
            if previous.revision != expected_orphan_revision:
                raise JournalIntegrityError("journal orphan is not exactly head revision plus one")
            repaired_head = {
                "campaign": previous.state.campaign.payload(),
                "record_sha256": previous.record_sha256,
                "revision": previous.revision,
                "schema_version": HEAD_SCHEMA,
            }
            _atomic_publish(self.head_path, _canonical(repaired_head), replace=True)
            _fsync_directory(self.root)
        elif previous.revision != head_revision:
            raise JournalIntegrityError("journal head does not bind the latest revision")
        return previous


@dataclass(frozen=True)
class TpSnapshot:
    campaign_epoch_echo: int
    trial_id_echo: int
    state: str
    candidate_token_echo: int
    terminal_reason: int
    execution_profile_integer_id_echo: int
    consumed_command_seq: int

    def __post_init__(self) -> None:
        _strict_int("TP campaign epoch echo", self.campaign_epoch_echo)
        _strict_int("TP trial id echo", self.trial_id_echo)
        _strict_int("TP candidate token echo", self.candidate_token_echo)
        _strict_int("TP terminal reason", self.terminal_reason)
        _strict_int(
            "TP execution profile integer id echo",
            self.execution_profile_integer_id_echo,
        )
        _strict_int("TP consumed command sequence", self.consumed_command_seq)
        if self.state not in TP_STATES:
            raise ValueError("TP snapshot state is not recognized")

    def payload(self) -> dict[str, Any]:
        return {
            "campaign_epoch_echo": self.campaign_epoch_echo,
            "candidate_token_echo": self.candidate_token_echo,
            "consumed_command_seq": self.consumed_command_seq,
            "execution_profile_integer_id_echo": (
                self.execution_profile_integer_id_echo
            ),
            "state": self.state,
            "terminal_reason": self.terminal_reason,
            "trial_id_echo": self.trial_id_echo,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "TpSnapshot":
        row = _exact_object(
            "TP snapshot",
            payload,
            {
                "campaign_epoch_echo",
                "trial_id_echo",
                "state",
                "candidate_token_echo",
                "terminal_reason",
                "execution_profile_integer_id_echo",
                "consumed_command_seq",
            },
        )
        return cls(**row)


class ReconcileAction(str, Enum):
    RESUME_HOME = "resume_home"
    RESUME_CLOSURE = "resume_closure"
    SEND_PERSISTED_ARM = "send_persisted_arm"
    MONITOR_ACTIVE = "monitor_active"
    SEND_PERSISTED_ACK = "send_persisted_ack"
    PERSIST_POST_ACK = "persist_post_ack"
    HOLD_WAIT_INFRA = "hold_wait_infra"
    HOLD_TERMINAL = "hold_terminal"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True)
class ReconcileDecision:
    action: ReconcileAction
    reason: str
    command_seq: int | None = None

    @property
    def fail_closed(self) -> bool:
        return self.action is ReconcileAction.FAIL_CLOSED

    @property
    def command_permitted(self) -> bool:
        return self.action in {
            ReconcileAction.SEND_PERSISTED_ARM,
            ReconcileAction.SEND_PERSISTED_ACK,
        }


def _fail(reason: str) -> ReconcileDecision:
    return ReconcileDecision(ReconcileAction.FAIL_CLOSED, reason)


def _snapshot_matches_cursor(
    campaign: CampaignIdentity, snapshot: TpSnapshot, cursor: TrialCursor
) -> bool:
    return (
        snapshot.campaign_epoch_echo == campaign.campaign_epoch
        and snapshot.trial_id_echo == cursor.trial_id
        and snapshot.candidate_token_echo == cursor.candidate_token
        and snapshot.execution_profile_integer_id_echo
        == cursor.execution_profile_integer_id
    )


def _snapshot_matches_retry_release(
    campaign: CampaignIdentity,
    snapshot: TpSnapshot,
    release: RetryRelease,
) -> bool:
    if snapshot.consumed_command_seq != release.consumed_command_seq:
        return False
    if release.terminal_reason == 4 and release.host_cause == "infra_stop":
        return (
            snapshot.state == "READY_HOME"
            and snapshot.campaign_epoch_echo == 0
            and snapshot.trial_id_echo == 0
            and snapshot.candidate_token_echo == 0
            and snapshot.execution_profile_integer_id_echo == 0
        )
    return (
        snapshot.state == "WAIT_INFRA_READY"
        and snapshot.campaign_epoch_echo == campaign.campaign_epoch
        and snapshot.trial_id_echo == release.trial_id
        and snapshot.candidate_token_echo == release.candidate_token
        and snapshot.execution_profile_integer_id_echo
        == release.execution_profile_integer_id
        and snapshot.terminal_reason == release.terminal_reason
    )


def _fault_post_ack_compatible(pending: PendingAck) -> bool:
    reason = pending.terminal_reason
    expected_phase = (
        "paused_code_bug"
        if reason == 13
        else "stopped_safety"
        if reason in {5, 6, 7}
        else "stopped_fail_closed"
    )
    return (
        reason not in {1, 2, 3, 4, 8, 10, 12, 14, 17}
        and pending.post_ack_phase == expected_phase
    )


def reconcile_tp_snapshot(
    entry: JournalEntry, snapshot: TpSnapshot
) -> ReconcileDecision:
    """Reconcile durable host intent with a read-only TP register snapshot.

    Only an already-persisted ARM or ACK may be emitted.  Any observation that
    could mean ARM/ACK reached the TP before the matching host transition was
    persisted returns ``FAIL_CLOSED``.
    """

    if not isinstance(entry, JournalEntry) or not isinstance(snapshot, TpSnapshot):
        raise ValueError("reconciliation requires JournalEntry and TpSnapshot")
    state = entry.state
    if snapshot.state == "FAULT":
        pending = state.pending_ack
        if (
            state.phase == "wait_ack"
            and pending is not None
            and _snapshot_matches_cursor(state.campaign, snapshot, pending.trial)
            and snapshot.terminal_reason == pending.terminal_reason
            and snapshot.consumed_command_seq == pending.ack_command_seq
            and _fault_post_ack_compatible(pending)
        ):
            return ReconcileDecision(
                ReconcileAction.PERSIST_POST_ACK,
                "persisted_ack_exactly_consumed_at_terminal_fault",
                pending.ack_command_seq,
            )
        retry = state.pending_retry
        if (
            state.phase == "paused_code_bug"
            and retry is not None
            and retry.kind == "code_fix"
            and _snapshot_matches_cursor(state.campaign, snapshot, retry.origin_trial)
            and snapshot.terminal_reason == retry.terminal_reason == 13
            and snapshot.consumed_command_seq == retry.last_consumed_command_seq
        ):
            return ReconcileDecision(
                ReconcileAction.HOLD_TERMINAL,
                "code_contract_bug_remains_durably_paused",
            )
        return _fail("tp_fault_requires_manual_recovery")

    if snapshot.state == "READY_HOME":
        if snapshot.campaign_epoch_echo not in {0, state.campaign.campaign_epoch}:
            return _fail("ready_home_campaign_echo_mismatch")
        if state.pending_ack is not None or state.phase == "wait_ack":
            pending = state.pending_ack
            if (
                pending is not None
                and pending.terminal_reason in {1, 4}
                and (
                    pending.post_ack_phase in {"home", "succeeded"}
                    if pending.terminal_reason == 1
                    else pending.post_ack_phase
                    in {
                        "wait_infra_ready",
                        "stopped_parameter",
                        "stopped_operator",
                        "stopped_fail_closed",
                    }
                )
                and snapshot.campaign_epoch_echo == 0
                and snapshot.trial_id_echo == 0
                and snapshot.candidate_token_echo == 0
                and snapshot.execution_profile_integer_id_echo == 0
                and snapshot.consumed_command_seq == pending.ack_command_seq
            ):
                return ReconcileDecision(
                    ReconcileAction.PERSIST_POST_ACK,
                    "persisted_ack_exactly_consumed_at_ready_home",
                    pending.ack_command_seq,
                )
            return _fail("ack_before_post_ack_persist_is_ambiguous")
        if state.phase == "wait_infra_ready":
            retry = state.pending_retry
            if (
                retry is not None
                and retry.kind == "infrastructure"
                and retry.terminal_reason == 4
                and retry.host_cause == "infra_stop"
                and snapshot.consumed_command_seq
                == retry.last_consumed_command_seq
            ):
                return ReconcileDecision(
                    ReconcileAction.HOLD_WAIT_INFRA,
                    "host_infrastructure_stop_remains_durably_paused_at_home",
                )
            return _fail("infra_release_before_persist_is_ambiguous")
        if state.active_trial is not None:
            cursor = state.active_trial
            if snapshot.consumed_command_seq >= cursor.arm_command_seq:
                return _fail("arm_consumed_but_tp_returned_home_is_ambiguous")
            if (
                snapshot.trial_id_echo > state.high_water.trial_id
                or snapshot.candidate_token_echo > state.high_water.candidate_token
            ):
                return _fail("ready_home_echo_exceeds_durable_high_water")
            return ReconcileDecision(
                ReconcileAction.SEND_PERSISTED_ARM,
                "durable_arm_not_yet_consumed",
                cursor.arm_command_seq,
            )
        if state.phase == "succeeded":
            if (
                snapshot.trial_id_echo > state.high_water.trial_id
                or snapshot.candidate_token_echo > state.high_water.candidate_token
                or snapshot.consumed_command_seq > state.high_water.command_seq
            ):
                return _fail("succeeded_home_echo_exceeds_durable_high_water")
            return ReconcileDecision(
                ReconcileAction.RESUME_HOME,
                "durable_succeeded_terminal_at_home_agrees",
            )
        if state.phase != "home":
            return _fail("ready_home_disagrees_with_durable_phase")
        if (
            snapshot.trial_id_echo > state.high_water.trial_id
            or snapshot.candidate_token_echo > state.high_water.candidate_token
            or snapshot.consumed_command_seq > state.high_water.command_seq
        ):
            return _fail("ready_home_echo_exceeds_durable_high_water")
        return ReconcileDecision(ReconcileAction.RESUME_HOME, "durable_home_agrees")

    if snapshot.state == "WAIT_ACK":
        cursor = state.active_trial
        if (
            state.phase == "trial_active"
            and cursor is not None
            and _snapshot_matches_cursor(state.campaign, snapshot, cursor)
            and snapshot.consumed_command_seq == cursor.arm_command_seq
            and snapshot.terminal_reason > 0
        ):
            return ReconcileDecision(
                ReconcileAction.RESUME_CLOSURE,
                "arm_consumed_and_tp_waits_for_durable_closure",
                cursor.arm_command_seq,
            )
        pending = state.pending_ack
        if state.phase != "wait_ack" or pending is None:
            return _fail("wait_ack_observed_before_durable_pending_ack")
        if not _snapshot_matches_cursor(state.campaign, snapshot, pending.trial):
            return _fail("wait_ack_identity_echo_mismatch")
        if snapshot.consumed_command_seq == pending.trial.arm_command_seq:
            return ReconcileDecision(
                ReconcileAction.SEND_PERSISTED_ACK,
                "immutable_bundle_and_ack_are_durable",
                pending.ack_command_seq,
            )
        if snapshot.consumed_command_seq == pending.ack_command_seq:
            return _fail("ack_consumed_without_durable_post_ack_transition")
        return _fail("wait_ack_consumed_sequence_is_ambiguous")

    if snapshot.state == "WAIT_INFRA_READY":
        pending = state.pending_ack
        if (
            state.phase == "wait_ack"
            and pending is not None
            and pending.post_ack_phase == "wait_infra_ready"
            and pending.terminal_reason in {8, 10, 12, 14}
            and _snapshot_matches_cursor(state.campaign, snapshot, pending.trial)
            and snapshot.terminal_reason == pending.terminal_reason
            and snapshot.consumed_command_seq == pending.ack_command_seq
        ):
            return ReconcileDecision(
                ReconcileAction.PERSIST_POST_ACK,
                "persisted_ack_exactly_consumed_at_wait_infra",
                pending.ack_command_seq,
            )
        cursor = state.active_trial
        if (
            state.phase == "trial_active"
            and cursor is not None
            and cursor.retry_release is not None
            and _snapshot_matches_retry_release(
                state.campaign, snapshot, cursor.retry_release
            )
        ):
            return ReconcileDecision(
                ReconcileAction.SEND_PERSISTED_ARM,
                "durable_infrastructure_retry_arm_not_yet_consumed",
                cursor.arm_command_seq,
            )
        retry = state.pending_retry
        if (
            state.phase != "wait_infra_ready"
            or retry is None
            or retry.kind != "infrastructure"
        ):
            return _fail("wait_infra_observed_before_durable_retry_state")
        if not _snapshot_matches_cursor(state.campaign, snapshot, retry.origin_trial):
            return _fail("wait_infra_identity_echo_mismatch")
        if snapshot.terminal_reason != retry.terminal_reason:
            return _fail("wait_infra_terminal_reason_mismatch")
        if snapshot.consumed_command_seq != retry.last_consumed_command_seq:
            return _fail("wait_infra_consumed_sequence_mismatch")
        return ReconcileDecision(
            ReconcileAction.HOLD_WAIT_INFRA,
            "infrastructure_retry_remains_durably_paused",
        )

    if snapshot.state in TRANSIENT_TP_STATES:
        cursor = state.active_trial
        if state.phase != "trial_active" or cursor is None:
            return _fail("arm_observed_before_durable_active_persist")
        if not _snapshot_matches_cursor(state.campaign, snapshot, cursor):
            return _fail("active_trial_identity_echo_mismatch")
        if snapshot.consumed_command_seq != cursor.arm_command_seq:
            return _fail("active_trial_consumed_sequence_mismatch")
        return ReconcileDecision(
            ReconcileAction.MONITOR_ACTIVE,
            "durable_active_trial_agrees_with_tp",
        )

    return _fail("unsupported_tp_recovery_state")
