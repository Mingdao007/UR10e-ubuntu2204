"""Exactly-once optimizer tell journal for Autotuner V5.

The campaign proposal layer is stateless between asks and reconstructs its GP
observations from committed campaign outcomes.  This journal is the durable
optimizer-side authority for ``tell_exact``: an AUTHORIZED physical record is
written at most once, and a crash can only replay the same receipt, never mint
a second observation.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

try:
    from step6_figure8_autotune_v1.v5_lifecycle_ledger import (
        FigureEightPhysicalRecordV2,
        LedgerRole,
        OptimizerReceiptV2,
        TellAuthorizationV2,
        TellState,
        canonical_sha256,
    )
except ModuleNotFoundError:  # pragma: no cover
    from tools.step6_figure8_autotune_v1.v5_lifecycle_ledger import (
        FigureEightPhysicalRecordV2,
        LedgerRole,
        OptimizerReceiptV2,
        TellAuthorizationV2,
        TellState,
        canonical_sha256,
    )


V5_OPTIMIZER_JOURNAL_SCHEMA = "step6.autotune/figure8-v5-optimizer-journal-v1"
V5_OPTIMIZER_JOURNAL_VERSION = 1
GENESIS_SHA256 = "0" * 64


class V5OptimizerJournalError(RuntimeError):
    """The V5 optimizer authority journal failed closed."""


def _sha(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise V5OptimizerJournalError(f"{role} must be a lowercase SHA-256")
    return value


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise V5OptimizerJournalError("optimizer journal value is not canonical JSON") from exc


class V5OptimizerTellJournalV1:
    def __init__(
        self,
        path: Path,
        *,
        campaign_fingerprint: str,
        release_identity_sha256: str,
        role: LedgerRole,
    ) -> None:
        self.path = Path(path).resolve()
        self.campaign_fingerprint = _sha(
            campaign_fingerprint, "optimizer campaign fingerprint"
        )
        self.release_identity_sha256 = _sha(
            release_identity_sha256, "optimizer release identity"
        )
        if not isinstance(role, LedgerRole):
            raise TypeError("optimizer journal role must be typed")
        self.role = role
        if self.path.is_symlink():
            raise V5OptimizerJournalError("optimizer journal must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("x", encoding="utf-8") as stream:
                stream.write(_canonical(self._header()) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        self._receipts: dict[str, tuple[OptimizerReceiptV2, dict[str, Any]]] = {}
        self._head_sha256 = GENESIS_SHA256
        self.cold_verify()

    def _header(self) -> dict[str, Any]:
        return {
            "schema": V5_OPTIMIZER_JOURNAL_SCHEMA,
            "version": V5_OPTIMIZER_JOURNAL_VERSION,
            "record_type": "header",
            "campaign_fingerprint": self.campaign_fingerprint,
            "release_identity_sha256": self.release_identity_sha256,
            "role": self.role.value,
            "genesis_sha256": GENESIS_SHA256,
        }

    @property
    def head_sha256(self) -> str:
        return self._head_sha256

    @property
    def tell_count(self) -> int:
        return len(self._receipts)

    def cold_verify(self) -> str:
        try:
            rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise V5OptimizerJournalError("optimizer journal is unreadable") from exc
        if not rows or rows[0] != self._header():
            raise V5OptimizerJournalError("optimizer journal header differs")
        previous = GENESIS_SHA256
        receipts: dict[str, tuple[OptimizerReceiptV2, dict[str, Any]]] = {}
        for row in rows[1:]:
            unsigned = {key: value for key, value in row.items() if key != "row_sha256"}
            if (
                row.get("schema") != V5_OPTIMIZER_JOURNAL_SCHEMA
                or row.get("version") != V5_OPTIMIZER_JOURNAL_VERSION
                or row.get("record_type") != "tell_exact"
                or row.get("campaign_fingerprint") != self.campaign_fingerprint
                or row.get("release_identity_sha256") != self.release_identity_sha256
                or row.get("role") != self.role.value
                or row.get("previous_sha256") != previous
                or row.get("row_sha256") != canonical_sha256(unsigned)
            ):
                raise V5OptimizerJournalError("optimizer journal hash/namespace differs")
            receipt = OptimizerReceiptV2.from_mapping(row.get("optimizer_receipt", {}))
            trial_id = receipt.trial_id
            if trial_id in receipts:
                raise V5OptimizerJournalError("optimizer trial was told more than once")
            if row.get("tell_token") != receipt.tell_token or row.get(
                "record_sha256"
            ) != receipt.record_sha256:
                raise V5OptimizerJournalError("optimizer tell receipt binding differs")
            candidate = row.get("candidate")
            mae_n = row.get("formal_mae_n")
            if not isinstance(candidate, Mapping) or isinstance(mae_n, bool):
                raise V5OptimizerJournalError("optimizer observation is incomplete")
            observation = {
                "candidate": dict(candidate),
                "formal_mae_n": float(mae_n),
                "record_sha256": receipt.record_sha256,
                "tell_token": receipt.tell_token,
            }
            receipts[trial_id] = (receipt, observation)
            previous = row["row_sha256"]
        self._receipts = receipts
        self._head_sha256 = previous
        return previous

    def receipt_for(self, trial_id: str) -> OptimizerReceiptV2 | None:
        self.cold_verify()
        item = self._receipts.get(trial_id)
        return None if item is None else item[0]

    def tell_exact(
        self,
        authorization: TellAuthorizationV2,
        record: FigureEightPhysicalRecordV2,
        *,
        candidate: Mapping[str, Any],
    ) -> OptimizerReceiptV2:
        self.cold_verify()
        if not isinstance(authorization, TellAuthorizationV2) or authorization.state is not TellState.AUTHORIZED:
            raise V5OptimizerJournalError("optimizer tell requires durable AUTHORIZED state")
        if not isinstance(record, FigureEightPhysicalRecordV2) or not record.eligible:
            raise V5OptimizerJournalError("optimizer tell requires one eligible physical record")
        token = record.tell_token()
        if (
            authorization.trial_id != record.trial_id
            or authorization.record_sha256 != record.record_sha256
            or authorization.tell_token != token
            or record.campaign_fingerprint != self.campaign_fingerprint
            or record.release_identity_sha256 != self.release_identity_sha256
            or record.role is not self.role
        ):
            raise V5OptimizerJournalError("optimizer authorization/record identity differs")
        candidate_value = dict(candidate)
        receipt_id = "v5-opt-" + hashlib.sha256(
            _canonical(
                {
                    "trial_id": record.trial_id,
                    "tell_token": token,
                    "record_sha256": record.record_sha256,
                    "candidate": candidate_value,
                    "formal_mae_n": record.metric_snapshot.formal_mae_n,
                }
            ).encode("utf-8")
        ).hexdigest()
        receipt = OptimizerReceiptV2(
            record.trial_id,
            token or "",
            record.record_sha256,
            receipt_id,
        )
        existing = self._receipts.get(record.trial_id)
        if existing is not None:
            if existing[0] != receipt or existing[1]["candidate"] != candidate_value:
                raise V5OptimizerJournalError("optimizer tell replay differs")
            return existing[0]
        row = {
            "schema": V5_OPTIMIZER_JOURNAL_SCHEMA,
            "version": V5_OPTIMIZER_JOURNAL_VERSION,
            "record_type": "tell_exact",
            "campaign_fingerprint": self.campaign_fingerprint,
            "release_identity_sha256": self.release_identity_sha256,
            "role": self.role.value,
            "previous_sha256": self._head_sha256,
            "trial_id": record.trial_id,
            "tell_token": token,
            "record_sha256": record.record_sha256,
            "candidate": candidate_value,
            "formal_mae_n": record.metric_snapshot.formal_mae_n,
            "optimizer_receipt": receipt.as_dict(),
        }
        row["row_sha256"] = canonical_sha256(row)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(_canonical(row) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.cold_verify()
        return receipt


__all__ = [
    "V5OptimizerJournalError",
    "V5OptimizerTellJournalV1",
    "V5_OPTIMIZER_JOURNAL_SCHEMA",
    "V5_OPTIMIZER_JOURNAL_VERSION",
]
