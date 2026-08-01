"""Fresh receipt and uninterrupted-session identity gates for r004."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .contracts import (
    CONTROLLER_READBACK_MAX_AGE_S,
    SCRIPT1_RECEIPT_MAX_AGE_S,
    SCRIPT1_TARGET_POSE,
    R004Contract,
    R004ContractError,
    digest,
)


def _vector(value: Iterable[float], size: int, role: str) -> tuple[float, ...]:
    result = tuple(float(item) for item in value)
    if len(result) != size or not all(math.isfinite(item) for item in result):
        raise R004ContractError(f"{role} must contain {size} finite values")
    return result


def _age(now_s: float, observed_at_s: float, role: str, maximum_s: float) -> None:
    if not math.isfinite(now_s) or not math.isfinite(observed_at_s):
        raise R004ContractError(f"{role} timestamp is nonfinite")
    age = now_s - observed_at_s
    if age < 0.0 or age > maximum_s:
        raise R004ContractError(f"{role} receipt is stale or from the future")


@dataclass(frozen=True)
class Script1StartReceipt:
    receipt_sha256: str
    script_sha256: str
    observed_at_s: float
    final_pose: tuple[float, float, float, float, float, float]
    final_q: tuple[float, float, float, float, float, float]
    stationary: bool
    safety_mode: str
    eoat_identity_sha256: str

    def __post_init__(self) -> None:
        digest(self.receipt_sha256, "Script 1 receipt_sha256")
        digest(self.script_sha256, "Script 1 script_sha256")
        digest(self.eoat_identity_sha256, "Script 1 EOAT identity")
        object.__setattr__(self, "final_pose", _vector(self.final_pose, 6, "Script 1 final pose"))
        object.__setattr__(self, "final_q", _vector(self.final_q, 6, "Script 1 final q"))
        if not isinstance(self.stationary, bool) or self.safety_mode != "NORMAL":
            raise R004ContractError("Script 1 receipt is not stationary Safety NORMAL")
        if math.dist(self.final_pose, SCRIPT1_TARGET_POSE) > 0.003:
            raise R004ContractError("Script 1 final pose differs from fixed campaign pose")

    def validate_for_epoch(self, contract: R004Contract, now_s: float, *, expected_script_sha256: str, expected_eoat_sha256: str) -> None:
        _age(now_s, self.observed_at_s, "Script 1", SCRIPT1_RECEIPT_MAX_AGE_S)
        if self.script_sha256 != expected_script_sha256:
            raise R004ContractError("Script 1 receipt SHA differs")
        if self.eoat_identity_sha256 != expected_eoat_sha256:
            raise R004ContractError("Script 1 V4 EOAT identity differs")
        if not self.stationary or self.safety_mode != "NORMAL":
            raise R004ContractError("Script 1 receipt stationary/Safety contract differs")
        if self.receipt_sha256 not in {
            expected_script_sha256,
            contract.sha256,
        } and len(self.receipt_sha256) != 64:
            raise R004ContractError("Script 1 receipt identity is malformed")


@dataclass(frozen=True)
class ControllerReadbackReceipt:
    receipt_sha256: str
    program: str
    controller_target: str
    script_sha256: str
    txt_sha256: str
    urp_sha256: str
    observed_at_s: float
    runtime_protocol: int
    runtime_digest_hi: int
    runtime_digest_lo: int
    eoat_identity_sha256: str
    payload_kg: float | None = None
    payload_cog_m: tuple[float, float, float] | None = None
    tcp_offset_m_rad: tuple[float, float, float, float, float, float] | None = None
    safety_mode: str = "NORMAL"
    stationary: bool = True
    route_id: str = ""

    def __post_init__(self) -> None:
        for role, value in (
            ("controller receipt", self.receipt_sha256),
            ("controller script", self.script_sha256),
            ("controller txt", self.txt_sha256),
            ("controller urp", self.urp_sha256),
            ("controller EOAT", self.eoat_identity_sha256),
        ):
            digest(value, role)
        if not self.program or not self.controller_target.endswith(".urp"):
            raise R004ContractError("controller readback identity is incomplete")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (self.runtime_protocol, self.runtime_digest_hi, self.runtime_digest_lo)):
            raise R004ContractError("controller runtime identity must be integer typed")
        if self.payload_kg is not None and (
            not math.isfinite(float(self.payload_kg)) or float(self.payload_kg) <= 0.0
        ):
            raise R004ContractError("controller payload readback is invalid")
        if self.payload_cog_m is not None:
            object.__setattr__(self, "payload_cog_m", _vector(self.payload_cog_m, 3, "controller CoG"))
        if self.tcp_offset_m_rad is not None:
            object.__setattr__(self, "tcp_offset_m_rad", _vector(self.tcp_offset_m_rad, 6, "controller TCP"))
        if self.safety_mode != "NORMAL" or not isinstance(self.stationary, bool):
            raise R004ContractError("controller readback is not stationary Safety NORMAL")
        if not isinstance(self.route_id, str):
            raise R004ContractError("controller route id is not typed")

    def validate_at_play(self, contract: R004Contract, now_s: float, *, expected_triplet: Mapping[str, str]) -> None:
        _age(now_s, self.observed_at_s, "controller readback", CONTROLLER_READBACK_MAX_AGE_S)
        if self.program != contract.raw["program"] or self.controller_target != contract.raw["script2"]["controller_target"]:
            raise R004ContractError("controller readback program target differs")
        if dict(expected_triplet) != {
            "script": self.script_sha256,
            "txt": self.txt_sha256,
            "urp": self.urp_sha256,
        }:
            raise R004ContractError("controller readback triplet differs")
        if self.eoat_identity_sha256 != contract.eoat_sha256:
            raise R004ContractError("controller readback EOAT identity differs")

    def validate_eoat_readback(
        self,
        *,
        payload_kg: float,
        payload_cog_m: Sequence[float],
        tcp_offset_m_rad: Sequence[float],
        payload_tolerance_kg: float = 0.0005,
        cog_tolerance_m: float = 0.00005,
        tcp_tolerance_m_rad: float = 0.00005,
    ) -> None:
        if self.payload_kg is None or self.payload_cog_m is None or self.tcp_offset_m_rad is None:
            raise R004ContractError("controller receipt omits exact V4 EOAT readback")
        if not math.isclose(self.payload_kg, payload_kg, rel_tol=0.0, abs_tol=payload_tolerance_kg):
            raise R004ContractError("controller payload differs from V4 profile")
        if any(
            not math.isclose(actual, expected, rel_tol=0.0, abs_tol=cog_tolerance_m)
            for actual, expected in zip(self.payload_cog_m, payload_cog_m, strict=True)
        ):
            raise R004ContractError("controller CoG differs from V4 profile")
        if any(
            not math.isclose(actual, expected, rel_tol=0.0, abs_tol=tcp_tolerance_m_rad)
            for actual, expected in zip(self.tcp_offset_m_rad, tcp_offset_m_rad, strict=True)
        ):
            raise R004ContractError("controller TCP differs from V4 profile")
        if not self.stationary or self.safety_mode != "NORMAL":
            raise R004ContractError("controller EOAT readback is not stationary Safety NORMAL")


@dataclass(frozen=True)
class RuntimeIdentityEvidence:
    program: str
    script_sha256: str
    runtime_protocol: int
    runtime_digest_hi: int
    runtime_digest_lo: int
    session_epoch: int
    resident_session_id: str
    program_running: bool
    uninterrupted: bool
    observed_at_s: float

    def __post_init__(self) -> None:
        digest(self.script_sha256, "runtime script")
        if not self.program or not self.resident_session_id:
            raise R004ContractError("runtime identity is incomplete")
        if not math.isfinite(float(self.observed_at_s)):
            raise R004ContractError("runtime identity timestamp is nonfinite")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (self.runtime_protocol, self.runtime_digest_hi, self.runtime_digest_lo, self.session_epoch)):
            raise R004ContractError("runtime identity integer fields are invalid")
        if not isinstance(self.program_running, bool) or not isinstance(self.uninterrupted, bool):
            raise R004ContractError("runtime identity flags are not bool")


class SessionIdentityGate:
    """Receipt is accepted once at Play; runtime identity extends only that session."""

    def __init__(self, contract: R004Contract) -> None:
        self.contract = contract
        self._active = False
        self._session_id: str | None = None
        self._epoch: int | None = None
        self._expected: ControllerReadbackReceipt | None = None
        self._invalidated_reason = ""
        self._last_runtime_observed_at_s: float | None = None
        self._used_controller_receipts: set[str] = set()

    @property
    def active(self) -> bool:
        return self._active

    @property
    def invalidated_reason(self) -> str:
        return self._invalidated_reason

    def begin_play(
        self,
        *,
        controller_receipt: ControllerReadbackReceipt,
        script1_receipt: Script1StartReceipt,
        now_s: float,
        epoch: int,
        session_id: str,
        expected_triplet: Mapping[str, str],
    ) -> None:
        if self._active:
            raise R004ContractError("resident session already active")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0 or not session_id:
            raise R004ContractError("new session epoch/id is invalid")
        if controller_receipt.receipt_sha256 in self._used_controller_receipts:
            raise R004ContractError("static controller readback receipt reused across sessions")
        controller_receipt.validate_at_play(self.contract, now_s, expected_triplet=expected_triplet)
        script1_receipt.validate_for_epoch(
            self.contract,
            now_s,
            expected_script_sha256=self.contract.script1_sha256["script"],
            expected_eoat_sha256=self.contract.eoat_sha256,
        )
        self._active = True
        self._epoch = epoch
        self._session_id = session_id
        self._expected = controller_receipt
        self._invalidated_reason = ""
        self._last_runtime_observed_at_s = float(now_s)
        self._used_controller_receipts.add(controller_receipt.receipt_sha256)

    def observe_runtime(self, evidence: RuntimeIdentityEvidence) -> bool:
        if not self._active or self._expected is None:
            return False
        if self._last_runtime_observed_at_s is None or evidence.observed_at_s <= self._last_runtime_observed_at_s:
            self.invalidate("runtime_identity_timestamp_not_newer")
            return False
        accepted = (
            evidence.program == self._expected.program
            and evidence.script_sha256 == self._expected.script_sha256
            and evidence.runtime_protocol == self._expected.runtime_protocol
            and evidence.runtime_digest_hi == self._expected.runtime_digest_hi
            and evidence.runtime_digest_lo == self._expected.runtime_digest_lo
            and evidence.session_epoch == self._epoch
            and evidence.resident_session_id == self._session_id
            and evidence.program_running
            and evidence.uninterrupted
        )
        if not accepted:
            self.invalidate("runtime_identity_or_session_changed")
        else:
            self._last_runtime_observed_at_s = evidence.observed_at_s
        return accepted

    def require_attempt_identity(self, *, epoch: int, session_id: str) -> None:
        if not self._active or epoch != self._epoch or session_id != self._session_id:
            raise R004ContractError("attempt identity is outside the uninterrupted session")

    def invalidate(self, reason: str) -> None:
        self._active = False
        self._invalidated_reason = reason
        self._session_id = None
        self._epoch = None
        self._expected = None
        self._last_runtime_observed_at_s = None


class Script1UseLedger:
    """Prevents a static Script 1 receipt from being reused across sessions."""

    def __init__(self) -> None:
        self._used: set[tuple[str, int]] = set()

    def consume(self, receipt: Script1StartReceipt, epoch: int) -> None:
        key = (receipt.receipt_sha256, epoch)
        if key in self._used:
            raise R004ContractError("Script 1 receipt reused in the same epoch")
        if any(receipt.receipt_sha256 == prior_sha for prior_sha, _ in self._used):
            raise R004ContractError("static Script 1 receipt reused across sessions")
        self._used.add(key)


__all__ = [
    "ControllerReadbackReceipt",
    "RuntimeIdentityEvidence",
    "Script1StartReceipt",
    "Script1UseLedger",
    "SessionIdentityGate",
]
