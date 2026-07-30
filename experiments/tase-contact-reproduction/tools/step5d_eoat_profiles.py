"""Hash-bound EOAT profiles and one shared apply-and-verify primitive."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "step5d.eoat/profile-v1"
OLD_PROFILE = ROOT / "config/step5d/eoat_profile_old_v3.json"
NEW_PROFILE = ROOT / "config/step5d/eoat_profile_new_v4.json"


class EoatProfileError(RuntimeError):
    pass


@dataclass(frozen=True)
class EOATProfile:
    profile_id: str
    revision: int
    profile_sha256: str
    payload_kg: float
    cog_m: tuple[float, float, float]
    controller_tcp_m_rad: tuple[float, float, float, float, float, float]
    kinematic_tcp_xyz_m: tuple[float, float, float]
    program_z_delta_m: float
    evidence_sha256: tuple[str, ...]


@dataclass(frozen=True)
class ControllerEOATReadback:
    payload_kg: float
    cog_m: tuple[float, float, float]
    tcp_m_rad: tuple[float, float, float, float, float, float]
    actual_tcp_speed_m_s_rad_s: tuple[float, float, float, float, float, float]
    safety_mode: str
    single_writer: bool


@dataclass(frozen=True)
class EOATApplyReceipt:
    profile_id: str
    profile_sha256: str
    payload_set: bool
    tcp_set: bool
    fresh_get_verified: bool
    stationary_verified: bool
    safety_normal_verified: bool
    single_writer_verified: bool
    readback_sha256: str
    receipt_sha256: str
    schema: str = "step5d.eoat/apply-verify-receipt-v1"

    def __post_init__(self) -> None:
        if self.schema != "step5d.eoat/apply-verify-receipt-v1":
            raise EoatProfileError("EOAT receipt schema differs")
        if not isinstance(self.profile_id, str) or not self.profile_id:
            raise EoatProfileError("EOAT receipt profile id is invalid")
        _digest(self.profile_sha256, "EOAT receipt profile")
        _digest(self.readback_sha256, "EOAT receipt readback")
        _digest(self.receipt_sha256, "EOAT receipt")
        if not all(
            isinstance(value, bool)
            for value in (
                self.payload_set,
                self.tcp_set,
                self.fresh_get_verified,
                self.stationary_verified,
                self.safety_normal_verified,
                self.single_writer_verified,
            )
        ):
            raise EoatProfileError("EOAT receipt verification flags are untyped")

    @property
    def passed(self) -> bool:
        return all(
            (
                self.payload_set,
                self.tcp_set,
                self.fresh_get_verified,
                self.stationary_verified,
                self.safety_normal_verified,
                self.single_writer_verified,
            )
        )


class EOATController(Protocol):
    def set_target_payload(
        self, payload_kg: float, cog_m: Sequence[float]
    ) -> None: ...

    def set_tcp(self, tcp_m_rad: Sequence[float]) -> None: ...

    def fresh_get_eoat(self) -> ControllerEOATReadback: ...


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise EoatProfileError(f"{role} must be a lowercase SHA-256")
    return value


def _finite_vector(value: Any, size: int, role: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != size:
        raise EoatProfileError(f"{role} must have {size} values")
    parsed = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in parsed):
        raise EoatProfileError(f"{role} must be finite")
    return parsed


def _resolve(path_text: str) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else ROOT / path


def load_eoat_profile(path: Path) -> EOATProfile:
    if path.is_symlink() or not path.is_file():
        raise EoatProfileError(f"EOAT profile must be a regular file: {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != SCHEMA or document.get("revision") != 1:
        raise EoatProfileError("EOAT profile identity differs")
    payload = float(document.get("payload_kg"))
    cog = _finite_vector(document.get("cog_m"), 3, "CoG")
    tcp = _finite_vector(document.get("controller_tcp_m_rad"), 6, "controller TCP")
    kinematic = _finite_vector(
        document.get("kinematic_tcp_xyz_m"), 3, "kinematic TCP"
    )
    delta = float(document.get("program_z_delta_m"))
    if not all(math.isfinite(value) for value in (payload, delta)) or payload <= 0:
        raise EoatProfileError("EOAT payload or program Z delta is invalid")
    if not math.isclose(delta, 0.0, abs_tol=1e-12):
        raise EoatProfileError("EOAT program Z delta must be zero")
    invariants = document.get("invariants")
    if not isinstance(invariants, dict) or not all(
        invariants.get(field) is True
        for field in (
            "fresh_apply_and_verify_before_motion",
            "stationary_required",
            "safety_normal_required",
            "single_writer_required",
        )
    ):
        raise EoatProfileError("EOAT invariant set differs")
    digests: list[str] = []
    for binding in document.get("evidence", ()):
        if not isinstance(binding, dict):
            raise EoatProfileError("EOAT evidence binding is invalid")
        expected = binding.get("sha256")
        if not isinstance(expected, str) or len(expected) != 64:
            raise EoatProfileError("EOAT evidence digest is invalid")
        source = _resolve(binding.get("path", ""))
        if source.is_symlink() or not source.is_file() or _sha256(source) != expected:
            raise EoatProfileError(f"EOAT evidence binding differs: {source}")
        digests.append(expected)
    if not digests:
        raise EoatProfileError("EOAT profile has no evidence")
    return EOATProfile(
        profile_id=document["profile_id"],
        revision=document["revision"],
        profile_sha256=_sha256(path),
        payload_kg=payload,
        cog_m=(cog[0], cog[1], cog[2]),
        controller_tcp_m_rad=(tcp[0], tcp[1], tcp[2], tcp[3], tcp[4], tcp[5]),
        kinematic_tcp_xyz_m=(kinematic[0], kinematic[1], kinematic[2]),
        program_z_delta_m=delta,
        evidence_sha256=tuple(digests),
    )


def load_old_eoat_profile() -> EOATProfile:
    return load_eoat_profile(OLD_PROFILE)


def load_new_eoat_profile() -> EOATProfile:
    return load_eoat_profile(NEW_PROFILE)


def _close_vector(
    actual: Sequence[float], expected: Sequence[float], tolerance: float
) -> bool:
    try:
        return len(actual) == len(expected) and all(
            math.isfinite(float(a))
            and math.isclose(float(a), float(e), rel_tol=0.0, abs_tol=tolerance)
            for a, e in zip(actual, expected, strict=True)
        )
    except (TypeError, ValueError):
        return False


def _readback_payload(readback: ControllerEOATReadback) -> bytes:
    payload = {
        "payload_kg": float(readback.payload_kg),
        "cog_m": [float(value) for value in readback.cog_m],
        "tcp_m_rad": [float(value) for value in readback.tcp_m_rad],
        "actual_tcp_speed_m_s_rad_s": [
            float(value) for value in readback.actual_tcp_speed_m_s_rad_s
        ],
        "safety_mode": readback.safety_mode,
        "single_writer": readback.single_writer,
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def apply_and_verify_eoat(
    profile: EOATProfile, controller: EOATController
) -> EOATApplyReceipt:
    """Apply profile data, then require a real fresh controller readback."""
    _digest(profile.profile_sha256, "EOAT profile")
    try:
        controller.set_target_payload(profile.payload_kg, profile.cog_m)
        controller.set_tcp(profile.controller_tcp_m_rad)
        readback = controller.fresh_get_eoat()
        readback_sha = hashlib.sha256(_readback_payload(readback)).hexdigest()
        speed = tuple(float(value) for value in readback.actual_tcp_speed_m_s_rad_s)
        stationary = (
            len(speed) == 6
            and all(math.isfinite(value) for value in speed)
            and math.sqrt(sum(value * value for value in speed[:3])) <= 0.0005
            and math.sqrt(sum(value * value for value in speed[3:])) <= 0.005
        )
        payload_ok = math.isclose(
            float(readback.payload_kg), profile.payload_kg, rel_tol=0.0, abs_tol=0.0005
        )
        cog_ok = _close_vector(readback.cog_m, profile.cog_m, 0.00005)
        tcp_ok = _close_vector(readback.tcp_m_rad, profile.controller_tcp_m_rad, 0.00005)
    except (AttributeError, TypeError, ValueError, OSError, OverflowError) as exc:
        raise EoatProfileError(f"EOAT apply/readback failed: {exc}") from exc
    receipt = EOATApplyReceipt(
        profile_id=profile.profile_id,
        profile_sha256=profile.profile_sha256,
        payload_set=True,
        tcp_set=True,
        fresh_get_verified=payload_ok and cog_ok and tcp_ok,
        stationary_verified=stationary,
        safety_normal_verified=readback.safety_mode == "NORMAL",
        single_writer_verified=readback.single_writer is True,
        readback_sha256=readback_sha,
        receipt_sha256="0" * 64,
    )
    receipt_material = {
        "profile_id": receipt.profile_id,
        "profile_sha256": receipt.profile_sha256,
        "readback_sha256": receipt.readback_sha256,
        "payload_set": receipt.payload_set,
        "tcp_set": receipt.tcp_set,
        "fresh_get_verified": receipt.fresh_get_verified,
        "stationary_verified": receipt.stationary_verified,
        "safety_normal_verified": receipt.safety_normal_verified,
        "single_writer_verified": receipt.single_writer_verified,
    }
    receipt = replace(
        receipt,
        receipt_sha256=hashlib.sha256(
            json.dumps(
                receipt_material,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest(),
    )
    if not receipt.passed:
        raise EoatProfileError(f"EOAT apply/readback verification failed: {receipt}")
    return receipt


def render_urscript_apply(profile: EOATProfile) -> str:
    cog = ", ".join(f"{value:.9f}" for value in profile.cog_m)
    tcp = ", ".join(f"{value:.9f}" for value in profile.controller_tcp_m_rad)
    return (
        f"  # EOAT_PROFILE_ID: {profile.profile_id}\n"
        f"  # EOAT_PROFILE_SHA256: {profile.profile_sha256}\n"
        f"  set_target_payload({profile.payload_kg:.3f}, [{cog}], [0, 0, 0, 0, 0, 0])\n"
        f"  set_tcp(p[{tcp}])"
    )


__all__ = [
    "ControllerEOATReadback",
    "EOATApplyReceipt",
    "EOATController",
    "EOATProfile",
    "EoatProfileError",
    "NEW_PROFILE",
    "OLD_PROFILE",
    "apply_and_verify_eoat",
    "load_eoat_profile",
    "load_new_eoat_profile",
    "load_old_eoat_profile",
    "render_urscript_apply",
]
