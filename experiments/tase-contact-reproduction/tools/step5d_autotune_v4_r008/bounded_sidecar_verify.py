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

Append fast-path (2026-08-05, seal-join cut): ``R006ObjectiveSidecar.append``
still ends with ``_verify_rows(cold_read=True)``, which — even with
``tail_rows=1`` — spawns a *second* ``_fresh_verify_artifact`` on the row that
``append`` just verified. Offline profile on canary 041810's ~18MB /
~28860-sample raw_bundle: that duplicate subprocess alone is ~2.5–2.9s and is
the largest single slice of the post-SAFE_RETURN ``seal_overlap_s≈9s`` hitch.
While the bounded scope is active, ``append`` reuses the in-append fresh
receipt to extend ``_cached`` (hash-chain row already durably written) instead
of re-cold-verifying the same artifact.

Binary raw Phase 1 (2026-08-07): under this scope, formal PATH artifacts write
samples to a companion ``.r008raw`` (R008RAW1) and keep a slim JSON stub.
``_fresh_verify_artifact`` is patched to hydrate + in-process
``cold_read_verify`` (no 18MB JSON parse). Legacy full-JSON artifacts still
verify. Falls back to full JSON when samples are not R008RAW1-compatible
(e.g. unit-test fixtures without kunwei/rtde/tp/writer + qdots).

Phase 4 (2026-08-07): hot ``append`` binds stub+``.r008raw`` bytes and
columnar-verifies from the in-memory builder receipt (no decode /
``from_mapping``). Cold-tail still hydrates from disk.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from step5d_autotune_v4_r008.binary_seal import (
    is_binary_seal_receipt,
    pop_pending_raw2,
    r008_binary_seal_scope,
)
from step5d_autotune_v4_r008.raw_force_binary import (
    load_receipt_mapping,
    load_receipt_mapping_from_bytes,
    prepare_slim_receipt,
    write_raw_sidecar_bytes,
)
from step5d_autotune_v4_r008.raw_force_binary_v2 import seal_sha256_of
from step5d_autotune_v4_r008.raw_force_columnar_verify import (
    columnar_verify_hot_append,
    columnar_verify_receipt,
)

R008_SIDECAR_TAIL_ROWS = 1


def _r008_fresh_verify_artifact(path: Path, campaign_fingerprint: str) -> Any:
    """Hydrate R008RAW1 (if present) then columnar- or cold-verify in-process.

    Phase-5 binary receipts keep empty seal samples; bind sibling R008RAW2 via
    ``cold_read_verify(..., artifact_raw_bytes=...)``.
    """

    from step5d_autotune_v4_r006 import objective as obj_mod
    from step5d_autotune_v4_r006.sidecar import R006SidecarError

    try:
        artifact = Path(path)
        payload = load_receipt_mapping(artifact)
        # Late attribute lookup so r008_binary_seal_scope patches apply.
        receipt = obj_mod.R006ObjectiveReceipt.from_mapping(payload)
        sibling = artifact.with_suffix(".r008raw")
        has_raw = sibling.is_file() and not sibling.is_symlink()
        cold_read_verify = obj_mod.cold_read_verify
        if is_binary_seal_receipt(receipt):
            if not has_raw:
                raise R006SidecarError("r008 binary seal artifact missing .r008raw")
            verified = cold_read_verify(
                receipt,
                expected_campaign_fingerprint=campaign_fingerprint,
                artifact_raw_bytes=sibling.read_bytes(),
            )
        else:
            samples = (
                receipt.raw_bundle.get("samples")
                if isinstance(receipt.raw_bundle, Mapping)
                else None
            )
            if has_raw or (isinstance(samples, list) and samples):
                try:
                    verified = columnar_verify_receipt(
                        receipt, expected_campaign_fingerprint=campaign_fingerprint
                    )
                except Exception:
                    # Fail closed to stock rebuild when columnar disagrees/errors.
                    verified = cold_read_verify(
                        receipt, expected_campaign_fingerprint=campaign_fingerprint
                    )
            else:
                verified = cold_read_verify(
                    receipt, expected_campaign_fingerprint=campaign_fingerprint
                )
    except Exception as exc:  # noqa: BLE001 -- mirror sidecar fail-closed
        raise R006SidecarError(f"r006 fresh artifact verification failed: {exc}") from exc
    if not verified.trainable:
        raise R006SidecarError("r006 fresh verifier did not grant trainable capability")
    return verified


