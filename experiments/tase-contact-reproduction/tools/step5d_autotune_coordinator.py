#!/usr/bin/env python3
"""Write-ahead coordinator for Step5d-native autotune ARM/ACK commands.

The policy supervisor remains pure.  This offline coordinator is the command
issuance boundary: it returns an ARM or ACK ``HostPacket`` only after the exact
intent, store reference, counters, and pending transition have been atomically
fsync'd by ``SupervisorJournal``.  It never opens RTDE or controller endpoints.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


RUNTIME_SOURCE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "ur10e_experiment_runtime"
)
if str(RUNTIME_SOURCE) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SOURCE))

from ur10e_experiment_runtime.identity import (  # noqa: E402
    canonical_json_bytes as runtime_canonical_json_bytes,
    strict_json_loads as runtime_strict_json_loads,
)

from step5d_autotune_contract import (
    CampaignSpec,
    CaptureManifest,
    Evaluation,
    ExecutionProfile,
    ForceCandidate,
    GovernorDecision,
    SearchAttestation,
    TrialTransition,
    TrialDisposition,
    TrialSpec,
    canonical_json_bytes,
    trial_source_from_trial,
)
from step5d_autotune_governor import AbEvidence, AbTrialIdentity, SaturationSample
from step5d_autotune_journal import (
    AdvisoryDispatchReceipt,
    CampaignIdentity,
    GovernorProbe,
    HighWaterMarks,
    JournalEntry,
    JournalIntegrityError,
    JournalReference,
    JournalState,
    PendingAck,
    PendingAdvance as JournalPendingAdvance,
    PendingRetry,
    ReconcileAction,
    ReconcileDecision,
    RetryRelease,
    SupervisorJournal,
    TerminalFate,
    TpSnapshot,
    TrialCursor,
    reconcile_tp_snapshot,
)
from step5d_autotune_optimizer import Observation, success_confirmed
from step5d_autotune_state_machine import HostCommand, HostPacket, host_packet_for_trial
from step5d_autotune_supervisor import (
    CampaignPhase,
    CampaignSupervisor,
    CloseDecision,
    CompletionProtocol,
    GovernorProbeState,
    PendingAdvance,
    SupervisorRecoverySnapshot,
    TrialIntent,
    execution_profile_integer_id,
)


class TrialRegistrar(Protocol):
    def register_trial(
        self, trial: TrialSpec, *, provenance_run_dir: Path | None = None
    ) -> Path: ...


class PreparedTrialLike(Protocol):
    trial: TrialSpec
    frozen: Any
    environment: Mapping[str, str]
    runner_arguments: tuple[str, ...]


class ContinuousTpCommandSink(Protocol):
    """Adapter to one already-owned continuous TP/RTDE writer session.

    Implementations must update all six Host->TP integer values from the packet
    as one fresh command transaction.  In particular ACK is not a new bridge
    process: it targets the same continuous TP session with a larger command_seq.
    """

    def send_command(
        self, packet: HostPacket, *, prepared_trial: PreparedTrialLike
    ) -> None: ...

    def read_latest(self) -> Any | None: ...


class TrialBriefAdmissionLike(Protocol):
    trial_uid: str
    ack_command_seq: int
    publication_uid: str
    document_sha256: str
    path: Path
    file_sha256: str
    optimizer_eligible: bool


class DirectTrialBriefAdmissionLike(Protocol):
    trial_uid: str
    arm_command_seq: int
    publication_uid: str
    document_sha256: str
    path: Path
    file_sha256: str
    optimizer_eligible: bool


class CoordinatorError(RuntimeError):
    """Base class for fail-closed coordinator failures."""


class RecoveryError(CoordinatorError):
    """Raised when history, journal, and TP state cannot be reconciled exactly."""


@dataclass(frozen=True)
class ReconcileResult:
    decision: ReconcileDecision
    packet: HostPacket | None


def _trial_brief_reference(
    receipt: TrialBriefAdmissionLike,
    *,
    trial_uid: str,
    ack_command_seq: int,
    optimizer_eligible: bool,
) -> JournalReference:
    if any(
        (
            receipt.trial_uid != trial_uid,
            receipt.ack_command_seq != ack_command_seq,
            receipt.optimizer_eligible is not optimizer_eligible,
            not isinstance(receipt.path, Path),
            not receipt.path.is_absolute(),
            receipt.path.is_symlink(),
            not receipt.path.is_file(),
        )
    ):
        raise RecoveryError(
            "post-ACK TrialBrief admission differs from the pending outcome"
        )
    encoded = receipt.path.read_bytes()
    file_sha256 = hashlib.sha256(encoded).hexdigest()
    try:
        document = runtime_strict_json_loads(encoded)
    except ValueError as exc:
        raise RecoveryError("post-ACK TrialBrief is malformed") from exc
    canonical = runtime_canonical_json_bytes(document)
    if any(
        (
            encoded != canonical + b"\n",
            receipt.file_sha256 != file_sha256,
            receipt.document_sha256 != hashlib.sha256(canonical).hexdigest(),
            document.get("publication_uid") != receipt.publication_uid,
            document.get("trial_uid") != trial_uid,
            document.get("optimizer_eligible") is not optimizer_eligible,
            document.get("publication_unique") is not True,
        )
    ):
        raise RecoveryError(
            "post-ACK TrialBrief bytes differ from the admission receipt"
        )
    return JournalReference(
        reference_id=receipt.publication_uid,
        path=str(receipt.path),
        sha256=file_sha256,
    )


def _direct_trial_brief_reference(
    receipt: DirectTrialBriefAdmissionLike,
    *,
    trial_uid: str,
    arm_command_seq: int,
    optimizer_eligible: bool,
) -> JournalReference:
    if any(
        (
            receipt.trial_uid != trial_uid,
            receipt.arm_command_seq != arm_command_seq,
            receipt.optimizer_eligible is not optimizer_eligible,
            not isinstance(receipt.path, Path),
            not receipt.path.is_absolute(),
            receipt.path.is_symlink(),
            not receipt.path.is_file(),
        )
    ):
        raise RecoveryError(
            "direct-ready TrialBrief admission differs from pending outcome"
        )
    encoded = receipt.path.read_bytes()
    file_sha256 = hashlib.sha256(encoded).hexdigest()
    try:
        document = runtime_strict_json_loads(encoded)
    except ValueError as exc:
        raise RecoveryError("direct-ready TrialBrief is malformed") from exc
    canonical = runtime_canonical_json_bytes(document)
    if any(
        (
            encoded != canonical + b"\n",
            receipt.file_sha256 != file_sha256,
            receipt.document_sha256 != hashlib.sha256(canonical).hexdigest(),
            document.get("schema") != "ur-exp/trial-brief-v2",
            document.get("protocol") != "v3_direct_arm_v1",
            document.get("publication_uid") != receipt.publication_uid,
            document.get("trial_uid") != trial_uid,
            document.get("optimizer_eligible") is not optimizer_eligible,
            document.get("publication_unique") is not True,
        )
    ):
        raise RecoveryError(
            "direct-ready TrialBrief bytes differ from admission receipt"
        )
    return JournalReference(
        reference_id=receipt.publication_uid,
        path=str(receipt.path),
        sha256=file_sha256,
    )


@dataclass(frozen=True)
class RestoreResult:
    coordinator: "CampaignCoordinator"
    decision: ReconcileDecision
    packet: HostPacket | None


@dataclass(frozen=True)
class MailboxObservation:
    """One strict mailbox read; distinct from not observing the mailbox."""

    observed: bool
    packet: HostPacket | None = None
    trial_uid: str | None = None
    mailbox_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.observed) is not bool:
            raise ValueError("mailbox observed flag must be a boolean")
        if not self.observed:
            if any(
                value is not None
                for value in (self.packet, self.trial_uid, self.mailbox_sha256)
            ):
                raise ValueError("unobserved mailbox cannot carry command data")
            return
        if self.packet is None:
            if self.trial_uid is not None or self.mailbox_sha256 is not None:
                raise ValueError("missing mailbox cannot carry command metadata")
            return
        if not isinstance(self.packet, HostPacket):
            raise ValueError("mailbox packet must be a HostPacket")
        if (
            not isinstance(self.trial_uid, str)
            or len(self.trial_uid) != 64
            or any(char not in "0123456789abcdef" for char in self.trial_uid)
        ):
            raise ValueError("mailbox trial_uid must be 64 lowercase hex characters")
        if (
            not isinstance(self.mailbox_sha256, str)
            or len(self.mailbox_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.mailbox_sha256)
        ):
            raise ValueError("mailbox SHA-256 must be 64 lowercase hex characters")

    @classmethod
    def unobserved(cls) -> "MailboxObservation":
        return cls(False)

    @classmethod
    def missing(cls) -> "MailboxObservation":
        return cls(True)

    @classmethod
    def from_command(cls, command: Any | None) -> "MailboxObservation":
        if command is None:
            return cls.missing()
        packet = getattr(command, "packet", None)
        binding = getattr(command, "binding", None)
        return cls(
            True,
            packet=packet,
            trial_uid=getattr(binding, "trial_uid", None),
            mailbox_sha256=getattr(command, "sha256", None),
        )

    def matches(self, packet: HostPacket, *, trial_uid: str) -> bool:
        return self.packet == packet and self.trial_uid == trial_uid


def _campaign_identity(supervisor: CampaignSupervisor) -> CampaignIdentity:
    return CampaignIdentity(
        campaign_id=supervisor.campaign.campaign_id,
        campaign_epoch=supervisor.campaign.campaign_epoch,
        campaign_fingerprint=supervisor.campaign.campaign_fingerprint,
        backend_id=supervisor.backend_id,
        source_fingerprint=supervisor.source_fingerprint,
        config_fingerprint=supervisor.config_fingerprint,
    )


def _dispatch_command_name(command: HostCommand) -> str:
    if command is HostCommand.ARM:
        return "arm"
    if command is HostCommand.ACK_BUNDLE:
        return "ack_bundle"
    raise CoordinatorError("only ARM and ACK have dispatch receipts")


def _receipt_from_observation(
    observation: MailboxObservation,
    packet: HostPacket,
    *,
    trial_uid: str,
) -> AdvisoryDispatchReceipt:
    if not observation.observed or not observation.matches(
        packet, trial_uid=trial_uid
    ):
        raise CoordinatorError("mailbox read-back differs from the dispatched packet")
    assert observation.mailbox_sha256 is not None
    return AdvisoryDispatchReceipt(
        trial_uid=trial_uid,
        campaign_epoch=packet.campaign_epoch,
        trial_id=packet.trial_id,
        command=_dispatch_command_name(packet.command),
        candidate_token=packet.candidate_token,
        execution_profile_integer_id=packet.execution_profile_id,
        command_seq=packet.command_seq,
        mailbox_sha256=observation.mailbox_sha256,
    )


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number is forbidden: {value}")
    return parsed


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def _read_reference(reference: JournalReference, *, role: str) -> tuple[bytes, Any]:
    path = Path(reference.path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RecoveryError(f"{role} is missing, unsafe, or a symlink: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RecoveryError(f"{role} must be a singly-linked regular file")
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RecoveryError(f"{role} disappeared while being verified") from exc
    identities = {
        (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)
        for row in (before, after, current)
    }
    if len(identities) != 1:
        raise RecoveryError(f"{role} changed while being verified")
    if digest.hexdigest() != reference.sha256:
        raise RecoveryError(f"{role} bytes differ from the journal SHA-256")
    encoded = b"".join(chunks)
    try:
        payload = json.loads(
            encoded.decode("utf-8"),
            parse_constant=_reject_constant,
            parse_float=_finite_float,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RecoveryError(f"{role} is not strict finite JSON: {exc}") from exc
    return encoded, payload


def _reference_for(path: Path, *, reference_id: str, role: str) -> JournalReference:
    if not isinstance(path, Path) or not path.is_absolute():
        raise CoordinatorError(f"{role} path must be absolute")
    provisional = JournalReference(reference_id, str(path), "0" * 64)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CoordinatorError(f"{role} is missing, unsafe, or a symlink: {path}") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise CoordinatorError(f"{role} must be a singly-linked regular file")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    current = path.stat(follow_symlinks=False)
    identities = {
        (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)
        for row in (before, after, current)
    }
    if len(identities) != 1:
        raise CoordinatorError(f"{role} changed while being hashed")
    return replace(provisional, sha256=digest.hexdigest())


def _verify_infra_abort_evidence(fate: TerminalFate) -> None:
    reference = fate.evidence
    if fate.kind != "infra_aborted_consumed" or reference is None:
        return
    _, marker = _read_reference(reference, role="infra-abort completion marker")
    if not isinstance(marker, Mapping) or any(
        (
            marker.get("capture_closed") is not True,
            marker.get("immutable") is not True,
            not isinstance(marker.get("source_files"), list),
        )
    ):
        raise RecoveryError("infra-abort completion marker is not immutable/closed")
    rows = {
        row.get("path"): row
        for row in marker["source_files"]
        if isinstance(row, Mapping) and isinstance(row.get("path"), str)
    }
    partial_rel = f"autotune_trials/{fate.trial.trial_uid}/capture.csv.part"
    for relative in ("summary.json", partial_rel):
        row = rows.get(relative)
        expected_sha = None if not isinstance(row, Mapping) else row.get("sha256")
        if not isinstance(expected_sha, str):
            raise RecoveryError(f"infra-abort marker lacks {relative}")
        path = Path(reference.path).parent / relative
        actual = _reference_for(
            path,
            reference_id=expected_sha,
            role=f"infra-abort {relative}",
        )
        if actual.sha256 != expected_sha:
            raise RecoveryError(f"infra-abort {relative} digest changed")
        if relative == "summary.json":
            _, summary = _read_reference(actual, role="infra-abort summary")
            if not isinstance(summary, Mapping) or summary.get("stop_reason") != "signal_sigint":
                raise RecoveryError("infra-abort summary stop reason is not signal_sigint")


def _verify_trial_brief_evidence(fate: TerminalFate, *, required: bool) -> None:
    if fate.kind not in {"ack_consumed", "direct_ready_completed"}:
        return
    reference = fate.evidence
    if reference is None:
        if required:
            raise RecoveryError("codex batch terminal fate lacks durable TrialBrief")
        return
    role = (
        "direct-ready TrialBrief"
        if fate.kind == "direct_ready_completed"
        else "post-ACK TrialBrief"
    )
    encoded, document = _read_reference(reference, role=role)
    if not isinstance(document, Mapping) or any(
        (
            encoded != runtime_canonical_json_bytes(document) + b"\n",
            document.get("publication_uid") != reference.reference_id,
            document.get("trial_uid") != fate.trial.trial_uid,
            document.get("publication_unique") is not True,
            not isinstance(document.get("optimizer_eligible"), bool),
            (
                fate.kind == "direct_ready_completed"
                and document.get("protocol") != "v3_direct_arm_v1"
            ),
        )
    ):
        raise RecoveryError("TrialBrief evidence is not exact and closed")


def _candidate_from_payload(payload: Any) -> ForceCandidate:
    if not isinstance(payload, Mapping):
        raise RecoveryError("history candidate is not an object")
    try:
        candidate = ForceCandidate(
            target_force_n=payload["target_force_n"],
            force_p_gain=payload["force_p_gain"],
            force_i_gain=payload["force_i_gain"],
            force_damping=payload["force_damping"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryError(f"history candidate is invalid: {exc}") from exc
    if canonical_json_bytes(candidate.payload()) != canonical_json_bytes(dict(payload)):
        raise RecoveryError("history candidate payload is not canonical")
    return candidate


def _campaign_from_payload(payload: Any) -> CampaignSpec:
    if not isinstance(payload, Mapping):
        raise RecoveryError("history campaign is not an object")
    candidate = dict(payload)
    if candidate.get("f0_shadow_reaction_normal_base") is not None:
        candidate["f0_shadow_reaction_normal_base"] = tuple(
            candidate["f0_shadow_reaction_normal_base"]
        )
    try:
        campaign = CampaignSpec(**candidate)
    except (TypeError, ValueError) as exc:
        raise RecoveryError(f"history campaign is invalid: {exc}") from exc
    return campaign


def _trial_from_payload(payload: Any) -> TrialSpec:
    if not isinstance(payload, Mapping):
        raise RecoveryError("history trial is not an object")
    try:
        attestation_payload = payload["search_attestation"]
        trial = TrialSpec(
            campaign=_campaign_from_payload(payload["campaign"]),
            trial_id=payload["trial_id"],
            candidate_token=payload["candidate_token"],
            command_seq=payload["command_seq"],
            plant_epoch=payload["plant_epoch"],
            candidate=_candidate_from_payload(payload["candidate"]),
            execution_profile=ExecutionProfile(**payload["execution_profile"]),
            backend_id=payload["backend_id"],
            source_fingerprint=payload["source_fingerprint"],
            config_fingerprint=payload["config_fingerprint"],
            transition=TrialTransition.from_payload(payload["transition"]),
            search_attestation=(
                None
                if attestation_payload is None
                else SearchAttestation.from_payload(attestation_payload)
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryError(f"history trial is invalid: {exc}") from exc
    if canonical_json_bytes(trial.payload()) != canonical_json_bytes(dict(payload)):
        raise RecoveryError("history trial payload is not canonical")
    return trial


def _evaluation_from_payload(payload: Any) -> Evaluation:
    if not isinstance(payload, Mapping):
        raise RecoveryError("history evaluation is not an object")
    try:
        evaluation = Evaluation(
            trial_uid=payload["trial_uid"],
            backend_id=payload["backend_id"],
            eligible=payload["eligible"],
            disposition=TrialDisposition(payload["disposition"]),
            objective_mae_n=payload["objective_mae_n"],
            force_bias_n=payload["force_bias_n"],
            force_std_n=payload["force_std_n"],
            coverage_12_plus_minus_1_ratio=payload[
                "coverage_12_plus_minus_1_ratio"
            ],
            complete_bins=payload["complete_bins"],
            safe_closure=payload["safe_closure"],
            structural_failures=tuple(payload["structural_failures"]),
            metrics=payload["metrics"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryError(f"history evaluation is invalid: {exc}") from exc
    if canonical_json_bytes(evaluation.history_payload()) != canonical_json_bytes(
        dict(payload)
    ):
        raise RecoveryError("history evaluation payload is not canonical")
    return evaluation


def _trial_reference(trial: TrialSpec, trial_dir: Path) -> JournalReference:
    if trial_dir.is_symlink():
        raise CoordinatorError("trial registrar returned a symlink")
    path = trial_dir / "trial_spec.json" if trial_dir.is_dir() else trial_dir
    if path.name != "trial_spec.json" or path.parent.name != trial.trial_uid:
        raise CoordinatorError("trial registrar returned a path outside the exact trial_uid")
    if path.is_symlink():
        raise CoordinatorError("trial_spec.json must not be a symlink")
    reference = _reference_for(
        path.absolute(), reference_id=trial.trial_uid, role="trial_spec.json"
    )
    _, payload = _read_reference(reference, role="trial_spec.json")
    if not isinstance(payload, dict):
        raise CoordinatorError("trial_spec.json must contain an object")
    candidate = dict(payload)
    candidate.pop("provenance_run_dir", None)
    if canonical_json_bytes(candidate) != canonical_json_bytes(trial.payload()):
        raise CoordinatorError("trial_spec.json differs from the exact TrialSpec")
    return reference


def _trial_reference_from_history(
    row: Mapping[str, Any], trial: TrialSpec
) -> JournalReference:
    provenance = row.get("artifact_provenance")
    if not isinstance(provenance, Mapping) or not provenance:
        raise RecoveryError("prior history lacks durable artifact provenance")
    roots: set[Path] = set()
    for role, artifact in provenance.items():
        if not isinstance(role, str) or not isinstance(artifact, Mapping):
            raise RecoveryError("prior artifact provenance is invalid")
        raw_path = artifact.get("path")
        digest = artifact.get("sha256")
        if not isinstance(raw_path, str) or not isinstance(digest, str):
            raise RecoveryError("prior artifact provenance lacks path/SHA-256")
        path = Path(raw_path)
        try:
            if any(
                (
                    not path.is_absolute(),
                    path.name != role,
                    path.parent.name != digest,
                    path.parent.parent.name != "sha256",
                    path.parent.parent.parent.name != "artifacts",
                )
            ):
                raise RecoveryError("prior artifact path is not a canonical store path")
            roots.add(path.parents[3])
        except IndexError as exc:
            raise RecoveryError("prior artifact path is too shallow") from exc
    if len(roots) != 1:
        raise RecoveryError("prior artifacts do not identify one canonical store")
    trial_dir = next(iter(roots)) / "trials" / trial.trial_uid
    try:
        return _trial_reference(trial, trial_dir)
    except CoordinatorError as exc:
        raise RecoveryError(
            "prior history lacks its exact durable trial_spec.json"
        ) from exc


def _trial_from_spec_reference(reference: JournalReference) -> TrialSpec:
    _, payload = _read_reference(reference, role="journal trial_spec.json")
    if not isinstance(payload, dict):
        raise RecoveryError("journal trial_spec.json is not an object")
    candidate = dict(payload)
    candidate.pop("provenance_run_dir", None)
    return _trial_from_payload(candidate)


def _trial_from_reference(cursor: TrialCursor) -> TrialSpec:
    trial = _trial_from_spec_reference(cursor.trial_spec)
    if any(
        (
            trial.trial_uid != cursor.trial_uid,
            trial.candidate.candidate_uid != cursor.candidate_uid,
            trial.trial_id != cursor.trial_id,
            trial.candidate_token != cursor.candidate_token,
            trial.command_seq != cursor.arm_command_seq,
            execution_profile_integer_id(trial.execution_profile)
            != cursor.execution_profile_integer_id,
        )
    ):
        raise RecoveryError("journal trial cursor differs from trial_spec.json")
    return trial


def _bundle_reference(path: Path, trial: TrialSpec) -> tuple[JournalReference, Mapping[str, Any]]:
    if path.name != "immutable_trial_bundle.json" or path.parent.name != trial.trial_uid:
        raise CoordinatorError("immutable bundle path differs from the pending trial_uid")
    if path.is_symlink():
        raise CoordinatorError("immutable bundle must not be a symlink")
    provisional = _reference_for(path.absolute(), reference_id="0" * 64, role="immutable bundle")
    _, payload = _read_reference(provisional, role="immutable bundle")
    if not isinstance(payload, dict):
        raise CoordinatorError("immutable bundle must contain an object")
    history_identity = payload.get("history_identity")
    if not isinstance(history_identity, str):
        raise CoordinatorError("immutable bundle lacks a history identity")
    reference = replace(provisional, reference_id=history_identity)
    if canonical_json_bytes(payload.get("trial")) != canonical_json_bytes(trial.payload()):
        raise CoordinatorError("immutable bundle differs from the exact pending TrialSpec")
    capture_payload = payload.get("capture")
    evaluation_payload = payload.get("evaluation")
    try:
        capture = CaptureManifest(**capture_payload)
        evaluation = _evaluation_from_payload(evaluation_payload)
    except (TypeError, ValueError, RecoveryError) as exc:
        raise CoordinatorError(f"immutable bundle closure is invalid: {exc}") from exc
    if any(
        (
            capture.trial_uid != trial.trial_uid,
            capture.backend_id != trial.backend_id,
            capture.candidate_token != trial.candidate_token,
            capture.safe_closure_evidence.returned_safe is not capture.returned_safe,
            evaluation.trial_uid != trial.trial_uid,
            evaluation.backend_id != trial.backend_id,
            evaluation.safe_closure is not capture.returned_safe,
        )
    ):
        raise CoordinatorError("immutable bundle identity/safe closure differs")
    return reference, payload


def _cursor(
    intent: TrialIntent,
    reference: JournalReference,
    *,
    retry_release: RetryRelease | None = None,
) -> TrialCursor:
    trial = intent.trial
    return TrialCursor(
        trial_uid=trial.trial_uid,
        candidate_uid=trial.candidate.candidate_uid,
        trial_id=trial.trial_id,
        candidate_token=trial.candidate_token,
        arm_command_seq=trial.command_seq,
        execution_profile_integer_id=intent.execution_profile_integer_id,
        trial_spec=reference,
        retry_release=retry_release,
    )


class CampaignCoordinator:
    """The only Step5d-native boundary that returns delivery-eligible packets."""

    def __init__(
        self,
        *,
        supervisor: CampaignSupervisor,
        journal: SupervisorJournal,
        latest: JournalEntry | None = None,
        trial_spec_references: Mapping[str, JournalReference] | None = None,
        bundle_references: Mapping[str, JournalReference] | None = None,
        terminal_fates: Sequence[TerminalFate] | None = None,
    ) -> None:
        if not isinstance(supervisor, CampaignSupervisor):
            raise ValueError("supervisor must be CampaignSupervisor")
        if not isinstance(journal, SupervisorJournal):
            raise ValueError("journal must be SupervisorJournal")
        self.supervisor = supervisor
        self.journal = journal
        self.latest = latest
        self._trial_spec_references = dict(trial_spec_references or {})
        self._bundle_references = dict(bundle_references or {})
        self._pending_terminal_by_trial: dict[str, tuple[int, str | None]] = {}
        self._dispatch_receipt = (
            None if latest is None else latest.state.dispatch_receipt
        )
        if latest is not None and terminal_fates is not None:
            raise ValueError("terminal fates cannot override a loaded journal")
        self._terminal_fates = tuple(
            latest.state.terminal_fates
            if latest is not None
            else terminal_fates or ()
        )
        self._poisoned = False

    @property
    def terminal_candidate_uids(self) -> frozenset[str]:
        return frozenset(fate.trial.candidate_uid for fate in self._terminal_fates)

    def _require_healthy(self) -> None:
        if self._poisoned:
            raise CoordinatorError("coordinator is fail-closed; restart from durable journal")

    def _intent_cursor(
        self,
        intent: TrialIntent,
        *,
        bind_retry_release: bool = False,
    ) -> TrialCursor:
        reference = self._trial_spec_references.get(intent.trial.trial_uid)
        if reference is None:
            raise CoordinatorError("trial has no durable trial_spec store reference")
        retry_release: RetryRelease | None = None
        if bind_retry_release and intent.retry_kind == "infrastructure":
            pending = (
                self.latest.state.pending_retry
                if self.latest is not None
                else None
            )
            if pending is not None and pending.kind == "infrastructure":
                origin = pending.origin_trial
                retry_release = RetryRelease(
                    origin_trial_uid=origin.trial_uid,
                    trial_id=origin.trial_id,
                    candidate_token=origin.candidate_token,
                    execution_profile_integer_id=origin.execution_profile_integer_id,
                    consumed_command_seq=pending.last_consumed_command_seq,
                    terminal_reason=pending.terminal_reason,
                    host_cause=pending.host_cause,
                )
            elif (
                self.latest is not None
                and self.latest.state.active_trial is not None
                and self.latest.state.active_trial.trial_uid == intent.trial.trial_uid
            ):
                retry_release = self.latest.state.active_trial.retry_release
            if retry_release is None:
                raise CoordinatorError(
                    "infrastructure retry ARM lacks its durable release origin"
                )
        return _cursor(intent, reference, retry_release=retry_release)

    def _state_from_snapshot(
        self, snapshot: SupervisorRecoverySnapshot
    ) -> JournalState:
        active = (
            None
            if snapshot.active is None
            else self._intent_cursor(snapshot.active, bind_retry_release=True)
        )
        pending_ack: PendingAck | None = None
        if snapshot.pending_ack is not None:
            intent, post_phase = snapshot.pending_ack
            if snapshot.prepared_ack is None:
                raise CoordinatorError("wait_ack cannot be persisted before ACK sequence reservation")
            bundle = self._bundle_references.get(intent.trial.trial_uid)
            if bundle is None:
                raise CoordinatorError("wait_ack cannot be persisted without immutable bundle hash")
            terminal = self._pending_terminal_by_trial.get(intent.trial.trial_uid)
            if terminal is None and self.latest is not None:
                prior = self.latest.state.pending_ack
                if prior is not None and prior.trial.trial_uid == intent.trial.trial_uid:
                    terminal = (prior.terminal_reason, prior.host_cause)
            if terminal is None:
                raise CoordinatorError(
                    "wait_ack cannot be persisted without terminal reason/host cause"
                )
            pending_ack = PendingAck(
                trial=self._intent_cursor(intent),
                ack_command_seq=snapshot.prepared_ack.command_seq,
                immutable_bundle=bundle,
                post_ack_phase=post_phase.value,
                terminal_reason=terminal[0],
                host_cause=terminal[1],
            )
        pending_advance: JournalPendingAdvance | None = None
        if snapshot.pending_advance is not None:
            pending = snapshot.pending_advance
            intent = pending.intent
            bundle = self._bundle_references.get(intent.trial.trial_uid)
            if bundle is None:
                raise CoordinatorError(
                    "wait_direct_commit cannot be persisted without immutable bundle hash"
                )
            terminal = self._pending_terminal_by_trial.get(intent.trial.trial_uid)
            if terminal is None and self.latest is not None:
                prior = self.latest.state.pending_advance
                if (
                    prior is not None
                    and prior.trial.trial_uid == intent.trial.trial_uid
                ):
                    terminal = (prior.terminal_reason, prior.host_cause)
            if terminal is None:
                raise CoordinatorError(
                    "wait_direct_commit lacks terminal reason/host cause"
                )
            pending_advance = JournalPendingAdvance(
                trial=self._intent_cursor(intent),
                immutable_bundle=bundle,
                post_commit_phase=pending.post_commit_phase.value,
                terminal_reason=terminal[0],
                host_cause=terminal[1],
            )
        pending_retry: PendingRetry | None = None
        if snapshot.pending_retry is not None:
            candidate, token, kind, retry_source = snapshot.pending_retry
            origin: TrialIntent | None = None
            if snapshot.pending_ack is not None:
                origin = snapshot.pending_ack[0]
            elif snapshot.pending_advance is not None:
                origin = snapshot.pending_advance.intent
            elif snapshot.active is not None:
                origin = snapshot.active
            else:
                reference = self._trial_spec_references.get(retry_source.trial_uid)
                if reference is not None:
                    trial = _trial_from_spec_reference(reference)
                    if (
                        trial_source_from_trial(trial) != retry_source
                        or trial.candidate != candidate
                        or trial.candidate_token != token
                    ):
                        raise CoordinatorError(
                            "pending retry source differs from durable TrialSpec"
                        )
                    origin = TrialIntent(
                        trial,
                        execution_profile_integer_id(trial.execution_profile),
                        {"selection": "recovered_retry_origin"},
                        kind,
                    )
            if origin is None:
                raise CoordinatorError("pending retry lacks a durable origin trial")
            if (
                trial_source_from_trial(origin.trial) != retry_source
                or origin.trial.candidate != candidate
                or origin.trial.candidate_token != token
            ):
                raise CoordinatorError(
                    "pending retry does not bind its exact durable origin trial"
                )
            consumed = (
                origin.trial.command_seq
                if snapshot.pending_ack is not None
                or snapshot.pending_advance is not None
                else snapshot.command_seq
            )
            retry_terminal: tuple[int, str | None] | None = None
            if (
                pending_ack is not None
                and pending_ack.trial.trial_uid == origin.trial.trial_uid
            ):
                retry_terminal = (
                    pending_ack.terminal_reason,
                    pending_ack.host_cause,
                )
            elif (
                pending_advance is not None
                and pending_advance.trial.trial_uid == origin.trial.trial_uid
            ):
                retry_terminal = (
                    pending_advance.terminal_reason,
                    pending_advance.host_cause,
                )
            elif self.latest is not None:
                prior_ack = self.latest.state.pending_ack
                prior_retry = self.latest.state.pending_retry
                if (
                    prior_ack is not None
                    and prior_ack.trial.trial_uid == origin.trial.trial_uid
                ):
                    retry_terminal = (
                        prior_ack.terminal_reason,
                        prior_ack.host_cause,
                    )
                elif (
                    prior_retry is not None
                    and prior_retry.origin_trial.trial_uid
                    == origin.trial.trial_uid
                ):
                    retry_terminal = (
                        prior_retry.terminal_reason,
                        prior_retry.host_cause,
                    )
            if retry_terminal is None:
                retry_terminal = (13 if kind == "code_fix" else 1, None)
            pending_retry = PendingRetry(
                origin_trial=self._intent_cursor(origin),
                kind=kind,
                last_consumed_command_seq=consumed,
                terminal_reason=retry_terminal[0],
                host_cause=retry_terminal[1],
            )
        governor_probe = None
        if snapshot.governor_probe is not None:
            probe = snapshot.governor_probe
            governor_probe = GovernorProbe(
                layer=probe.layer,
                profile_before_id=probe.profile_a.profile_id,
                profile_after_id=probe.profile_b.profile_id,
                stage=probe.stage,
                force_candidate_uid=probe.force_candidate.candidate_uid,
                plant_epoch=probe.identity_a.plant_epoch,
                trial_a_uid=probe.identity_a.trial_uid,
                trial_b_uid=(
                    None if probe.identity_b is None else probe.identity_b.trial_uid
                ),
                trial_a_prime_uid=(
                    None
                    if probe.identity_a_prime is None
                    else probe.identity_a_prime.trial_uid
                ),
            )
        references = tuple(
            self._bundle_references[uid] for uid in sorted(self._bundle_references)
        )
        return JournalState(
            campaign=_campaign_identity(self.supervisor),
            phase=snapshot.phase.value,
            high_water=HighWaterMarks(
                snapshot.trial_counter,
                snapshot.command_seq,
                len(snapshot.candidate_tokens),
            ),
            candidate_tokens=snapshot.candidate_tokens,
            plant_epoch=self.supervisor.plant_epoch,
            execution_profile_id=self.supervisor.execution_profile.profile_id,
            execution_profile_integer_id=execution_profile_integer_id(
                self.supervisor.execution_profile
            ),
            active_trial=active,
            pending_ack=pending_ack,
            pending_advance=pending_advance,
            pending_retry=pending_retry,
            cooldown_remaining=snapshot.cooldown_remaining,
            governor_probe=governor_probe,
            observation_references=references,
            history_references=references,
            dispatch_receipt=self._dispatch_receipt,
            terminal_fates=self._terminal_fates,
        )

    def _append_snapshot(self, snapshot: SupervisorRecoverySnapshot) -> JournalEntry:
        expected = 0 if self.latest is None else self.latest.revision
        entry = self.journal.append(
            self._state_from_snapshot(snapshot), expected_revision=expected
        )
        self.latest = entry
        return entry

    def persist_home(self) -> JournalEntry:
        self._require_healthy()
        if self.supervisor.phase is not CampaignPhase.HOME:
            raise CoordinatorError("only campaign home can be initialized")
        try:
            return self._append_snapshot(self.supervisor.recovery_snapshot())
        except Exception:
            self._poisoned = True
            raise

    def cancel_unconsumed_arm(
        self,
        tp_snapshot: TpSnapshot,
        *,
        mailbox_observation: MailboxObservation,
    ) -> TrialSpec:
        """Durably clear an ARM that the TP proves it never consumed."""

        self._require_healthy()
        if not isinstance(mailbox_observation, MailboxObservation):
            raise CoordinatorError("cancel requires a strict mailbox observation")
        if not mailbox_observation.observed:
            raise CoordinatorError("cancel requires an actual mailbox read")
        if self.latest is None or self.latest.state.active_trial is None:
            raise CoordinatorError("cancel requires one persisted active ARM")
        persisted = self.latest.state.active_trial
        if self._dispatch_receipt is not None:
            raise CoordinatorError("a dispatched ARM cannot be cancelled")
        if (
            mailbox_observation.packet is not None
            and mailbox_observation.packet.command_seq
            > tp_snapshot.consumed_command_seq
        ):
            raise CoordinatorError(
                "mailbox contains a command not proven consumed by the TP"
            )
        try:
            trial = self.supervisor.cancel_unconsumed_arm(
                tp_snapshot,
                persisted_trial_uid=persisted.trial_uid,
                has_dispatch_receipt=False,
            )
            self._terminal_fates = self._terminal_fates + (
                TerminalFate(
                    kind="cancelled_unconsumed",
                    trial=persisted,
                    command="arm",
                    command_seq=persisted.arm_command_seq,
                    tp_snapshot=tp_snapshot,
                ),
            )
            self._append_snapshot(self.supervisor.recovery_snapshot())
            return trial
        except Exception:
            self._poisoned = True
            raise

    def terminalize_consumed_infra_abort(
        self,
        tp_snapshot: TpSnapshot,
        *,
        evidence: JournalReference,
        persist: bool = True,
    ) -> TrialSpec:
        """Retire a physically attempted ARM after immutable bridge-stop proof."""

        self._require_healthy()
        if type(persist) is not bool:
            raise CoordinatorError("persist must be bool")
        if not isinstance(evidence, JournalReference):
            raise CoordinatorError("infra abort requires immutable stop evidence")
        if self.latest is None or self.latest.state.active_trial is None:
            raise CoordinatorError("infra abort requires one persisted active ARM")
        persisted = self.latest.state.active_trial
        receipt = self._dispatch_receipt
        if receipt is None:
            raise CoordinatorError("infra abort requires the durable ARM dispatch receipt")
        if any(
            (
                receipt.trial_uid != persisted.trial_uid,
                receipt.trial_id != persisted.trial_id,
                receipt.command != "arm",
                receipt.candidate_token != persisted.candidate_token,
                receipt.execution_profile_integer_id
                != persisted.execution_profile_integer_id,
                receipt.command_seq != persisted.arm_command_seq,
            )
        ):
            raise CoordinatorError("infra abort dispatch receipt differs from active ARM")
        try:
            trial = self.supervisor.terminalize_consumed_infra_abort(
                tp_snapshot,
                persisted_trial_uid=persisted.trial_uid,
            )
            self._terminal_fates = self._terminal_fates + (
                TerminalFate(
                    kind="infra_aborted_consumed",
                    trial=persisted,
                    command="arm",
                    command_seq=persisted.arm_command_seq,
                    tp_snapshot=tp_snapshot,
                    dispatch_receipt=receipt,
                    evidence=evidence,
                ),
            )
            self._dispatch_receipt = None
            if persist:
                self._append_snapshot(self.supervisor.recovery_snapshot())
            return trial
        except Exception:
            self._poisoned = True
            raise

    def issue_arm(
        self,
        registrar: TrialRegistrar,
        *,
        provenance_run_dir: Path | None = None,
        require_cuda_botorch: bool = True,
        cuda_fit_mode: str = "serial",
        parallel_cuda_verified: bool = False,
        search_attestations: Sequence[SearchAttestation] = (),
        forced_candidate: ForceCandidate | None = None,
        allow_archived_code_fix_replay: bool = False,
        allow_exact_incomplete_batch_retry: bool = False,
        allow_fresh_exact_batch_bootstrap: bool = False,
        attempt_started: Callable[[TrialSpec], None] | None = None,
    ) -> HostPacket:
        """Register TrialSpec, fsync ARM intent, then return its HostPacket."""

        self._require_healthy()
        try:
            intent = self.supervisor.next_trial(
                require_cuda_botorch=require_cuda_botorch,
                cuda_fit_mode=cuda_fit_mode,
                parallel_cuda_verified=parallel_cuda_verified,
                search_attestations=search_attestations,
                forced_candidate=forced_candidate,
                allow_archived_code_fix_replay=allow_archived_code_fix_replay,
                allow_exact_incomplete_batch_retry=(
                    allow_exact_incomplete_batch_retry
                ),
                allow_fresh_exact_batch_bootstrap=(
                    allow_fresh_exact_batch_bootstrap
                ),
                forbidden_candidate_uids={
                    fate.trial.candidate_uid
                    for fate in self._terminal_fates
                    if fate.kind == "infra_aborted_consumed"
                },
            )
            transition_source = intent.trial.transition.source
            if transition_source is not None:
                source_reference = self._trial_spec_references.get(
                    transition_source.trial_uid
                )
                if source_reference is None:
                    raise CoordinatorError(
                        "trial transition source lacks a durable TrialSpec reference"
                    )
                source_trial = _trial_from_spec_reference(source_reference)
                if trial_source_from_trial(source_trial) != transition_source:
                    raise CoordinatorError(
                        "trial transition source differs from durable TrialSpec bytes"
                    )
            trial_dir = registrar.register_trial(
                intent.trial, provenance_run_dir=provenance_run_dir
            )
            reference = _trial_reference(intent.trial, trial_dir)
            self._trial_spec_references[intent.trial.trial_uid] = reference
            if attempt_started is not None:
                attempt_started(intent.trial)
            self._append_snapshot(self.supervisor.recovery_snapshot())
            return host_packet_for_trial(
                intent.trial,
                command=HostCommand.ARM,
                execution_profile_id=intent.execution_profile_integer_id,
            )
        except Exception:
            self._poisoned = True
            raise

    def close_trial(self, **kwargs: Any) -> CloseDecision:
        self._require_healthy()
        return self.supervisor.close_trial(**kwargs)

    def issue_ack(
        self,
        immutable_bundle_path: Path,
        *,
        verified_resume_history: Sequence[Mapping[str, Any]],
    ) -> HostPacket:
        """Verify bundle bytes, fsync wait_ack intent, then return exact ACK."""

        self._require_healthy()
        snapshot = self.supervisor.recovery_snapshot()
        if snapshot.pending_ack is None:
            raise CoordinatorError("no closed trial is waiting for a bundle ACK")
        intent, _ = snapshot.pending_ack
        try:
            reference, bundle_payload = _bundle_reference(
                immutable_bundle_path, intent.trial
            )
            matching_rows = [
                row
                for row in verified_resume_history
                if isinstance(row, Mapping)
                and row.get("trial_uid") == intent.trial.trial_uid
            ]
            if len(matching_rows) != 1:
                raise CoordinatorError(
                    "ACK requires one exact row from read_resume_history()"
                )
            history_row = matching_rows[0]
            if any(
                (
                    history_row.get("history_identity") != reference.reference_id,
                    canonical_json_bytes(history_row.get("trial"))
                    != canonical_json_bytes(bundle_payload.get("trial")),
                    canonical_json_bytes(history_row.get("evaluation"))
                    != canonical_json_bytes(bundle_payload.get("evaluation")),
                )
            ):
                raise CoordinatorError(
                    "immutable bundle differs from verified resume history"
                )
            self._bundle_references[intent.trial.trial_uid] = reference
            capture_payload = bundle_payload.get("capture")
            if not isinstance(capture_payload, Mapping):
                raise CoordinatorError("immutable bundle capture payload is missing")
            terminal_reason = capture_payload.get("terminal_reason")
            host_cause = capture_payload.get("host_cause")
            if (
                isinstance(terminal_reason, bool)
                or not isinstance(terminal_reason, int)
                or terminal_reason <= 0
                or (host_cause is not None and not isinstance(host_cause, str))
            ):
                raise CoordinatorError(
                    "immutable bundle terminal reason/host cause is invalid"
                )
            self._pending_terminal_by_trial[intent.trial.trial_uid] = (
                terminal_reason,
                host_cause,
            )
            packet = self.supervisor.prepare_ack_packet()
            # The materialized transaction changes from ARM to ACK.  The ARM
            # dispatch receipt remains advisory and is no longer current.
            self._dispatch_receipt = None
            self._append_snapshot(self.supervisor.recovery_snapshot())
            return packet
        except Exception:
            self._poisoned = True
            raise

    def persist_direct_advance(
        self,
        immutable_bundle_path: Path,
        *,
        verified_resume_history: Sequence[Mapping[str, Any]],
    ) -> JournalEntry:
        """Fsync r006 PendingAdvance without reserving an ACK sequence."""

        self._require_healthy()
        snapshot = self.supervisor.recovery_snapshot()
        if snapshot.pending_advance is None:
            raise CoordinatorError("no closed trial is waiting for direct advance")
        if snapshot.pending_ack is not None or snapshot.prepared_ack is not None:
            raise CoordinatorError("direct advance cannot reuse legacy ACK state")
        intent = snapshot.pending_advance.intent
        try:
            reference, bundle_payload = _bundle_reference(
                immutable_bundle_path, intent.trial
            )
            matching_rows = [
                row
                for row in verified_resume_history
                if isinstance(row, Mapping)
                and row.get("trial_uid") == intent.trial.trial_uid
            ]
            if len(matching_rows) != 1:
                raise CoordinatorError(
                    "direct advance requires one exact cold-read history row"
                )
            history_row = matching_rows[0]
            if any(
                (
                    history_row.get("history_identity")
                    != reference.reference_id,
                    canonical_json_bytes(history_row.get("trial"))
                    != canonical_json_bytes(bundle_payload.get("trial")),
                    canonical_json_bytes(history_row.get("evaluation"))
                    != canonical_json_bytes(bundle_payload.get("evaluation")),
                )
            ):
                raise CoordinatorError(
                    "direct advance bundle differs from cold-read history"
                )
            capture_payload = bundle_payload.get("capture")
            if not isinstance(capture_payload, Mapping):
                raise CoordinatorError("direct advance bundle capture is missing")
            terminal_reason = capture_payload.get("terminal_reason")
            host_cause = capture_payload.get("host_cause")
            if terminal_reason != 1 or (
                host_cause is not None and not isinstance(host_cause, str)
            ):
                raise CoordinatorError(
                    "direct advance requires terminal-ready reason 1"
                )
            self._bundle_references[intent.trial.trial_uid] = reference
            self._pending_terminal_by_trial[intent.trial.trial_uid] = (
                terminal_reason,
                host_cause,
            )
            return self._append_snapshot(snapshot)
        except Exception:
            self._poisoned = True
            raise

    def complete_direct_ready(
        self,
        immutable_bundle_path: Path,
        *,
        verified_resume_history: Sequence[Mapping[str, Any]],
        tp_snapshot: TpSnapshot,
        trial_brief_admission: DirectTrialBriefAdmissionLike,
    ) -> ReconcileResult:
        """Persist r006 terminal completion without preparing an ACK command."""

        self._require_healthy()
        if self.latest is None or self.latest.state.pending_advance is None:
            raise CoordinatorError("direct-ready completion lacks PendingAdvance")
        persisted = self.latest.state.pending_advance.trial
        snapshot = self.supervisor.recovery_snapshot()
        if snapshot.pending_advance is None:
            raise CoordinatorError("direct-ready completion lacks closed policy outcome")
        if snapshot.pending_ack is not None or snapshot.prepared_ack is not None:
            raise CoordinatorError("direct-ready completion reused legacy ACK state")
        pending = snapshot.pending_advance
        intent = pending.intent
        post_phase = pending.post_commit_phase
        trial = intent.trial
        if any(
            (
                persisted.trial_uid != trial.trial_uid,
                tp_snapshot.campaign_epoch_echo != trial.campaign.campaign_epoch,
                tp_snapshot.trial_id_echo != trial.trial_id,
                tp_snapshot.candidate_token_echo != trial.candidate_token,
                tp_snapshot.execution_profile_integer_id_echo
                != intent.execution_profile_integer_id,
                tp_snapshot.consumed_command_seq != trial.command_seq,
                tp_snapshot.terminal_reason != 1,
                tp_snapshot.state
                not in {"READY_NEAR", "READY_HOME_CLOSED"},
            )
        ):
            raise CoordinatorError("direct-ready TP snapshot differs from active ARM")
        try:
            reference, bundle_payload = _bundle_reference(
                immutable_bundle_path, trial
            )
            matching_rows = [
                row
                for row in verified_resume_history
                if isinstance(row, Mapping)
                and row.get("trial_uid") == trial.trial_uid
            ]
            if len(matching_rows) != 1:
                raise CoordinatorError(
                    "direct-ready completion requires one cold-read history row"
                )
            history_row = matching_rows[0]
            if any(
                (
                    history_row.get("history_identity") != reference.reference_id,
                    canonical_json_bytes(history_row.get("trial"))
                    != canonical_json_bytes(bundle_payload.get("trial")),
                    canonical_json_bytes(history_row.get("evaluation"))
                    != canonical_json_bytes(bundle_payload.get("evaluation")),
                )
            ):
                raise CoordinatorError(
                    "immutable bundle differs from direct-ready cold-read history"
                )
            matching_outcomes = [
                outcome
                for outcome in snapshot.outcome_timeline
                if outcome.evaluation.trial_uid == trial.trial_uid
            ]
            if len(matching_outcomes) != 1:
                raise CoordinatorError("direct-ready policy lacks one exact outcome")
            brief_reference = _direct_trial_brief_reference(
                trial_brief_admission,
                trial_uid=trial.trial_uid,
                arm_command_seq=trial.command_seq,
                optimizer_eligible=matching_outcomes[0].eligible,
            )
            self._bundle_references[trial.trial_uid] = reference
            capture_payload = bundle_payload.get("capture")
            if not isinstance(capture_payload, Mapping):
                raise CoordinatorError("immutable bundle capture payload is missing")
            terminal_reason = capture_payload.get("terminal_reason")
            host_cause = capture_payload.get("host_cause")
            if terminal_reason != 1 or (
                host_cause is not None and not isinstance(host_cause, str)
            ):
                raise CoordinatorError("direct-ready bundle terminal reason differs")
            self._pending_terminal_by_trial[trial.trial_uid] = (
                terminal_reason,
                host_cause,
            )
            self._terminal_fates = self._terminal_fates + (
                TerminalFate(
                    kind="direct_ready_completed",
                    trial=persisted,
                    command="arm",
                    command_seq=persisted.arm_command_seq,
                    tp_snapshot=tp_snapshot,
                    dispatch_receipt=self._dispatch_receipt,
                    evidence=brief_reference,
                ),
            )
            predicted = replace(
                snapshot,
                phase=post_phase,
                pending_advance=None,
            )
            self._dispatch_receipt = None
            self._append_snapshot(predicted)
            self.supervisor.confirm_direct_ready(trial.trial_uid)
            return ReconcileResult(
                ReconcileDecision(
                    ReconcileAction.PERSIST_DIRECT_READY,
                    "direct_ready_bundle_and_trial_brief_persisted",
                    trial.command_seq,
                ),
                None,
            )
        except Exception:
            self._poisoned = True
            raise

    @staticmethod
    def _validate_prepared_trial(
        prepared: PreparedTrialLike, trial: TrialSpec
    ) -> None:
        if getattr(prepared, "trial", None) != trial:
            raise CoordinatorError("PreparedTrial does not bind the durable TrialSpec")
        environment = getattr(prepared, "environment", None)
        arguments = getattr(prepared, "runner_arguments", None)
        if not isinstance(environment, Mapping) or not isinstance(arguments, tuple):
            raise CoordinatorError("PreparedTrial runtime binding is incomplete")
        expected_strings = {
            "STEP5D_AUTOTUNE_TRIAL_UID": trial.trial_uid,
            "STEP5D_AUTOTUNE_CAMPAIGN_EPOCH": str(trial.campaign.campaign_epoch),
            "STEP5D_AUTOTUNE_TRIAL_ID": str(trial.trial_id),
            "STEP5D_AUTOTUNE_CANDIDATE_TOKEN": str(trial.candidate_token),
            "STEP5D_AUTOTUNE_EXECUTION_PROFILE_ID": str(
                execution_profile_integer_id(trial.execution_profile)
            ),
            "STEP5D_AUTOTUNE_COMMAND_SEQ": str(trial.command_seq),
        }
        for name, expected in expected_strings.items():
            if environment.get(name) != expected:
                raise CoordinatorError(f"PreparedTrial environment differs at {name}")
        expected_numbers = {
            "STEP5D_AUTOTUNE_FORCE_P": trial.candidate.force_p_gain,
            "STEP5D_AUTOTUNE_FORCE_I": trial.candidate.force_i_gain,
            "STEP5D_AUTOTUNE_FORCE_DAMPING": trial.candidate.force_damping,
            "STEP5D_AUTOTUNE_NORMAL_RATE_RAD_S": (
                trial.execution_profile.normal_max_rate_rad_s
            ),
            "STEP5D_AUTOTUNE_HOST_SLEW_RAD_S2": (
                trial.execution_profile.host_qdot_slew_rad_s2
            ),
            "STEP5D_AUTOTUNE_SPEEDJ_ACCELERATION_RAD_S2": (
                trial.execution_profile.tp_speedj_accel_rad_s2
            ),
        }
        for name, expected in expected_numbers.items():
            try:
                actual = float(environment[name])
            except (KeyError, TypeError, ValueError) as exc:
                raise CoordinatorError(
                    f"PreparedTrial environment lacks numeric {name}"
                ) from exc
            if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-12):
                raise CoordinatorError(f"PreparedTrial environment differs at {name}")
        if "BRIDGE_NORMAL_FILTER_ALPHA" in environment:
            raise CoordinatorError("PreparedTrial reintroduced forbidden normal_filter_alpha")
        expected_arguments = {
            "--step5d-autotune-force-p": environment["STEP5D_AUTOTUNE_FORCE_P"],
            "--step5d-autotune-force-i": environment["STEP5D_AUTOTUNE_FORCE_I"],
            "--step5d-autotune-force-damping": environment[
                "STEP5D_AUTOTUNE_FORCE_DAMPING"
            ],
            "--step5d-autotune-normal-rate-rad-s": environment[
                "STEP5D_AUTOTUNE_NORMAL_RATE_RAD_S"
            ],
            "--step5d-autotune-host-slew-rad-s2": environment[
                "STEP5D_AUTOTUNE_HOST_SLEW_RAD_S2"
            ],
            "--step5d-autotune-speedj-acceleration-rad-s2": environment[
                "STEP5D_AUTOTUNE_SPEEDJ_ACCELERATION_RAD_S2"
            ],
            "--step5d-autotune-campaign-epoch": str(
                trial.campaign.campaign_epoch
            ),
            "--step5d-autotune-trial-id": str(trial.trial_id),
            "--step5d-autotune-command": "1",
            "--step5d-autotune-candidate-token": str(trial.candidate_token),
            "--step5d-autotune-execution-profile-id": str(
                execution_profile_integer_id(trial.execution_profile)
            ),
            "--step5d-autotune-command-sequence": str(trial.command_seq),
        }
        for flag, expected in expected_arguments.items():
            indices = [index for index, value in enumerate(arguments) if value == flag]
            if len(indices) != 1 or indices[0] + 1 >= len(arguments):
                raise CoordinatorError(f"PreparedTrial runner args differ at {flag}")
            if arguments[indices[0] + 1] != expected:
                raise CoordinatorError(f"PreparedTrial runner args differ at {flag}")
        if any("normal-filter-alpha" in value for value in arguments):
            raise CoordinatorError("PreparedTrial runner args contain forbidden alpha")
        frozen = getattr(prepared, "frozen", None)
        if frozen is None or any(
            (
                getattr(frozen, "source_fingerprint", None)
                != trial.source_fingerprint,
                getattr(frozen, "config_fingerprint", None)
                != trial.config_fingerprint,
                getattr(frozen, "composite_fingerprint", None)
                != trial.campaign.campaign_fingerprint,
            )
        ):
            raise CoordinatorError("PreparedTrial frozen fingerprint differs from trial")

    def dispatch(
        self,
        packet: HostPacket,
        *,
        prepared_trial: PreparedTrialLike,
        sink: ContinuousTpCommandSink,
    ) -> AdvisoryDispatchReceipt:
        """Deliver one already-fsync'd packet through the continuous-TP seam."""

        self._require_healthy()
        if self.latest is None:
            raise CoordinatorError("no durable journal revision binds this packet")
        snapshot = self.supervisor.recovery_snapshot()
        if packet.command is HostCommand.ARM:
            if snapshot.active is None:
                raise CoordinatorError("durable journal has no active ARM")
            intent = snapshot.active
            expected = host_packet_for_trial(
                intent.trial,
                command=HostCommand.ARM,
                execution_profile_id=intent.execution_profile_integer_id,
            )
        elif packet.command is HostCommand.ACK_BUNDLE:
            if snapshot.pending_ack is None or snapshot.prepared_ack is None:
                raise CoordinatorError("durable journal has no prepared ACK")
            intent = snapshot.pending_ack[0]
            expected = snapshot.prepared_ack
        else:
            raise CoordinatorError("coordinator dispatch supports only ARM and ACK")
        if packet != expected:
            raise CoordinatorError("packet differs from the latest durable command intent")
        self._validate_prepared_trial(prepared_trial, intent.trial)
        sender = getattr(sink, "send_command", None)
        if not callable(sender):
            raise CoordinatorError("continuous TP command sink lacks send_command")
        reader = getattr(sink, "read_latest", None)
        if not callable(reader):
            raise CoordinatorError("continuous TP command sink lacks read_latest")
        try:
            sender(packet, prepared_trial=prepared_trial)
            observation = MailboxObservation.from_command(reader())
            receipt = _receipt_from_observation(
                observation,
                packet,
                trial_uid=intent.trial.trial_uid,
            )
            if receipt != self._dispatch_receipt:
                self._dispatch_receipt = receipt
                self._append_snapshot(self.supervisor.recovery_snapshot())
            return receipt
        except Exception:
            self._poisoned = True
            raise

    def _current_packet(self) -> tuple[HostPacket, str] | None:
        if self.latest is None:
            return None
        state = self.latest.state
        if state.active_trial is not None:
            cursor = state.active_trial
            return (
                HostPacket(
                    campaign_epoch=state.campaign.campaign_epoch,
                    trial_id=cursor.trial_id,
                    command=HostCommand.ARM,
                    candidate_token=cursor.candidate_token,
                    execution_profile_id=cursor.execution_profile_integer_id,
                    command_seq=cursor.arm_command_seq,
                ),
                cursor.trial_uid,
            )
        if state.pending_ack is not None:
            pending = state.pending_ack
            cursor = pending.trial
            return (
                HostPacket(
                    campaign_epoch=state.campaign.campaign_epoch,
                    trial_id=cursor.trial_id,
                    command=HostCommand.ACK_BUNDLE,
                    candidate_token=cursor.candidate_token,
                    execution_profile_id=cursor.execution_profile_integer_id,
                    command_seq=pending.ack_command_seq,
                ),
                cursor.trial_uid,
            )
        return None

    def _adopt_mailbox_observation(
        self,
        observation: MailboxObservation | None,
        tp_snapshot: TpSnapshot,
    ) -> None:
        current = self._current_packet()
        if observation is None or not observation.observed:
            if current is not None:
                raise RecoveryError(
                    "current durable command requires an actual mailbox observation"
                )
            return
        if observation.packet is not None:
            if current is not None and observation.matches(
                current[0], trial_uid=current[1]
            ):
                receipt = _receipt_from_observation(
                    observation, current[0], trial_uid=current[1]
                )
                if self._dispatch_receipt not in (None, receipt):
                    raise RecoveryError(
                        "mailbox read-back differs from durable dispatch receipt"
                    )
                if self._dispatch_receipt is None:
                    self._dispatch_receipt = receipt
                    self._append_snapshot(self.supervisor.recovery_snapshot())
                return
            if observation.packet.command_seq > tp_snapshot.consumed_command_seq:
                raise RecoveryError(
                    "mailbox contains a non-current command not proven consumed"
                )

    def reconcile(
        self,
        tp_snapshot: TpSnapshot,
        *,
        trial_brief_admission: TrialBriefAdmissionLike | None = None,
    ) -> ReconcileResult:
        self._require_healthy()
        if self.latest is None:
            raise RecoveryError("no durable journal revision exists")
        decision = reconcile_tp_snapshot(self.latest, tp_snapshot)
        if decision.fail_closed:
            self._poisoned = True
            raise RecoveryError(decision.reason)
        if decision.action is ReconcileAction.PERSIST_POST_ACK:
            snapshot = self.supervisor.recovery_snapshot()
            if snapshot.pending_ack is None or snapshot.prepared_ack is None:
                self._poisoned = True
                raise RecoveryError("post-ACK recovery lacks prepared policy state")
            _, post_phase = snapshot.pending_ack
            intent, _ = snapshot.pending_ack
            matching_outcomes = [
                outcome
                for outcome in snapshot.outcome_timeline
                if outcome.evaluation.trial_uid == intent.trial.trial_uid
            ]
            if len(matching_outcomes) != 1:
                self._poisoned = True
                raise RecoveryError("post-ACK policy state lacks one exact outcome")
            brief_reference = None
            if self.supervisor.selection_policy == "codex_batches":
                if trial_brief_admission is None:
                    self._poisoned = True
                    raise RecoveryError(
                        "codex batch ACK requires durable post-closure TrialBrief"
                    )
                brief_reference = _trial_brief_reference(
                    trial_brief_admission,
                    trial_uid=intent.trial.trial_uid,
                    ack_command_seq=snapshot.prepared_ack.command_seq,
                    optimizer_eligible=matching_outcomes[0].eligible,
                )
            predicted = replace(
                snapshot,
                phase=post_phase,
                pending_ack=None,
                prepared_ack=None,
            )
            try:
                pending = self.latest.state.pending_ack
                if pending is None:
                    raise RecoveryError("post-ACK recovery lacks journal ACK cursor")
                self._terminal_fates = self._terminal_fates + (
                    TerminalFate(
                        kind="ack_consumed",
                        trial=pending.trial,
                        command="ack_bundle",
                        command_seq=pending.ack_command_seq,
                        tp_snapshot=tp_snapshot,
                        dispatch_receipt=self._dispatch_receipt,
                        evidence=brief_reference,
                    ),
                )
                self._dispatch_receipt = None
                self._append_snapshot(predicted)
                self.supervisor.confirm_ack_consumed(snapshot.prepared_ack)
            except Exception:
                self._poisoned = True
                raise
            return ReconcileResult(decision, None)
        packet: HostPacket | None = None
        snapshot = self.supervisor.recovery_snapshot()
        if decision.action is ReconcileAction.SEND_PERSISTED_ARM:
            if snapshot.active is None:
                raise RecoveryError("persisted ARM decision lacks active intent")
            packet = host_packet_for_trial(
                snapshot.active.trial,
                command=HostCommand.ARM,
                execution_profile_id=snapshot.active.execution_profile_integer_id,
            )
        elif decision.action is ReconcileAction.SEND_PERSISTED_ACK:
            packet = snapshot.prepared_ack
            if packet is None:
                raise RecoveryError("persisted ACK decision lacks prepared packet")
        return ReconcileResult(decision, packet)

    def mark_infrastructure_ready(self, tp_snapshot: TpSnapshot) -> JournalEntry:
        """Persist infra release at HOME before arming an untried candidate."""

        self._require_healthy()
        if self.latest is None:
            raise RecoveryError("no durable journal revision exists")
        decision = reconcile_tp_snapshot(self.latest, tp_snapshot)
        if decision.action is not ReconcileAction.HOLD_WAIT_INFRA:
            self._poisoned = True
            raise RecoveryError(
                "infrastructure release requires the exact durable WAIT_INFRA TP state: "
                + decision.reason
            )
        try:
            self.supervisor.mark_infra_ready()
            return self._append_snapshot(self.supervisor.recovery_snapshot())
        except Exception:
            self._poisoned = True
            raise

    def begin_governor_probe(
        self, samples: Sequence[SaturationSample] | None = None
    ) -> tuple[ExecutionProfile | None, GovernorDecision]:
        self._require_healthy()
        try:
            candidate, decision = self.supervisor.begin_governor_probe(samples)
            if candidate is not None:
                self._append_snapshot(self.supervisor.recovery_snapshot())
            return candidate, decision
        except Exception:
            self._poisoned = True
            raise

    def complete_governor_probe(self, evidence: AbEvidence) -> GovernorDecision:
        self._require_healthy()
        try:
            decision = self.supervisor.complete_governor_probe(evidence)
            self._append_snapshot(self.supervisor.recovery_snapshot())
            return decision
        except Exception:
            self._poisoned = True
            raise

    def resume_after_code_change(
        self,
        *,
        new_journal: SupervisorJournal,
        campaign: CampaignSpec,
        source_fingerprint: str,
        config_fingerprint: str,
        search_attestation: SearchAttestation | None = None,
    ) -> "CampaignCoordinator":
        self._require_healthy()
        if new_journal.root == self.journal.root:
            raise CoordinatorError("code epoch requires a distinct new journal root")
        try:
            new_journal.load_latest()
        except JournalIntegrityError as exc:
            if "missing" not in str(exc) and "no durable revision" not in str(exc):
                raise CoordinatorError("new code-epoch journal is not empty") from exc
        else:
            raise CoordinatorError("new code-epoch journal already contains a revision")
        self.supervisor.resume_after_code_change(
            campaign=campaign,
            source_fingerprint=source_fingerprint,
            config_fingerprint=config_fingerprint,
            search_attestation=search_attestation,
        )
        coordinator = CampaignCoordinator(
            supervisor=self.supervisor,
            journal=new_journal,
            trial_spec_references=self._trial_spec_references,
            # Outcome/bundle history never crosses a code campaign epoch.
            bundle_references={},
            terminal_fates=tuple(
                fate
                for fate in self._terminal_fates
                if fate.kind == "infra_aborted_consumed"
            ),
        )
        coordinator.persist_home()
        self._poisoned = True
        return coordinator

    @classmethod
    def restore(
        cls,
        *,
        supervisor: CampaignSupervisor,
        journal: SupervisorJournal,
        latest: JournalEntry,
        resume_history: Sequence[Mapping[str, Any]],
        promotion_history: Sequence[Mapping[str, Any]],
        tp_snapshot: TpSnapshot,
        prior_resume_history: Sequence[Mapping[str, Any]] = (),
        profile_catalog: Sequence[ExecutionProfile] = (),
        mailbox_observation: MailboxObservation | None = None,
        defer_reconcile: bool = False,
    ) -> RestoreResult:
        """Restore from verified store rows, latest journal, and one TP snapshot."""

        identity = _campaign_identity(supervisor)
        current = journal.load_latest(expected_campaign=identity)
        if current != latest:
            raise RecoveryError("provided latest journal entry is stale or unverified")
        if any(not isinstance(row, Mapping) for row in resume_history):
            raise RecoveryError("resume history contains a non-object row")
        expected_promotion: list[Mapping[str, Any]] = []
        for row in resume_history:
            evaluation_payload = row.get("evaluation")
            if not isinstance(evaluation_payload, Mapping):
                raise RecoveryError("resume history evaluation is not an object")
            if (
                evaluation_payload.get("eligible") is True
                and evaluation_payload.get("disposition")
                == TrialDisposition.OBJECTIVE.value
            ):
                expected_promotion.append(row)
        if [canonical_json_bytes(dict(row)) for row in promotion_history] != [
            canonical_json_bytes(dict(row)) for row in expected_promotion
        ]:
            raise RecoveryError(
                "promotion history is not the exact eligible-objective subset of resume history"
            )

        trials: dict[str, TrialSpec] = {}
        evaluations: dict[str, Evaluation] = {}
        rows_by_trial: dict[str, Mapping[str, Any]] = {}
        outcomes: list[Observation] = []
        prior_trial_id = 0
        prior_command_seq = 0
        token_map: dict[str, int] = {}
        token_owners: dict[int, str] = {}
        prior_outcomes_by_epoch: dict[int, list[Observation]] = {}
        prior_trials: dict[str, TrialSpec] = {}
        prior_spec_refs: dict[str, JournalReference] = {}
        for row in prior_resume_history:
            trial = _trial_from_payload(row.get("trial"))
            evaluation = _evaluation_from_payload(row.get("evaluation"))
            if any(
                (
                    row.get("trial_uid") != trial.trial_uid,
                    evaluation.trial_uid != trial.trial_uid,
                    trial.campaign.campaign_id != supervisor.campaign.campaign_id,
                    trial.campaign.campaign_epoch >= supervisor.campaign.campaign_epoch,
                    trial.backend_id != supervisor.backend_id,
                    trial.trial_id <= prior_trial_id,
                    trial.command_seq <= prior_command_seq,
                    trial.trial_uid in trials,
                )
            ):
                raise RecoveryError("prior resume history identity/order is invalid")
            candidate_uid = trial.candidate.candidate_uid
            prior_token = token_map.get(candidate_uid)
            prior_owner = token_owners.get(trial.candidate_token)
            if prior_token not in (None, trial.candidate_token) or prior_owner not in (
                None,
                candidate_uid,
            ):
                raise RecoveryError("prior history candidate token mapping is inconsistent")
            token_map[candidate_uid] = trial.candidate_token
            token_owners[trial.candidate_token] = candidate_uid
            observation = Observation(
                trial.candidate,
                evaluation,
                trial.execution_profile.profile_id,
                trial.plant_epoch,
                row.get("artifact_provenance", {}).get("csv", {}).get("sha256"),
            )
            prior_outcomes_by_epoch.setdefault(
                trial.campaign.campaign_epoch, []
            ).append(observation)
            prior_trials[trial.trial_uid] = trial
            prior_spec_refs[trial.trial_uid] = _trial_reference_from_history(
                row, trial
            )
            trials[trial.trial_uid] = trial
            prior_trial_id = trial.trial_id
            prior_command_seq = trial.command_seq
        for row in resume_history:
            trial = _trial_from_payload(row.get("trial"))
            evaluation = _evaluation_from_payload(row.get("evaluation"))
            if any(
                (
                    row.get("trial_uid") != trial.trial_uid,
                    evaluation.trial_uid != trial.trial_uid,
                    trial.campaign != supervisor.campaign,
                    trial.backend_id != supervisor.backend_id,
                    trial.source_fingerprint != supervisor.source_fingerprint,
                    trial.config_fingerprint != supervisor.config_fingerprint,
                    trial.trial_id <= prior_trial_id,
                    trial.command_seq <= prior_command_seq,
                    trial.trial_uid in trials,
                )
            ):
                raise RecoveryError("resume history identity/order differs from campaign")
            candidate_uid = trial.candidate.candidate_uid
            prior_token = token_map.get(candidate_uid)
            prior_owner = token_owners.get(trial.candidate_token)
            if prior_token not in (None, trial.candidate_token) or prior_owner not in (
                None,
                candidate_uid,
            ):
                raise RecoveryError("resume history candidate token mapping is inconsistent")
            token_map[candidate_uid] = trial.candidate_token
            token_owners[trial.candidate_token] = candidate_uid
            latest_trace = row.get("artifact_provenance", {}).get("csv", {}).get(
                "sha256"
            )
            outcomes.append(
                Observation(
                    trial.candidate,
                    evaluation,
                    trial.execution_profile.profile_id,
                    trial.plant_epoch,
                    latest_trace,
                )
            )
            trials[trial.trial_uid] = trial
            evaluations[trial.trial_uid] = evaluation
            rows_by_trial[trial.trial_uid] = row
            prior_trial_id = trial.trial_id
            prior_command_seq = trial.command_seq

        state = latest.state
        for fate in state.terminal_fates:
            _verify_infra_abort_evidence(fate)
            _verify_trial_brief_evidence(
                fate,
                required=supervisor.selection_policy == "codex_batches",
            )
        if any(
            (
                state.execution_profile_id
                != supervisor.execution_profile.profile_id,
                state.execution_profile_integer_id
                != execution_profile_integer_id(supervisor.execution_profile),
                state.plant_epoch != supervisor.plant_epoch,
                state.high_water.trial_id < prior_trial_id,
                state.high_water.command_seq < prior_command_seq,
            )
        ):
            raise RecoveryError("journal profile/plant/high-water differs from verified history")
        spec_refs: dict[str, JournalReference] = dict(prior_spec_refs)
        cursors = list(state._cursors())
        cross_epoch_infra_fates = {
            fate.trial.trial_uid
            for fate in state.terminal_fates
            if fate.kind == "infra_aborted_consumed"
        }
        for cursor in cursors:
            trial = _trial_from_reference(cursor)
            if trial.campaign != supervisor.campaign or any(
                (
                    trial.backend_id != supervisor.backend_id,
                    trial.source_fingerprint != supervisor.source_fingerprint,
                    trial.config_fingerprint != supervisor.config_fingerprint,
                )
            ):
                # A code-fix retry origin may belong to the immediately prior
                # campaign and is carried only into a newly rooted journal.
                retained_infra_attempt = bool(
                    trial.trial_uid in cross_epoch_infra_fates
                    and trial.campaign.campaign_id == supervisor.campaign.campaign_id
                    and trial.campaign.campaign_epoch
                    < supervisor.campaign.campaign_epoch
                    and trial.backend_id == supervisor.backend_id
                )
                if not retained_infra_attempt and (
                    state.pending_retry is None
                    or state.pending_retry.kind != "code_fix"
                ):
                    raise RecoveryError("journal cursor differs from campaign fingerprints")
            spec_refs[trial.trial_uid] = cursor.trial_spec
            prior_token = token_map.get(trial.candidate.candidate_uid)
            prior_owner = token_owners.get(trial.candidate_token)
            if prior_token not in (None, trial.candidate_token) or prior_owner not in (
                None,
                trial.candidate.candidate_uid,
            ):
                raise RecoveryError("journal cursor candidate token mapping conflicts")
            token_map[trial.candidate.candidate_uid] = trial.candidate_token
            token_owners[trial.candidate_token] = trial.candidate.candidate_uid
            trials.setdefault(trial.trial_uid, trial)

        if token_map != dict(state.candidate_tokens):
            raise RecoveryError("journal candidate token map differs from reconstructed trials")
        reconstructed_trial_high_water = max(
            (trial.trial_id for trial in trials.values()), default=0
        )
        if state.high_water.trial_id != reconstructed_trial_high_water:
            raise RecoveryError("journal trial high-water has no exact reconstructed trial")

        bundle_refs: dict[str, JournalReference] = {}
        bundle_payloads: dict[str, Mapping[str, Any]] = {}
        history_ref_ids = {row.reference_id for row in state.history_references}
        if {row.reference_id for row in state.observation_references} != history_ref_ids:
            raise RecoveryError("journal observation/history references differ")
        for reference in state.history_references:
            _, payload = _read_reference(reference, role="journal immutable bundle")
            if not isinstance(payload, Mapping):
                raise RecoveryError("journal immutable bundle is not an object")
            trial_payload = payload.get("trial")
            trial_uid = trial_payload.get("trial_uid") if isinstance(trial_payload, Mapping) else None
            if trial_uid not in rows_by_trial:
                raise RecoveryError("journal bundle reference lacks verified resume history")
            trial = trials[trial_uid]
            expected_reference, _ = _bundle_reference(Path(reference.path), trial)
            if expected_reference != reference:
                raise RecoveryError("journal immutable bundle reference differs from bytes")
            if reference.reference_id != rows_by_trial[trial_uid].get("history_identity"):
                raise RecoveryError("journal bundle history identity differs from store row")
            try:
                trial_reference = _trial_reference(
                    trial,
                    Path(reference.path).parent,
                )
            except CoordinatorError as exc:
                raise RecoveryError(
                    "journal history lacks its exact durable TrialSpec reference"
                ) from exc
            prior_trial_reference = spec_refs.get(trial_uid)
            if prior_trial_reference not in (None, trial_reference):
                raise RecoveryError(
                    "journal history TrialSpec reference conflicts with a live cursor"
                )
            spec_refs[trial_uid] = trial_reference
            bundle_refs[trial_uid] = reference
            bundle_payloads[trial_uid] = payload

        active_cursor = state.active_trial
        transitional_uid = (
            active_cursor.trial_uid
            if active_cursor is not None and active_cursor.trial_uid in rows_by_trial
            else None
        )
        if transitional_uid is None:
            confirmed = success_confirmed(
                outcomes,
                profile_id=state.execution_profile_id,
                plant_epoch=state.plant_epoch,
            )
            success_declared = state.phase == CampaignPhase.SUCCEEDED.value or (
                state.pending_ack is not None
                and state.pending_ack.post_ack_phase
                == CampaignPhase.SUCCEEDED.value
            ) or (
                state.pending_advance is not None
                and state.pending_advance.post_commit_phase
                == CampaignPhase.SUCCEEDED.value
            )
            if success_declared != confirmed:
                raise RecoveryError(
                    "journal success phase differs from verified outcome timeline"
                )
        stable_history_uids = set(rows_by_trial) - ({transitional_uid} if transitional_uid else set())
        if stable_history_uids != set(bundle_refs):
            raise RecoveryError("verified history and journal bundle references are not closed")

        def intent_for(trial: TrialSpec, retry_kind: str | None = None) -> TrialIntent:
            return TrialIntent(
                trial=trial,
                execution_profile_integer_id=execution_profile_integer_id(
                    trial.execution_profile
                ),
                selection={"selection": "restored_from_verified_journal"},
                retry_kind=retry_kind,
            )

        active = None
        if active_cursor is not None:
            active = intent_for(
                trials[active_cursor.trial_uid],
                "infrastructure"
                if active_cursor.retry_release is not None
                else None,
            )
        pending_ack_value = None
        prepared_ack = None
        if state.pending_ack is not None:
            trial = trials[state.pending_ack.trial.trial_uid]
            intent = intent_for(trial)
            evaluation = evaluations.get(trial.trial_uid)
            if evaluation is None:
                raise RecoveryError("pending ACK lacks a verified outcome row")
            capture_payload = bundle_payloads.get(trial.trial_uid, {}).get("capture")
            if not isinstance(capture_payload, Mapping) or any(
                (
                    capture_payload.get("terminal_reason")
                    != state.pending_ack.terminal_reason,
                    capture_payload.get("host_cause")
                    != state.pending_ack.host_cause,
                )
            ):
                raise RecoveryError(
                    "pending ACK terminal reason/host cause differs from bundle bytes"
                )
            if evaluation.disposition is TrialDisposition.OBJECTIVE:
                confirmed = success_confirmed(
                    outcomes,
                    profile_id=trial.execution_profile.profile_id,
                    plant_epoch=trial.plant_epoch,
                )
                b_probe_pending = bool(
                    state.governor_probe is not None
                    and state.governor_probe.stage in {"a", "b"}
                    and trial.execution_profile.profile_id
                    == state.governor_probe.profile_after_id
                )
                expected_post_phase = (
                    CampaignPhase.SUCCEEDED
                    if confirmed and not b_probe_pending
                    else CampaignPhase.HOME
                )
            else:
                expected_post_phase = {
                    TrialDisposition.WAIT_INFRA_READY: CampaignPhase.WAIT_INFRA_READY,
                    TrialDisposition.CODE_CONTRACT_BUG: CampaignPhase.PAUSED_CODE_BUG,
                    TrialDisposition.FAIL_CLOSED: CampaignPhase.HOME,
                    TrialDisposition.SAFETY_STOP: CampaignPhase.STOPPED_SAFETY,
                    TrialDisposition.MANUAL_RECOVERY: CampaignPhase.MANUAL_RECOVERY,
                    TrialDisposition.PARAMETER_EVENT: CampaignPhase.STOPPED_PARAMETER,
                    TrialDisposition.OPERATOR_STOP: CampaignPhase.STOPPED_OPERATOR,
                }.get(evaluation.disposition, CampaignPhase.STOPPED_FAIL_CLOSED)
            if state.pending_ack.post_ack_phase != expected_post_phase.value:
                raise RecoveryError("pending ACK post phase differs from verified outcome")
            pending_ack_value = (
                intent,
                expected_post_phase,
            )
            prepared_ack = HostPacket(
                campaign_epoch=trial.campaign.campaign_epoch,
                trial_id=trial.trial_id,
                command=HostCommand.ACK_BUNDLE,
                candidate_token=trial.candidate_token,
                execution_profile_id=intent.execution_profile_integer_id,
                command_seq=state.pending_ack.ack_command_seq,
            )
        pending_advance_value: PendingAdvance | None = None
        if state.pending_advance is not None:
            trial = trials[state.pending_advance.trial.trial_uid]
            intent = intent_for(trial)
            evaluation = evaluations.get(trial.trial_uid)
            if evaluation is None:
                raise RecoveryError("pending advance lacks a verified outcome row")
            capture_payload = bundle_payloads.get(trial.trial_uid, {}).get(
                "capture"
            )
            if not isinstance(capture_payload, Mapping) or any(
                (
                    capture_payload.get("terminal_reason")
                    != state.pending_advance.terminal_reason,
                    capture_payload.get("host_cause")
                    != state.pending_advance.host_cause,
                )
            ):
                raise RecoveryError(
                    "pending advance terminal reason/host cause differs from bundle bytes"
                )
            if evaluation.disposition is TrialDisposition.OBJECTIVE:
                confirmed = success_confirmed(
                    outcomes,
                    profile_id=trial.execution_profile.profile_id,
                    plant_epoch=trial.plant_epoch,
                )
                b_probe_pending = bool(
                    state.governor_probe is not None
                    and state.governor_probe.stage in {"a", "b"}
                    and trial.execution_profile.profile_id
                    == state.governor_probe.profile_after_id
                )
                expected_post_phase = (
                    CampaignPhase.SUCCEEDED
                    if confirmed and not b_probe_pending
                    else CampaignPhase.HOME
                )
            else:
                expected_post_phase = {
                    TrialDisposition.WAIT_INFRA_READY: CampaignPhase.WAIT_INFRA_READY,
                    TrialDisposition.CODE_CONTRACT_BUG: CampaignPhase.PAUSED_CODE_BUG,
                    TrialDisposition.FAIL_CLOSED: CampaignPhase.HOME,
                    TrialDisposition.SAFETY_STOP: CampaignPhase.STOPPED_SAFETY,
                    TrialDisposition.MANUAL_RECOVERY: CampaignPhase.MANUAL_RECOVERY,
                    TrialDisposition.PARAMETER_EVENT: CampaignPhase.STOPPED_PARAMETER,
                    TrialDisposition.OPERATOR_STOP: CampaignPhase.STOPPED_OPERATOR,
                }.get(evaluation.disposition, CampaignPhase.STOPPED_FAIL_CLOSED)
            if (
                state.pending_advance.post_commit_phase
                != expected_post_phase.value
            ):
                raise RecoveryError(
                    "pending advance post phase differs from verified outcome"
                )
            pending_advance_value = PendingAdvance(
                intent=intent,
                post_commit_phase=expected_post_phase,
            )
        pending_retry_value = None
        if state.pending_retry is not None:
            retry_trial = trials[state.pending_retry.origin_trial.trial_uid]
            retry_evaluation = evaluations.get(retry_trial.trial_uid)
            retry_capture = bundle_payloads.get(retry_trial.trial_uid, {}).get(
                "capture"
            )
            if retry_capture is not None and (
                not isinstance(retry_capture, Mapping)
                or retry_capture.get("terminal_reason")
                != state.pending_retry.terminal_reason
                or retry_capture.get("host_cause") != state.pending_retry.host_cause
            ):
                raise RecoveryError(
                    "pending retry terminal reason/host cause differs from bundle bytes"
                )
            expected_retry_kind = (
                None
                if retry_evaluation is None
                else {
                    TrialDisposition.WAIT_INFRA_READY: "infrastructure",
                    TrialDisposition.CODE_CONTRACT_BUG: "code_fix",
                    TrialDisposition.FAIL_CLOSED: "evidence",
                }.get(retry_evaluation.disposition)
            )
            if expected_retry_kind not in (None, state.pending_retry.kind):
                raise RecoveryError("pending retry kind differs from verified outcome")
            pending_retry_value = (
                retry_trial.candidate,
                retry_trial.candidate_token,
                state.pending_retry.kind,
                trial_source_from_trial(retry_trial),
            )

        profiles = {supervisor.execution_profile.profile_id: supervisor.execution_profile}
        profiles.update({row.profile_id: row for row in profile_catalog})
        governor_value: GovernorProbeState | None = None
        persisted_probe = state.governor_probe
        if persisted_probe is not None:
            try:
                profile_a = profiles[persisted_probe.profile_before_id]
                profile_b = profiles[persisted_probe.profile_after_id]
                trial_a = trials[persisted_probe.trial_a_uid]
            except KeyError as exc:
                raise RecoveryError(
                    "governor probe profile/A trial is absent from verified recovery state"
                ) from exc

            def closed_identity(
                trial_uid: str | None,
                *,
                label: str,
                expected_profile: ExecutionProfile,
            ) -> AbTrialIdentity | None:
                if trial_uid is None:
                    return None
                try:
                    trial = trials[trial_uid]
                    evaluation = evaluations[trial_uid]
                except KeyError as exc:
                    raise RecoveryError(
                        f"governor {label} identity lacks verified closed history"
                    ) from exc
                if any(
                    (
                        trial.candidate.candidate_uid
                        != persisted_probe.force_candidate_uid,
                        trial.execution_profile != expected_profile,
                        trial.plant_epoch != persisted_probe.plant_epoch,
                        not evaluation.eligible,
                        not evaluation.safe_closure,
                    )
                ):
                    raise RecoveryError(
                        f"governor {label} identity differs from verified outcome"
                    )
                return AbTrialIdentity(
                    trial_uid=trial.trial_uid,
                    force_candidate_uid=trial.candidate.candidate_uid,
                    profile_id=trial.execution_profile.profile_id,
                    plant_epoch=trial.plant_epoch,
                )

            identity_a = closed_identity(
                persisted_probe.trial_a_uid,
                label="A",
                expected_profile=profile_a,
            )
            assert identity_a is not None
            if any(
                (
                    trial_a.candidate.candidate_uid
                    != persisted_probe.force_candidate_uid,
                    trial_a.plant_epoch != persisted_probe.plant_epoch,
                )
            ):
                raise RecoveryError(
                    "governor frozen candidate/plant epoch differs from A trial"
                )
            governor_value = GovernorProbeState(
                profile_a=profile_a,
                profile_b=profile_b,
                layer=persisted_probe.layer,
                force_candidate=trial_a.candidate,
                identity_a=identity_a,
                stage=persisted_probe.stage,
                identity_b=closed_identity(
                    persisted_probe.trial_b_uid,
                    label="B",
                    expected_profile=profile_b,
                ),
                identity_a_prime=closed_identity(
                    persisted_probe.trial_a_prime_uid,
                    label="A-prime",
                    expected_profile=profile_a,
                ),
            )

        restore_outcomes = outcomes
        if transitional_uid is not None:
            restore_outcomes = [
                outcome
                for outcome in outcomes
                if outcome.evaluation.trial_uid != transitional_uid
            ]
        snapshot = SupervisorRecoverySnapshot(
            phase=CampaignPhase(state.phase),
            outcome_timeline=tuple(restore_outcomes),
            trial_counter=state.high_water.trial_id,
            command_seq=state.high_water.command_seq,
            candidate_tokens=state.candidate_tokens,
            active=active,
            pending_retry=pending_retry_value,
            pending_ack=pending_ack_value,
            prepared_ack=prepared_ack,
            pending_advance=pending_advance_value,
            cooldown_remaining=state.cooldown_remaining,
            governor_probe=governor_value,
        )
        supervisor.archived_epoch_outcomes = [
            (epoch, tuple(rows))
            for epoch, rows in sorted(prior_outcomes_by_epoch.items())
        ]
        supervisor.archived_epoch_observations = [
            (
                epoch,
                tuple(
                    row
                    for row in rows
                    if row.eligible
                    and row.evaluation.disposition is TrialDisposition.OBJECTIVE
                ),
            )
            for epoch, rows in sorted(prior_outcomes_by_epoch.items())
        ]
        supervisor._archived_trial_sources = {
            trial_uid: trial_source_from_trial(trial)
            for trial_uid, trial in prior_trials.items()
        }
        supervisor.restore_recovery_snapshot(snapshot)
        coordinator = cls(
            supervisor=supervisor,
            journal=journal,
            latest=latest,
            trial_spec_references=spec_refs,
            bundle_references=bundle_refs,
        )
        coordinator._adopt_mailbox_observation(
            mailbox_observation,
            tp_snapshot,
        )

        if defer_reconcile:
            return RestoreResult(
                coordinator,
                ReconcileDecision(
                    ReconcileAction.HOLD_TERMINAL,
                    "reconciliation deferred for explicit code-epoch migration",
                ),
                None,
            )

        if transitional_uid is not None:
            direct_protocol = (
                supervisor.completion_protocol is CompletionProtocol.DIRECT_ARM_V1
            )
            expected_states = (
                {"READY_NEAR", "READY_HOME_CLOSED"}
                if direct_protocol
                else {"WAIT_ACK"}
            )
            if (
                tp_snapshot.state not in expected_states
                or active_cursor is None
                or tp_snapshot.campaign_epoch_echo != supervisor.campaign.campaign_epoch
                or tp_snapshot.trial_id_echo != active_cursor.trial_id
                or tp_snapshot.candidate_token_echo != active_cursor.candidate_token
                or tp_snapshot.execution_profile_integer_id_echo
                != active_cursor.execution_profile_integer_id
                or tp_snapshot.consumed_command_seq != active_cursor.arm_command_seq
            ):
                raise RecoveryError(
                    "bundle-before-completion recovery lacks exact TP terminal echo"
                )
            trial = trials[transitional_uid]
            bundle_path = Path(active_cursor.trial_spec.path).parent / "immutable_trial_bundle.json"
            reference, payload = _bundle_reference(bundle_path, trial)
            if reference.reference_id != rows_by_trial[transitional_uid].get(
                "history_identity"
            ):
                raise RecoveryError("transitional bundle differs from verified history")
            manifest = CaptureManifest(**payload["capture"])
            if manifest.terminal_reason != tp_snapshot.terminal_reason:
                raise RecoveryError(
                    "transitional bundle terminal reason differs from exact TP terminal"
                )
            evaluation = evaluations[transitional_uid]
            coordinator.close_trial(
                manifest=manifest,
                evaluation=evaluation,
                safe_closure=manifest.safe_closure_evidence,
                bundle_path=bundle_path,
            )
            if direct_protocol:
                coordinator.persist_direct_advance(
                    bundle_path,
                    verified_resume_history=resume_history,
                )
                decision = ReconcileDecision(
                    ReconcileAction.PERSIST_DIRECT_READY,
                    "verified_bundle_was_persisted_before_direct_advance_intent",
                    active_cursor.arm_command_seq,
                )
                return RestoreResult(coordinator, decision, None)
            packet = coordinator.issue_ack(
                bundle_path,
                verified_resume_history=resume_history,
            )
            decision = ReconcileDecision(
                ReconcileAction.SEND_PERSISTED_ACK,
                "verified_bundle_was_persisted_before_ack_intent",
                packet.command_seq,
            )
            return RestoreResult(coordinator, decision, packet)

        result = coordinator.reconcile(tp_snapshot)
        return RestoreResult(coordinator, result.decision, result.packet)
