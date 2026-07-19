"""Write-once post-closure TrialBrief publication."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Mapping

from .batch import BatchIdentity, ExactAckReceipt, SafeClosureReceipt
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
    row = batch.rows[row_index - 1]
    if ack.batch_uid != batch.batch_uid or closure.batch_uid != batch.batch_uid:
        raise ValueError("TrialBrief batch identity differs")
    if ack.row_index != row_index or closure.row_index != row_index:
        raise ValueError("TrialBrief row identity differs")
    if ack.trial_uid != trial_uid or closure.trial_uid != trial_uid:
        raise ValueError("TrialBrief trial identity differs")
    if ack.control_candidate_uid != row.control_candidate_uid:
        raise ValueError("TrialBrief control candidate differs from actual overlay")
    if closure.ack_uid != ack.ack_uid or not closure.post_ack_verified:
        raise ValueError("TrialBrief requires exact ACK and post-ACK closure")
    if legacy_trial_number == 21 and (
        metric_role is not MetricRole.UNAVAILABLE or objective is not None
    ):
        raise ValueError("legacy trial 21 force metric must remain unavailable/null")
    if metric_role is MetricRole.UNAVAILABLE and objective is not None:
        raise ValueError("unavailable metric must be null")
    for name, digest in artifact_digests.items():
        if not name or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("artifact digests must be named lowercase SHA256 values")
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
        publication_unique=True,
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
        "objective": gate.objective,
        "oracle_status": oracle_status.value,
        "observer_status": observer_status.value,
        "optimizer_eligible": gate.optimizer_eligible,
        "optimizer_rejection_reasons": list(gate.rejection_reasons),
        "artifact_digests": dict(sorted(artifact_digests.items())),
        "fingerprint_verified": fingerprint_verified,
    }
    return TrialBrief(strict_json_loads(canonical_json_bytes(document)))


class EvidenceSink:
    def __init__(self, root: Path) -> None:
        self.root = root

    def publish_trial_brief(self, brief: TrialBrief) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{brief.publication_uid}.trial-brief.json"
        payload = canonical_json_bytes(brief.document) + b"\n"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise FileExistsError("TrialBrief publication identity collision")
            return path
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
        directory = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return path
