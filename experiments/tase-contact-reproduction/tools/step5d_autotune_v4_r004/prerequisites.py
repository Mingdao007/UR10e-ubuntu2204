"""Explicit receipt/session prerequisites for the r004 live boundary."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from step5d_eoat_profiles import load_new_eoat_profile

from .contracts import PROGRAM, R004Contract, R004ContractError, runtime_identity_limbs
from .identity import ControllerReadbackReceipt, RuntimeIdentityEvidence, Script1StartReceipt
from .transport import expected_runtime_identity


RECEIPT_SCHEMA = "step5d.autotune-v4/r004-live-prerequisites-v1"


class PrerequisiteError(RuntimeError):
    """A live boundary prerequisite is absent, stale, or mismatched."""


def _regular_json(path: Path, role: str) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise PrerequisiteError(f"{role} must be a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PrerequisiteError(f"{role} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise PrerequisiteError(f"{role} must be a JSON object")
    return value


def _digest(value: Any, role: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise PrerequisiteError(f"{role} must be a lowercase SHA-256")
    return value


def _vector(value: Any, size: int, role: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise PrerequisiteError(f"{role} must contain {size} values")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise PrerequisiteError(f"{role} contains a nonfinite value")
    return result


def load_controller_receipt(path: Path) -> ControllerReadbackReceipt:
    value = _regular_json(path, "controller readback receipt")
    readback = value.get("readback", value)
    controller = value.get("controller", value)
    if not isinstance(readback, dict) or not isinstance(controller, dict):
        raise PrerequisiteError("controller receipt readback/controller sections are invalid")
    try:
        speed = _vector(readback.get("actual_tcp_speed_m_s_rad_s", readback.get("actual_TCP_speed")), 6, "controller TCP speed")
        stationary = bool(value.get("stationary", controller.get("stationary", False)))
        if not stationary:
            stationary = (
                math.sqrt(sum(item * item for item in speed[:3])) <= 0.0005
                and math.sqrt(sum(item * item for item in speed[3:])) <= 0.005
            )
        return ControllerReadbackReceipt(
            receipt_sha256=_digest(value.get("receipt_sha256", value.get("receipt_id")), "controller receipt"),
            program=str(value.get("program", value.get("program_basename", ""))),
            controller_target=str(value.get("controller_target", "")),
            script_sha256=_digest(value.get("script_sha256"), "controller script"),
            txt_sha256=_digest(value.get("txt_sha256"), "controller txt"),
            urp_sha256=_digest(value.get("urp_sha256"), "controller urp"),
            observed_at_s=float(value.get("observed_at_s")),
            runtime_protocol=int(value.get("runtime_protocol")),
            runtime_digest_hi=int(value.get("runtime_digest_hi")),
            runtime_digest_lo=int(value.get("runtime_digest_lo")),
            eoat_identity_sha256=_digest(value.get("eoat_identity_sha256"), "controller EOAT"),
            payload_kg=float(readback.get("payload_kg")),
            payload_cog_m=_vector(readback.get("payload_cog_m"), 3, "controller CoG"),
            tcp_offset_m_rad=_vector(readback.get("tcp_offset_m_rad"), 6, "controller TCP"),
            safety_mode=str(value.get("safety_mode", controller.get("safety_mode", ""))),
            stationary=stationary,
            route_id=str(value.get("route_id", "")),
        )
    except (KeyError, TypeError, ValueError, OverflowError, R004ContractError) as exc:
        raise PrerequisiteError("controller readback receipt fields are invalid") from exc


def load_script1_receipt(path: Path) -> Script1StartReceipt:
    value = _regular_json(path, "Script1 receipt")
    try:
        return Script1StartReceipt(
            receipt_sha256=_digest(value.get("receipt_sha256", value.get("receipt_id")), "Script1 receipt"),
            script_sha256=_digest(value.get("script_sha256"), "Script1 script"),
            observed_at_s=float(value.get("observed_at_s")),
            final_pose=_vector(value.get("final_pose"), 6, "Script1 final pose"),
            final_q=_vector(value.get("final_q"), 6, "Script1 final q"),
            stationary=value.get("stationary") is True,
            safety_mode=str(value.get("safety_mode", "")),
            eoat_identity_sha256=_digest(value.get("eoat_identity_sha256"), "Script1 EOAT"),
        )
    except (KeyError, TypeError, ValueError, OverflowError, R004ContractError) as exc:
        raise PrerequisiteError("Script1 receipt fields are invalid") from exc


def load_runtime_evidence(path: Path) -> RuntimeIdentityEvidence:
    value = _regular_json(path, "runtime identity evidence")
    try:
        return RuntimeIdentityEvidence(
            program=str(value.get("program", "")),
            script_sha256=_digest(value.get("script_sha256"), "runtime script"),
            runtime_protocol=int(value.get("runtime_protocol")),
            runtime_digest_hi=int(value.get("runtime_digest_hi")),
            runtime_digest_lo=int(value.get("runtime_digest_lo")),
            session_epoch=int(value.get("session_epoch")),
            resident_session_id=str(value.get("resident_session_id", "")),
            program_running=value.get("program_running") is True,
            uninterrupted=value.get("uninterrupted") is True,
            observed_at_s=float(value.get("observed_at_s")),
        )
    except (KeyError, TypeError, ValueError, OverflowError, R004ContractError) as exc:
        raise PrerequisiteError("runtime identity evidence fields are invalid") from exc


@dataclass(frozen=True)
class LivePrerequisites:
    contract: R004Contract
    controller: ControllerReadbackReceipt
    script1: Script1StartReceipt
    runtime: RuntimeIdentityEvidence
    expected_triplet: Mapping[str, str]
    route_id: str
    session_epoch: int
    resident_session_id: str
    input_baseline_ledger_sha256: str

    def validate(self, *, now_s: float) -> None:
        if not self.route_id or self.controller.route_id != self.route_id:
            raise PrerequisiteError("single-writer route identity differs")
        _digest(self.input_baseline_ledger_sha256, "input baseline ledger")
        if self.session_epoch <= 0 or self.runtime.session_epoch != self.session_epoch:
            raise PrerequisiteError("r004 session epoch differs")
        if self.runtime.resident_session_id != self.resident_session_id:
            raise PrerequisiteError("r004 resident session identity differs")
        self.controller.validate_at_play(self.contract, now_s, expected_triplet=self.expected_triplet)
        self.controller.validate_eoat_readback(
            payload_kg=load_new_eoat_profile().payload_kg,
            payload_cog_m=load_new_eoat_profile().cog_m,
            tcp_offset_m_rad=load_new_eoat_profile().controller_tcp_m_rad,
        )
        self.script1.validate_for_epoch(
            self.contract,
            now_s,
            expected_script_sha256=self.contract.script1_sha256["script"],
            expected_eoat_sha256=self.contract.eoat_sha256,
        )
        expected_protocol, expected_hi, expected_lo = expected_runtime_identity(self.contract)
        if (
            self.controller.runtime_protocol,
            self.controller.runtime_digest_hi,
            self.controller.runtime_digest_lo,
        ) != (expected_protocol, expected_hi, expected_lo):
            raise PrerequisiteError("controller runtime identity digest differs")
        if (
            self.runtime.program != PROGRAM
            or self.runtime.script_sha256 != self.controller.script_sha256
            or self.runtime.runtime_protocol != expected_protocol
            or self.runtime.runtime_digest_hi != expected_hi
            or self.runtime.runtime_digest_lo != expected_lo
            or not self.runtime.program_running
            or not self.runtime.uninterrupted
        ):
            raise PrerequisiteError("runtime identity is not the same uninterrupted r004 resident session")
        if self.controller.safety_mode != "NORMAL" or not self.controller.stationary or self.runtime.observed_at_s <= self.controller.observed_at_s:
            raise PrerequisiteError("live entry requires fresh stationary Safety NORMAL evidence")


def load_prerequisites(
    contract: R004Contract,
    *,
    controller_path: Path,
    script1_path: Path,
    runtime_path: Path,
    expected_triplet: Mapping[str, str],
    route_id: str,
    session_epoch: int,
    resident_session_id: str,
    input_baseline_ledger_sha256: str,
) -> LivePrerequisites:
    bundle = LivePrerequisites(
        contract=contract,
        controller=load_controller_receipt(controller_path),
        script1=load_script1_receipt(script1_path),
        runtime=load_runtime_evidence(runtime_path),
        expected_triplet=dict(expected_triplet),
        route_id=route_id,
        session_epoch=session_epoch,
        resident_session_id=resident_session_id,
        input_baseline_ledger_sha256=_digest(input_baseline_ledger_sha256, "input baseline ledger"),
    )
    return bundle


__all__ = [
    "LivePrerequisites",
    "PrerequisiteError",
    "RECEIPT_SCHEMA",
    "load_controller_receipt",
    "load_prerequisites",
    "load_runtime_evidence",
    "load_script1_receipt",
]
