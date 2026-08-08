"""Thin r007 cold-read seam for native r006 candidates.

The frozen r005 ledger still owns hash-chain, raw-artifact, objective, and
sealed-record verification.  This additive subclass changes only the candidate
constructor used while reconstructing those already sealed rows: the canonical
r006 payload is decoded by ``R006Candidate.from_canonical`` so its typed I-mode
and quarter-octave domain remain intact through the r006 sidecar, optimizer,
and refill paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from step5d_autotune_v4_r005.observations import (
    ObservationError,
    ObservationLedger,
    ObservationRecord,
    RawArtifactBinding,
    VerifiedObservationEvidence,
)
from step5d_force_objective import ForceObjective, ForceObjectiveError
from step5d_autotune_v4_r006.live_adapter import (
    R006Candidate,
    R006LiveAdapterError,
    R006ObservationLedger,
)


class R007NativeObservationLedger(ObservationLedger):
    """r005 durable ledger with an r006-native candidate reconstruction seam."""

    def append(self, record: ObservationRecord) -> ObservationRecord:
        if not isinstance(record, ObservationRecord):
            raise ObservationError("r007 ledger append requires an ObservationRecord")
        if not isinstance(record.candidate, R006Candidate):
            raise ObservationError("r007 ledger append requires an r006 native candidate")
        return super().append(record)

    def _record_from_row(
        self,
        row: Mapping[str, Any],
        details: Mapping[str, Any],
    ) -> ObservationRecord:
        """Rebuild a sealed row without the r005 bounded Candidate constructor."""

        candidate_payload = dict(row["candidate"])
        try:
            candidate = R006Candidate.from_canonical(candidate_payload)
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
                candidate=candidate,
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
        except (
            KeyError,
            TypeError,
            ValueError,
            ForceObjectiveError,
            ObservationError,
            R006LiveAdapterError,
        ) as exc:
            raise ObservationError("r007 native sealed observation reconstruction failed") from exc

    def bootstrap_anchor_objectives(self) -> tuple[float, ...]:
        """Keep the inherited objective view native when checking the anchor."""

        anchor = R006Candidate()
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


def open_r007_r006_ledger(
    path: Path,
    *,
    campaign_fingerprint: str,
    eoat_sha256: str,
    artifact_root: Path | None = None,
) -> R006ObservationLedger:
    """Open the existing r006 sidecar pairing over an r007 native ledger."""

    ledger = R007NativeObservationLedger(
        path,
        campaign_fingerprint=campaign_fingerprint,
        eoat_sha256=eoat_sha256,
        artifact_root=artifact_root,
    )
    return R006ObservationLedger(
        ledger,
        campaign_fingerprint=campaign_fingerprint,
    )


__all__ = [
    "R007NativeObservationLedger",
    "open_r007_r006_ledger",
]