def _receipt_from_artifact_bytes(encoded: bytes, artifact: Path) -> Any:
    from step5d_autotune_v4_r006.objective import R006ObjectiveReceipt

    payload = load_receipt_mapping_from_bytes(encoded, artifact_path=Path(artifact))
    return R006ObjectiveReceipt.from_mapping(payload)


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
            receipt = _receipt_from_artifact_bytes(encoded, artifact)
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
            # Hydrate R008RAW1 when the JSON stub has empty samples.
            receipt = _receipt_from_artifact_bytes(encoded, artifact)
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


def _bounded_append(
    self: Any,
    receipt: Any,
    *,
    epoch: int,
    candidate_uid: str,
    kind: str,
    point_key: list[Any],
) -> Mapping[str, Any]:
    """Like ``R006ObjectiveSidecar.append`` but one fresh-verify, not two.

    Writes the immutable artifact, runs a single ``_fresh_verify_artifact``,
    appends the hash-chain row, then extends ``_cached`` from that verified
    receipt. Skips the trailing ``_verify_rows(cold_read=True)`` that would
    otherwise re-spawn the same subprocess on the brand-new tail row.
    """

    from step5d_autotune_v4_r006 import sidecar as mod

    if not isinstance(receipt, mod.R006ObjectiveReceipt):
        raise mod.R006SidecarError("r006 append requires a typed builder receipt")
    if receipt.verification_state not in {"builder_sealed", "verified_raw_artifact"}:
        raise mod.R006SidecarError("r006 receipt state is invalid")
    if receipt.campaign_fingerprint != self.campaign_fingerprint:
        raise mod.R006SidecarError("r006 receipt campaign differs")
    if receipt.metadata.get("candidate_uid") != candidate_uid:
        raise mod.R006SidecarError("r006 receipt candidate binding differs")
    matches = [
        row for row in self._cached if row["attempt_sequence"] == receipt.attempt_sequence
    ]
    if matches:
        existing = matches[0]
        if (
            existing["execution_id"] == receipt.execution_id
            and existing["builder_seal_sha256"] == receipt.builder_seal_sha256
            and existing["candidate_uid"] == candidate_uid
        ):
            return existing
        raise mod.R006SidecarError("r006 attempt sequence already has different evidence")
    identity = {
        "campaign_fingerprint": self.campaign_fingerprint,
        "attempt_sequence": receipt.attempt_sequence,
        "execution_id": receipt.execution_id,
        "candidate_uid": candidate_uid,
        "builder_seal_sha256": receipt.builder_seal_sha256,
    }
    artifact_name = mod._sha(mod.canonical_bytes(identity)) + ".json"
    target = self._artifact_path(artifact_name)
    receipt_dict = receipt.as_dict()
    # Phase 5: finalize already produced slim receipt + pending R008RAW2 bytes.
    raw_payload = pop_pending_raw2(receipt.attempt_sequence, receipt.execution_id)
    if raw_payload is None:
        # Prefer R008RAW1/legacy slim for pre-Phase-5 receipts.
        raw_payload = prepare_slim_receipt(receipt_dict, artifact_json_path=target)
    encoded = mod.canonical_bytes(receipt_dict) + b"\n"
    if target.exists():
        if not target.is_file() or target.read_bytes() != encoded:
            raise mod.R006SidecarError("r006 immutable artifact identity collision")
    else:
        if raw_payload is not None:
            write_raw_sidecar_bytes(target.with_suffix(".r008raw"), raw_payload)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".r006-", suffix=".tmp", dir=self.artifact_root
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            mod._fsync_dir(self.artifact_root)
            mod._fsync_dir(self.path.parent)
        finally:
            temporary.unlink(missing_ok=True)
    # Phase 4/5: bind disk bytes + columnar bins. Phase-5 binary receipts bind
    # seal-block SHA (already in raw_bundle_digest) instead of JSON bundle SHA.
    if raw_payload is not None and is_binary_seal_receipt(receipt):
        if seal_sha256_of(raw_payload) != receipt.raw_bundle_digest:
            raise mod.R006SidecarError("r008raw seal-block digest differs from receipt")
        if target.read_bytes() != encoded:
            raise mod.R006SidecarError("artifact stub bytes differ from local write")
        if target.with_suffix(".r008raw").read_bytes() != raw_payload:
            raise mod.R006SidecarError("r008raw bytes differ from local encode")
        # Bins already sealed in finalize; mark verified without re-SHA fat bundle.
        from dataclasses import replace

        verified = replace(receipt, verification_state="verified_raw_artifact")
    elif raw_payload is not None:
        try:
            verified = columnar_verify_hot_append(
                receipt,
                expected_campaign_fingerprint=self.campaign_fingerprint,
                artifact_path=target,
                encoded_stub=encoded,
                raw_payload=raw_payload,
            )
        except Exception:
            verified = _r008_fresh_verify_artifact(target, self.campaign_fingerprint)
    elif target.with_suffix(".r008raw").is_file():
        verified = _r008_fresh_verify_artifact(target, self.campaign_fingerprint)
    else:
        verified = mod._fresh_verify_artifact(target, self.campaign_fingerprint)
    previous = (
        str(self._cached[-1]["row_sha256"]) if self._cached else mod.GENESIS_SHA256
    )
    row: dict[str, Any] = {
        "schema": mod.SIDECAR_SCHEMA,
        "record_type": "objective_artifact",
        "campaign_fingerprint": self.campaign_fingerprint,
        "epoch": int(epoch),
        "attempt_sequence": receipt.attempt_sequence,
        "execution_id": receipt.execution_id,
        "candidate_uid": candidate_uid,
        "kind": str(kind),
        "point_key": list(point_key),
        "artifact_name": artifact_name,
        "artifact_sha256": mod._sha(encoded),
        "artifact_size": len(encoded),
        "builder_seal_sha256": verified.builder_seal_sha256,
        "previous_sha256": previous,
    }
    row["row_sha256"] = mod._row_sha(row)
    with self.path.open("ab") as stream:
        stream.write(mod.canonical_bytes(row) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    mod._fsync_dir(self.path.parent)
    enriched = _enrich(row, verified, target)
    self._cached = tuple(self._cached) + (enriched,)
    return self._cached[-1]


@contextmanager
def r008_bounded_sidecar_scope(*, tail_rows: int = R008_SIDECAR_TAIL_ROWS) -> Iterator[None]:
    """Process-local patch: bound R006ObjectiveSidecar's per-append cold-read cost."""

    from step5d_autotune_v4_r006 import sidecar as mod

    global R008_SIDECAR_TAIL_ROWS
    previous_tail = R008_SIDECAR_TAIL_ROWS
    R008_SIDECAR_TAIL_ROWS = int(tail_rows)
    original_verify = mod.R006ObjectiveSidecar._verify_rows
    original_append = mod.R006ObjectiveSidecar.append
    original_fresh = mod._fresh_verify_artifact
    try:
        mod.R006ObjectiveSidecar._verify_rows = _bounded_verify_rows  # type: ignore[assignment]
        mod.R006ObjectiveSidecar.append = _bounded_append  # type: ignore[assignment]
        mod._fresh_verify_artifact = _r008_fresh_verify_artifact  # type: ignore[assignment]
        with r008_binary_seal_scope():
            yield
    finally:
        mod.R006ObjectiveSidecar._verify_rows = original_verify  # type: ignore[assignment]
        mod.R006ObjectiveSidecar.append = original_append  # type: ignore[assignment]
        mod._fresh_verify_artifact = original_fresh  # type: ignore[assignment]
        R008_SIDECAR_TAIL_ROWS = previous_tail


__all__ = ["R008_SIDECAR_TAIL_ROWS", "r008_bounded_sidecar_scope"]
