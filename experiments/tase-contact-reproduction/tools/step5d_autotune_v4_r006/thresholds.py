"""Required, hash-bound r006 runtime threshold receipt."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .contracts import R006Contract, canonical_bytes


class ThresholdReceiptError(ValueError):
    """A live ARM threshold receipt is missing, malformed, or unbound."""


SCHEMA = "step5d.autotune-v4/r006-runtime-thresholds-v1"
VERSION = "r006-v1"


def _digest(value: Any, role: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ThresholdReceiptError(f"{role} must be a lowercase SHA-256")
    return value


def _number(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ThresholdReceiptError(f"{role} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ThresholdReceiptError(f"{role} must be finite and positive")
    return result


@dataclass(frozen=True)
class ThresholdReceipt:
    schema: str
    version: str
    pac_epsilon_n: float
    application_mae_threshold_n: float
    contract_sha256: str
    campaign_fingerprint: str
    issued_at_unix_s: float
    receipt_sha256: str

    def __post_init__(self) -> None:
        if self.schema != SCHEMA or self.version != VERSION:
            raise ThresholdReceiptError("threshold receipt schema/version differs")
        _number(self.pac_epsilon_n, "pac_epsilon_n")
        _number(self.application_mae_threshold_n, "application_mae_threshold_n")
        _digest(self.contract_sha256, "contract_sha256")
        _digest(self.campaign_fingerprint, "campaign_fingerprint")
        _digest(self.receipt_sha256, "receipt_sha256")
        if isinstance(self.issued_at_unix_s, bool) or not math.isfinite(float(self.issued_at_unix_s)):
            raise ThresholdReceiptError("issued_at_unix_s is invalid")

    def _unsigned(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "pac_epsilon_n": self.pac_epsilon_n,
            "application_mae_threshold_n": self.application_mae_threshold_n,
            "contract_sha256": self.contract_sha256,
            "campaign_fingerprint": self.campaign_fingerprint,
            "issued_at_unix_s": self.issued_at_unix_s,
        }

    def validate_seal(self) -> None:
        expected = hashlib.sha256(canonical_bytes(self._unsigned())).hexdigest()
        if expected != self.receipt_sha256:
            raise ThresholdReceiptError("threshold receipt seal differs")

    def validate_for_contract(self, contract: R006Contract) -> None:
        if self.contract_sha256 != contract.sha256:
            raise ThresholdReceiptError("threshold receipt contract binding differs")
        if self.campaign_fingerprint != contract.campaign_fingerprint:
            raise ThresholdReceiptError("threshold receipt campaign binding differs")
        self.validate_seal()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ThresholdReceipt":
        required = {
            "schema", "version", "pac_epsilon_n", "application_mae_threshold_n",
            "contract_sha256", "campaign_fingerprint", "issued_at_unix_s", "receipt_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise ThresholdReceiptError("threshold receipt fields differ")
        try:
            receipt = cls(
                schema=str(value["schema"]),
                version=str(value["version"]),
                pac_epsilon_n=value["pac_epsilon_n"],
                application_mae_threshold_n=value["application_mae_threshold_n"],
                contract_sha256=str(value["contract_sha256"]),
                campaign_fingerprint=str(value["campaign_fingerprint"]),
                issued_at_unix_s=float(value["issued_at_unix_s"]),
                receipt_sha256=str(value["receipt_sha256"]),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise ThresholdReceiptError("threshold receipt values are invalid") from exc
        receipt.validate_seal()
        return receipt


def load_threshold_receipt(path: Path, *, contract: R006Contract) -> ThresholdReceipt:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ThresholdReceiptError("threshold receipt must be a regular file")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ThresholdReceiptError("threshold receipt is not strict JSON") from exc
    receipt = ThresholdReceipt.from_mapping(raw)
    receipt.validate_for_contract(contract)
    return receipt


__all__ = ["SCHEMA", "VERSION", "ThresholdReceipt", "ThresholdReceiptError", "load_threshold_receipt"]
