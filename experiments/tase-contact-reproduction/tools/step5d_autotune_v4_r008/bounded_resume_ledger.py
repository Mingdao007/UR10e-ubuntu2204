"""r008 bounded-tail cold-resume verification.

Both constructing an ``ObservationLedger`` over an existing file and calling
``fresh_process_verify()`` later route through the same private method,
``_fresh_audit_and_cache()``: it re-derives *every* non-QUALIFICATION row's
force objective by re-reading and recomputing from its raw sample file (tens
of thousands of samples per formal 60s trial, in one subprocess pass over the
whole ledger). That cost is paid once per row, for every row, every time the
ledger object is constructed or re-verified -- so it grows with total
campaign length even though each row was already fresh-verified once, at
append time, when it was sealed. Empirically this exceeded even the
r008-raised 600s budget (``fresh_verify.R008_FRESH_VERIFY_TIMEOUT_S``) once a
campaign accumulated ~30 formal rows (2026-08-03), surfacing as
``fresh-process ledger raw-evidence verification failed`` on cold resume --
before any trial is dispatched, so no live motion is attempted, but resuming
a large campaign becomes impossible without dropping its history.

The ledger's row-level hash chain (``verify_hash_chain``) already protects
every row's own stored ``force_objective``/``raw_artifact`` fields against
tampering: it is cheap (pure hash comparisons over the small row JSON, no raw
artifact file I/O) and is always run regardless (``ObservationLedger.__init__``
runs it directly, independent of ``_fresh_audit_and_cache``). What the
expensive recomputation *additionally* catches is the raw artifact *file*
silently diverging from what the ledger row claims, independent of the
ledger's own integrity. That risk is concentrated in the most recent rows
(e.g. a crash mid-write left an inconsistent tail); older rows were already
fresh-verified once and are protected by the hash chain against any
subsequent tampering of what they claim.

This bounds the expensive per-row recomputation to the newest ``tail_rows``
(default 5) non-QUALIFICATION rows and trusts older sealed rows' own stored
fields. It changes only r008's cold-resume/construction path; the hot
per-append verify path (``ObservationLedger.append``) is untouched and still
fresh-verifies every new row exactly once, as before.
"""

from __future__ import annotations

from typing import Any

from step5d_autotune_v4_r005.observations import (
    ObservationError,
    verify_hash_chain,
)

from step5d_autotune_v4_r007.native_ledger import R007NativeObservationLedger


class R008BoundedResumeObservationLedger(R007NativeObservationLedger):
    """r007 native ledger with a bounded-cost cold-resume/construct verify."""

    def __init__(self, *args: Any, tail_rows: int = 5, **kwargs: Any) -> None:
        if int(tail_rows) < 1:
            raise ObservationError("r008 bounded resume tail_rows must be positive")
        self._bounded_tail_rows = int(tail_rows)
        super().__init__(*args, **kwargs)

    def _fresh_audit_and_cache(self) -> dict[str, Any]:
        from step5d_autotune_v4_r005 import observations as obs

        rows = verify_hash_chain(self.path)
        non_qual_positions = [
            index for index, row in enumerate(rows) if row.get("kind") != "QUALIFICATION"
        ]
        tail = set(non_qual_positions[-self._bounded_tail_rows :])

        details: dict[str, Any] = {}
        for index, row in enumerate(rows):
            if row.get("kind") == "QUALIFICATION":
                continue
            sequence = row["attempt_sequence"]
            raw_artifact = row.get("raw_artifact")
            if not isinstance(raw_artifact, dict):
                raise ObservationError("r008 bounded resume: row lacks raw artifact binding")
            if index in tail:
                artifact_path = self._artifact_path(raw_artifact["relative_path"])
                info = obs._run_fresh("artifact", artifact_path)
                fresh_objective = info.get("objective")
                if fresh_objective != row.get("force_objective"):
                    raise ObservationError(
                        f"r008 bounded resume: row {sequence} objective differs from fresh artifact"
                    )
                if info.get("byte_sha256") != raw_artifact.get("byte_sha256"):
                    raise ObservationError(
                        f"r008 bounded resume: row {sequence} artifact bytes differ from ledger binding"
                    )
                details[str(sequence)] = {"objective": fresh_objective, "raw_artifact": raw_artifact}
            else:
                # Hash chain already confirmed this row's own stored fields
                # are untampered; trust them without re-reading the
                # (potentially tens-of-megabyte) raw sample file again.
                details[str(sequence)] = {
                    "objective": row.get("force_objective"),
                    "raw_artifact": raw_artifact,
                }

        records = tuple(self._record_from_row(row, details) for row in rows)
        self._set_cache(rows, records)
        return {"ledger_sha256": self._cached_head_sha256, "details": details, "bounded_resume": True}


__all__ = ["R008BoundedResumeObservationLedger"]
