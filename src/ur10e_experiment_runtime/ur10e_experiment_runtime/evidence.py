"""Write-once post-closure TrialBrief publication."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Mapping

from .batch import (
    BatchFate,
    BatchIdentity,
    BatchJournal,
    ExactAckReceipt,
    SafeClosureReceipt,
)
from .failure_to_guard import (
    MetricRole,
    ObserverStatus,
    OracleStatus,
    TrialOutcomeClass,
    gate_optimizer_observation,
)
from .identity import canonical_json_bytes, canonical_sha256, strict_json_loads


@dataclass(frozen=True)
class TrialBrief:
    document: Mapping[str, Any]

    @property
    def publication_uid(self) -> str:
        return str(self.document["publication_uid"])


def build_trial_brief(
    *,
    batch: BatchIdentity,
    row_index: int,
    trial_uid: str,
    immutable_bundle_sha256: str,
    ack: ExactAckReceipt,
    closure: SafeClosureReceipt,
    outcome_class: TrialOutcomeClass,
    metric_role: MetricRole,
    objective: float | None,
    oracle_status: OracleStatus,
    observer_status: ObserverStatus,
    artifact_digests: Mapping[str, str],
    fingerprint_verified: bool,
    legacy_trial_number: int | None = None,
) -> TrialBrief:
    if (
        isinstance(row_index, bool)
        or not isinstance(row_index, int)
        or not 1 <= row_index <= len(batch.rows)
    ):
        raise ValueError("TrialBrief row index is invalid")
    row = batch.rows[row_index - 1]
    if ack.batch_uid != batch.batch_uid or closure.batch_uid != batch.batch_uid:
        raise ValueError("TrialBrief batch identity differs")
    if ack.row_index != row_index or closure.row_index != row_index:
        raise ValueError("TrialBrief row identity differs")
    if ack.trial_uid != trial_uid or closure.trial_uid != trial_uid:
        raise ValueError("TrialBrief trial identity differs")
    if ack.control_candidate_uid != row.control_candidate_uid:
        raise ValueError("TrialBrief control candidate differs from actual overlay")
    if ack.immutable_bundle_sha256 != immutable_bundle_sha256:
        raise ValueError("TrialBrief immutable bundle differs from exact ACK")
    if closure.ack_uid != ack.ack_uid or not closure.post_ack_verified:
        raise ValueError("TrialBrief requires exact ACK and post-ACK closure")
    if closure.return_reference_uid != ack.return_reference_uid:
        raise ValueError("TrialBrief return reference differs from exact ACK")
    if closure.controller_readback_sha256 != ack.controller_readback_sha256:
        raise ValueError("TrialBrief controller readback differs from exact ACK")
    if (
        len(immutable_bundle_sha256) != 64
        or any(c not in "0123456789abcdef" for c in immutable_bundle_sha256)
    ):
        raise ValueError("immutable bundle must be a lowercase SHA256")
    if legacy_trial_number == 21 and (
        metric_role is not MetricRole.UNAVAILABLE or objective is not None
    ):
        raise ValueError("legacy trial 21 force metric must remain unavailable/null")
    if metric_role is MetricRole.UNAVAILABLE and objective is not None:
        raise ValueError("unavailable metric must be null")
    if legacy_trial_number in {*range(13, 21), 22} and (
        metric_role is not MetricRole.DIAGNOSTIC_ONLY
    ):
        raise ValueError("legacy diagnostic trials must remain diagnostic_only")
    if not artifact_digests:
        raise ValueError("TrialBrief requires non-empty artifact digests")
    for name, digest in artifact_digests.items():
        if not name or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("artifact digests must be named lowercase SHA256 values")
    if artifact_digests.get("bundle") != immutable_bundle_sha256:
        raise ValueError("TrialBrief bundle artifact digest differs")
    publication_material = {
        "batch_uid": batch.batch_uid,
        "row_uid": canonical_sha256({"batch_uid": batch.batch_uid, "row": row.to_dict()}),
        "bundle_sha256": immutable_bundle_sha256,
        "ack_uid": ack.ack_uid,
        "closure_uid": closure.receipt_sha256,
    }
    publication_uid = canonical_sha256(
        {"schema": "ur-exp/trial-brief-publication/v1", **publication_material}
    )
    gate = gate_optimizer_observation(
        outcome_class,
        objective,
        metric_role=metric_role,
        oracle_status=oracle_status,
        observer_status=observer_status,
        fingerprint_verified=fingerprint_verified,
        exact_ack_consumed=True,
        post_ack_closure_verified=True,
        publication_unique=False,
    )
    document = {
        "schema": "ur-exp/trial-brief-v1",
        "publication_uid": publication_uid,
        **publication_material,
        "row_index": row_index,
        "trial_uid": trial_uid,
        "legacy_trial_number": legacy_trial_number,
        "control_candidate_uid": row.control_candidate_uid,
        "trial_overlay": dict(row.trial_overlay),
        "outcome_class": outcome_class.value,
        "metric_role": metric_role.value,
        "metric_value": objective,
        "objective": gate.objective,
        "oracle_status": oracle_status.value,
        "observer_status": observer_status.value,
        "optimizer_eligible": gate.optimizer_eligible,
        "optimizer_rejection_reasons": list(gate.rejection_reasons),
        "artifact_digests": dict(sorted(artifact_digests.items())),
        "fingerprint_verified": fingerprint_verified,
        "publication_unique": False,
    }
    return TrialBrief(strict_json_loads(canonical_json_bytes(document)))


def _published_document(brief: TrialBrief) -> Mapping[str, Any]:
    document = dict(brief.document)
    if document.get("publication_unique") is not False:
        raise ValueError("EvidenceSink accepts only an unpublished TrialBrief draft")
    gate = gate_optimizer_observation(
        document["outcome_class"],
        document.get("metric_value"),
        metric_role=document["metric_role"],
        oracle_status=document["oracle_status"],
        observer_status=document["observer_status"],
        fingerprint_verified=document.get("fingerprint_verified") is True,
        exact_ack_consumed=True,
        post_ack_closure_verified=True,
        publication_unique=True,
    )
    document["objective"] = gate.objective
    document["optimizer_eligible"] = gate.optimizer_eligible
    document["optimizer_rejection_reasons"] = list(gate.rejection_reasons)
    document["publication_unique"] = True
    return strict_json_loads(canonical_json_bytes(document))


class EvidenceSink:
    def __init__(self, root: Path, batch_journal: BatchJournal) -> None:
        if not isinstance(root, Path) or not root.is_absolute() or root.is_symlink():
            raise ValueError("EvidenceSink root must be an absolute non-symlink path")
        self.root = root
        self.batch_journal = batch_journal

    def publish_trial_brief(self, brief: TrialBrief) -> Path:
        row_index = brief.document.get("row_index")
        if (
            isinstance(row_index, bool)
            or not isinstance(row_index, int)
            or not 1 <= row_index <= len(self.batch_journal.identity().rows)
        ):
            raise ValueError("TrialBrief row index is invalid")
        state = self.batch_journal.state()
        row = state.rows[row_index - 1]
        if (
            state.batch_uid != brief.document.get("batch_uid")
            or row.fate is not BatchFate.ACK_COMPLETED
            or row.trial_uid != brief.document.get("trial_uid")
            or row.immutable_bundle_sha256 != brief.document.get("bundle_sha256")
            or row.ack_uid != brief.document.get("ack_uid")
            or row.closure_receipt_sha256 != brief.document.get("closure_uid")
        ):
            raise ValueError(
                "TrialBrief publication requires the exact durable ACK-completed row"
            )
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{brief.publication_uid}.trial-brief.json"
        published = _published_document(brief)
        document_bytes = canonical_json_bytes(published)
        payload = document_bytes + b"\n"
        created = False
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
            created = True
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise FileExistsError("TrialBrief publication identity collision")
        if created:
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short TrialBrief write")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            directory = os.open(
                self.root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        current = self.batch_journal.state().rows[row_index - 1]
        if current.trial_brief_document_sha256 is None:
            self.batch_journal.record_trial_brief_published(
                row_index=row_index,
                trial_uid=str(brief.document["trial_uid"]),
                publication_uid=brief.publication_uid,
                document_sha256=canonical_sha256(published),
                optimizer_eligible=published["optimizer_eligible"],
            )
        else:
            if (
                current.trial_brief_publication_uid != brief.publication_uid
                or current.trial_brief_document_sha256
                != canonical_sha256(published)
                or current.optimizer_eligible != published["optimizer_eligible"]
            ):
                raise FileExistsError("TrialBrief durable ledger identity collision")
        return path
