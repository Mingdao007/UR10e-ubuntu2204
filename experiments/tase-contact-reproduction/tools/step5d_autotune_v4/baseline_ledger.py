"""Single-owner, hash-bound qualification ledger for V4 baseline holds."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Mapping

from .contracts import TARGET_FORCE_N, V4Contract


BASELINE_SUCCESS_STAGE = 22
SCHEMA = "step5d.autotune-v4/baseline-qualification-ledger-v1"


class BaselineLedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class BaselineSuccessReceipt:
    attempt_id: str
    terminal_stage: int
    campaign_fingerprint: str
    eoat_sha256: str
    target_force_n: float
    sensor_authority: str
    completion_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.attempt_id, str)
            or not self.attempt_id
            or not isinstance(self.terminal_stage, int)
            or isinstance(self.terminal_stage, bool)
            or self.terminal_stage != BASELINE_SUCCESS_STAGE
        ):
            raise BaselineLedgerError("baseline success receipt identity differs")
        if (
            not isinstance(self.target_force_n, (int, float))
            or isinstance(self.target_force_n, bool)
            or not math.isfinite(float(self.target_force_n))
        ):
            raise BaselineLedgerError("baseline target is not finite")
        if (
            self.sensor_authority != "kunwei_only"
            or not math.isclose(self.target_force_n, TARGET_FORCE_N, abs_tol=1e-12)
        ):
            raise BaselineLedgerError("baseline success invariants differ")
        for role, value in (
            ("campaign fingerprint", self.campaign_fingerprint),
            ("EOAT SHA-256", self.eoat_sha256),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise BaselineLedgerError(f"{role} differs")
        if (
            not isinstance(self.completion_sha256, str)
            or len(self.completion_sha256) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.completion_sha256
            )
        ):
            raise BaselineLedgerError("completion SHA-256 differs")


@dataclass(frozen=True)
class BaselineQualificationSnapshot:
    consecutive_successes: int
    receipt_ids: tuple[str, ...]
    frozen: bool
    freeze_reason: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.consecutive_successes, int)
            or isinstance(self.consecutive_successes, bool)
            or self.consecutive_successes < 0
            or self.consecutive_successes != len(self.receipt_ids)
        ):
            raise BaselineLedgerError("baseline qualification count is invalid")
        if any(
            not isinstance(receipt_id, str) or not receipt_id
            for receipt_id in self.receipt_ids
        ) or len(set(self.receipt_ids)) != len(self.receipt_ids):
            raise BaselineLedgerError("baseline receipt ids are not unique strings")
        if not isinstance(self.frozen, bool) or not isinstance(self.freeze_reason, str):
            raise BaselineLedgerError("baseline qualification freeze state is invalid")
        if not self.frozen and self.freeze_reason:
            raise BaselineLedgerError("unfrozen baseline qualification has a reason")

    @property
    def full_path_allowed(self) -> bool:
        return self.consecutive_successes >= 3 and not self.frozen


@dataclass
class BaselineQualificationLedger:
    """The only runtime owner allowed to change baseline qualification state."""

    contract: V4Contract
    _receipts: dict[str, BaselineSuccessReceipt] = field(default_factory=dict)
    _streak: list[str] = field(default_factory=list)
    _frozen: bool = False
    _freeze_reason: str = ""

    def record_success(self, receipt: BaselineSuccessReceipt) -> bool:
        if self._frozen:
            raise BaselineLedgerError("qualification ledger is frozen")
        self._validate_binding(receipt)
        existing = self._receipts.get(receipt.attempt_id)
        if existing is not None:
            if existing != receipt:
                raise BaselineLedgerError("attempt id conflicts with prior receipt")
            return False
        self._receipts[receipt.attempt_id] = receipt
        self._streak.append(receipt.attempt_id)
        return True

    def record_failure(
        self,
        *,
        attempt_id: str,
        terminal_stage: int,
        reason: str,
        safety_or_structural: bool,
    ) -> None:
        if (
            not isinstance(attempt_id, str)
            or not attempt_id
            or not isinstance(terminal_stage, int)
            or isinstance(terminal_stage, bool)
            or terminal_stage < 0
            or not isinstance(reason, str)
            or not reason
        ):
            raise BaselineLedgerError("failure identity is incomplete")
        if terminal_stage <= BASELINE_SUCCESS_STAGE:
            self._streak.clear()
        if safety_or_structural:
            self._frozen = True
            self._freeze_reason = reason

    def snapshot(self) -> BaselineQualificationSnapshot:
        return BaselineQualificationSnapshot(
            consecutive_successes=len(self._streak),
            receipt_ids=tuple(self._streak),
            frozen=self._frozen,
            freeze_reason=self._freeze_reason,
        )

    def seal(self) -> Mapping[str, object]:
        rows = [
            {
                "attempt_id": value.attempt_id,
                "terminal_stage": value.terminal_stage,
                "campaign_fingerprint": value.campaign_fingerprint,
                "eoat_sha256": value.eoat_sha256,
                "target_force_n": value.target_force_n,
                "sensor_authority": value.sensor_authority,
                "completion_sha256": value.completion_sha256,
            }
            for value in self._receipts.values()
        ]
        payload = {
            "schema": SCHEMA,
            "campaign_fingerprint": self.contract.campaign_fingerprint,
            "eoat_sha256": self.contract.eoat_sha256,
            "receipts": rows,
            "consecutive_success_attempt_ids": list(self._streak),
            "frozen": self._frozen,
            "freeze_reason": self._freeze_reason,
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return {**payload, "ledger_sha256": hashlib.sha256(encoded).hexdigest()}

    def _validate_binding(self, receipt: BaselineSuccessReceipt) -> None:
        if (
            receipt.campaign_fingerprint != self.contract.campaign_fingerprint
            or receipt.eoat_sha256 != self.contract.eoat_sha256
        ):
            raise BaselineLedgerError("baseline receipt binding differs")


__all__ = [
    "BASELINE_SUCCESS_STAGE",
    "BaselineLedgerError",
    "BaselineQualificationLedger",
    "BaselineQualificationSnapshot",
    "BaselineSuccessReceipt",
    "SCHEMA",
]
