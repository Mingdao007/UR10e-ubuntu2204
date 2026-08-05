"""r008 overlay: bound the r006 objective sidecar's per-append cold-read cost.

Root cause (found live, 2026-08-03): ``R006ObjectiveSidecar.append`` -- called
once after *every* completed attempt -- ends with
``self._cached = self._verify_rows(cold_read=True)``. ``_verify_rows`` with
``cold_read=True`` re-derives *every* historical row's objective receipt by
spawning one subprocess per row (``_fresh_verify_artifact``, ~20s timeout
each). So every single new attempt pays a cost proportional to the *entire*
campaign so far, not just its own new row -- an O(n^2) hot-path cost across a
campaign, distinct from (and in addition to) the cold-resume-only O(n) cost
``bounded_resume_ledger.py`` already bounds.

This was directly observed: the wall-clock gap between consecutive formal
trials in ``live_20260803_1113_stage_d`` grew from ~156s to ~307s over 30
trials, almost doubling, while the formal PATH+contact window itself stayed a
fixed 60s the whole time -- i.e. the growth is pure per-append verification
overhead, not motion time. The user's target is a full cycle within ~75s
(60s contact + ~10-15s move/search); a slowly quadratic sidecar re-verify on
every append makes that target unreachable past a handful of attempts.

The row-level hash chain inside ``_verify_rows`` (``previous_sha256`` /
``row_sha256``, checked unconditionally regardless of ``cold_read``) already
protects every row's own stored fields against tampering. What the per-row
subprocess re-verification *additionally* catches is the artifact file on
disk silently diverging from what the sidecar row claims. That risk is
concentrated in the newest rows; older rows were already cold-read verified
once (when they themselves were the tail) and are protected against later
tampering by the hash chain.

This process-local, r008-scoped patch bounds the expensive per-row
subprocess re-verification to the newest ``tail_rows`` (default 1) rows on
every ``append``/construct, and reuses the previous call's cached enriched
row for older, hash-chain-confirmed-unchanged rows instead of re-deriving it.
It changes only r008's process; ``tools/step5d_autotune_v4_r006/sidecar.py``
is not edited, so r006/r007 are byte-for-byte unaffected.

Default lowered 5→1 (2026-08-05): after PATH60‖seal fork isolation, the parent
still pays an un-forked ``fresh_process_verify`` inside the PATH60 window; only
the newly written row needs subprocess cold verify — older tail rows already
passed cold verify on prior appends and remain hash-chain protected.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import MappingProxyType
from typing import Any, Iterator, Mapping

R008_SIDECAR_TAIL_ROWS = 1


def _bounded_verify_rows(self: Any, *, cold_read: bool) -> tuple[Mapping[str, Any], ...]:
    from step5d_autotune_v4_r006 import sidecar as mod

    if not cold_read:
        rows = mod._read_rows(self.path)
        # Mirror the original's warm (non-cold) path exactly: no subprocess,
        # trust the artifact bytes on disk as-is. Preserved for parity; r008's
        # own hot path always calls with cold_read=True.
        previous = mod.GENESIS_SHA256
        seen: set[tuple[int, str]] = set()
        verified_rows: list[Mapping[str, Any]] = []
        header_expected = {
            "schema": mod.SIDECAR_SCHEMA,
            "record_type": "header",
            "campaign_fingerprint": self.campaign_fingerprint,
            "objective_authority": "fresh_subprocess_raw_recompute_only",
        }
        if not rows or rows[0] != header_expected:
            raise mod.R006SidecarError("r006 sidecar header differs")
        for number, raw in enumerate(rows[1:], 2):
            row = dict(raw)
            _check_row_shape(mod, row, self.campaign_fingerprint, number, previous, seen)
            artifact = self._artifact_path(str(row.get("artifact_name", "")))
            encoded = artifact.read_bytes()
            receipt = mod.R006ObjectiveReceipt.from_mapping(__import__("json").loads(encoded))
            verified_rows.append(_enrich(row, receipt, artifact))
            previous = str(row["row_sha256"])
        return tuple(verified_rows)

    rows = mod._read_rows(self.path)
    header_expected = {
        "schema": mod.SIDECAR_SCHEMA,
        "record_type": "header",
        "campaign_fingerprint": self.campaign_fingerprint,
        "objective_authority": "fresh_subprocess_raw_recompute_only",
    }
    if not rows or rows[0] != header_expected:
        raise mod.R006SidecarError("r006 sidecar header differs")

    body = rows[1:]
    tail_start = max(0, len(body) - R008_SIDECAR_TAIL_ROWS)
    cached_by_identity = {
        (int(row.get("attempt_sequence", -1)), str(row.get("execution_id", ""))): row
        for row in getattr(self, "_cached", ())
    }

    previous = mod.GENESIS_SHA256
    seen: set[tuple[int, str]] = set()
    verified_rows: list[Mapping[str, Any]] = []
    for position, raw in enumerate(body):
        number = position + 2
        row = dict(raw)
        identity = _check_row_shape(mod, row, self.campaign_fingerprint, number, previous, seen)
        artifact = self._artifact_path(str(row.get("artifact_name", "")))
        encoded = artifact.read_bytes()
        if mod._sha(encoded) != row.get("artifact_sha256") or len(encoded) != row.get("artifact_size"):
            raise mod.R006SidecarError(f"r006 sidecar row {number} artifact bytes differ")

        cached = cached_by_identity.get(identity)
        if position >= tail_start:
            receipt = mod._fresh_verify_artifact(artifact, self.campaign_fingerprint)
            verified_rows.append(_bound_and_enrich(mod, row, receipt, artifact, identity, number))
        elif cached is not None:
            # Hash chain (above) already reconfirmed this row's own stored
            # fields are unchanged since the last time it was fresh-verified
            # (when it was itself within the tail); trust the prior result
            # instead of spawning another subprocess for it.
            verified_rows.append(cached)
        else:
            # Cold construction (first verify in this process: no warm
            # in-process cache yet -- e.g. every live resume). Without this
            # branch, every non-tail row falls through to the expensive
            # subprocess path above on the FIRST verify call of a fresh
            # process, defeating the tail bound entirely: observed live
            # 2026-08-03 as a ~4.3 minute cold-resume stall re-verifying all
            # 73 historical rows via fresh subprocess despite tail_rows=5.
            # The artifact byte hash (checked above) already protects
            # against silent file drift since this row was last verified;
            # parsing the stored sufficient-statistics receipt in-process
            # (no raw-sample recompute, no subprocess) is the same trust
            # tier bounded_resume_ledger.py already uses for old ledger rows.
            receipt = mod.R006ObjectiveReceipt.from_mapping(json.loads(encoded))
            verified_rows.append(_bound_and_enrich(mod, row, receipt, artifact, identity, number))
        previous = str(row["row_sha256"])

    return tuple(verified_rows)


def _bound_and_enrich(
    mod: Any,
    row: dict[str, Any],
    receipt: Any,
    artifact: Any,
    identity: tuple[int, str],
    number: int,
) -> Mapping[str, Any]:
    if (
        receipt.attempt_sequence != identity[0]
        or receipt.execution_id != identity[1]
        or receipt.metadata.get("candidate_uid") != row.get("candidate_uid")
        or receipt.builder_seal_sha256 != row.get("builder_seal_sha256")
    ):
        raise mod.R006SidecarError(f"r006 sidecar row {number} receipt binding differs")
    return _enrich(row, receipt, artifact)


def _check_row_shape(
    mod: Any,
    row: dict[str, Any],
    campaign_fingerprint: str,
    number: int,
    previous: str,
    seen: set[tuple[int, str]],
) -> tuple[int, str]:
    if row.get("schema") != mod.SIDECAR_SCHEMA or row.get("record_type") != "objective_artifact":
        raise mod.R006SidecarError(f"r006 sidecar row {number} schema differs")
    if row.get("campaign_fingerprint") != campaign_fingerprint:
        raise mod.R006SidecarError(f"r006 sidecar row {number} campaign differs")
    if row.get("previous_sha256") != previous or row.get("row_sha256") != mod._row_sha(row):
        raise mod.R006SidecarError(f"r006 sidecar row {number} hash chain differs")
    identity = (int(row.get("attempt_sequence", 0)), str(row.get("execution_id", "")))
    if identity[0] <= 0 or not identity[1] or identity in seen:
        raise mod.R006SidecarError(f"r006 sidecar row {number} identity differs")
    seen.add(identity)
    return identity


def _enrich(row: Mapping[str, Any], receipt: Any, artifact: Any) -> Mapping[str, Any]:
    enriched = {
        **row,
        "receipt": receipt.as_dict(),
        "trainable": receipt.trainable,
        "objective_mae_n": receipt.objective,
        "artifact_path": str(artifact.resolve(strict=True)),
    }
    return MappingProxyType(enriched)


@contextmanager
def r008_bounded_sidecar_scope(*, tail_rows: int = R008_SIDECAR_TAIL_ROWS) -> Iterator[None]:
    """Process-local patch: bound R006ObjectiveSidecar's per-append cold-read cost."""

    from step5d_autotune_v4_r006 import sidecar as mod

    global R008_SIDECAR_TAIL_ROWS
    previous_tail = R008_SIDECAR_TAIL_ROWS
    R008_SIDECAR_TAIL_ROWS = int(tail_rows)
    original = mod.R006ObjectiveSidecar._verify_rows
    try:
        mod.R006ObjectiveSidecar._verify_rows = _bounded_verify_rows  # type: ignore[assignment]
        yield
    finally:
        mod.R006ObjectiveSidecar._verify_rows = original  # type: ignore[assignment]
        R008_SIDECAR_TAIL_ROWS = previous_tail


__all__ = ["R008_SIDECAR_TAIL_ROWS", "r008_bounded_sidecar_scope"]
