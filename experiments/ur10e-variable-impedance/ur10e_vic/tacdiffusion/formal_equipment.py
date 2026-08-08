"""Formal V4 EOAT and Kunwei frame identity with fresh read-back gates."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import KunweiOnlyForceAuthorityV1, validate_formal_force_source_payload


FORMAL_EQUIPMENT_SCHEMA_V1 = "ur10e_tacdiffusion_formal_equipment/v1"
FORMAL_EOAT_PROFILE_ID = "new-3d-printed-eoat-v4"
FORMAL_PAYLOAD_KG = 0.413
FORMAL_PAYLOAD_COG_M = (0.0011, 0.0031, 0.0163)
FORMAL_TCP_OFFSET_M_RAD = (0.0, 0.0, 0.0874, 0.0, 0.0, 0.0)
FORMAL_KUNWEI_ORIGIN_TOOL0_M = (0.0, 0.0, 0.0315)
FORMAL_SENSOR_TO_TCP_M = (0.0, 0.0, 0.0559)
FORMAL_EQUIPMENT_READBACK_SCHEMA_V1 = "ur10e_tacdiffusion_formal_equipment_readback/v1"


def _vector(value: object, length: int, name: str) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must contain {length} finite values")
    result = tuple(float(item) for item in value)
    if len(result) != length or not all(math.isfinite(item) for item in result):
        raise ValueError(f"{name} must contain {length} finite values")
    return result


def _close_vector(observed: Sequence[float], expected: Sequence[float], tolerance: float) -> bool:
    return len(observed) == len(expected) and all(
        math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=tolerance)
        for left, right in zip(observed, expected)
    )


def _expected_wrench_transform(z_m: float) -> tuple[tuple[float, ...], ...]:
    return (
        (1.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0, 0.0, 0.0),
        (0.0, z_m, 0.0, 1.0, 0.0, 0.0),
        (-z_m, 0.0, 0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
    )


@dataclass(frozen=True)
class FormalEquipmentContractV1:
    payload: Mapping[str, Any]
    authority: KunweiOnlyForceAuthorityV1
    wrench_transform_sensor_to_tcp_6x6: tuple[tuple[float, ...], ...]

    @property
    def fingerprint_sha256(self) -> str:
        encoded = json.dumps(
            self.payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def validate_readback(self, receipt: Mapping[str, Any]) -> None:
        validate_formal_equipment_readback(receipt, contract=self)


def load_formal_equipment_contract(path: str | Path) -> FormalEquipmentContractV1:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("schema_version") != FORMAL_EQUIPMENT_SCHEMA_V1:
        raise ValueError("unsupported formal equipment contract")
    validate_formal_force_source_payload(raw, path="formal_equipment")
    eoat = raw.get("eoat")
    kunwei = raw.get("kunwei")
    sources = raw.get("source_hashes")
    gates = raw.get("runtime_gates")
    if not all(isinstance(value, Mapping) for value in (eoat, kunwei, sources, gates)):
        raise ValueError("formal equipment sections are incomplete")
    assert isinstance(eoat, Mapping) and isinstance(kunwei, Mapping)
    if eoat.get("profile_id") != FORMAL_EOAT_PROFILE_ID:
        raise ValueError("formal EOAT profile identity mismatch")
    if not math.isclose(float(eoat.get("payload_kg", math.nan)), FORMAL_PAYLOAD_KG, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("formal payload mismatch")
    cog = _vector(eoat.get("payload_cog_m"), 3, "payload_cog_m")
    tcp = _vector(eoat.get("tcp_offset_m_rad"), 6, "tcp_offset_m_rad")
    if cog != FORMAL_PAYLOAD_COG_M or tcp != FORMAL_TCP_OFFSET_M_RAD:
        raise ValueError("formal EOAT CoG/TCP mismatch")
    authority = KunweiOnlyForceAuthorityV1(
        source_identity=str(kunwei.get("source_identity", "")),
        sensor_model=str(kunwei.get("sensor_model", "")),
        transport=str(kunwei.get("transport", "")),
        zero_behavior=str(kunwei.get("zero_behavior", "")),
        tare_behavior=str(kunwei.get("tare_behavior", "")),
        sensor_config_behavior=str(kunwei.get("sensor_config_behavior", "")),
    )
    origin = _vector(kunwei.get("sensor_origin_tool0_m"), 3, "sensor_origin_tool0_m")
    sensor_to_tcp = _vector(
        kunwei.get("sensor_origin_to_tcp_sensor_m"), 3, "sensor_origin_to_tcp_sensor_m"
    )
    if origin != FORMAL_KUNWEI_ORIGIN_TOOL0_M or sensor_to_tcp != FORMAL_SENSOR_TO_TCP_M:
        raise ValueError("formal Kunwei frame geometry mismatch")
    if not math.isclose(tcp[2] - origin[2], sensor_to_tcp[2], rel_tol=0.0, abs_tol=1e-12):
        raise ValueError("formal sensor-to-TCP Z derivation mismatch")
    raw_transform = kunwei.get("wrench_transform_sensor_to_tcp_6x6")
    if not isinstance(raw_transform, Sequence) or isinstance(raw_transform, (str, bytes)):
        raise ValueError("formal wrench transform is missing")
    transform = tuple(_vector(row, 6, "wrench transform row") for row in raw_transform)
    if transform != _expected_wrench_transform(sensor_to_tcp[2]):
        raise ValueError("formal wrench transform does not match derived geometry")
    if kunwei.get("normal_force_axis") != "fz" or float(kunwei.get("normal_force_sign", 0.0)) != -1.0:
        raise ValueError("formal positive normal-load binding mismatch")
    assert isinstance(sources, Mapping) and isinstance(gates, Mapping)
    if len(sources) < 5 or any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
        for value in sources.values()
    ):
        raise ValueError("formal equipment source hashes are incomplete")
    required_gates = {
        "fresh_payload_cog_tcp_readback_before_motion",
        "remote_control_required",
        "safety_normal_required",
        "stationary_required",
        "single_writer_required",
        "kunwei_raw_stream_required",
        "ur_force_fields_forbidden",
    }
    if any(gates.get(name) is not True for name in required_gates):
        raise ValueError("formal equipment runtime gates are not fail-closed")
    canonical = json.loads(json.dumps(raw, sort_keys=True, allow_nan=False))
    return FormalEquipmentContractV1(canonical, authority, transform)


def validate_formal_equipment_readback(
    receipt: Mapping[str, Any], *, contract: FormalEquipmentContractV1
) -> None:
    if not isinstance(receipt, Mapping) or receipt.get("schema_version") != FORMAL_EQUIPMENT_READBACK_SCHEMA_V1:
        raise ValueError("formal equipment readback schema mismatch")
    validate_formal_force_source_payload(receipt, path="formal_equipment_readback")
    if receipt.get("equipment_contract_sha256") != contract.fingerprint_sha256:
        raise ValueError("formal equipment readback contract binding mismatch")
    dashboard = receipt.get("dashboard")
    readback = receipt.get("readback")
    checks = receipt.get("checks")
    if not all(isinstance(value, Mapping) for value in (dashboard, readback, checks)):
        raise ValueError("formal equipment readback sections are incomplete")
    assert isinstance(dashboard, Mapping) and isinstance(readback, Mapping) and isinstance(checks, Mapping)
    if dashboard.get("remote_control") is not True or dashboard.get("safety_mode") != "NORMAL":
        raise ValueError("formal equipment readback Remote/Safety gate failed")
    if dashboard.get("robot_mode") != "RUNNING" or dashboard.get("program_running") is not False:
        raise ValueError("formal equipment readback robot/program gate failed")
    payload = float(readback.get("payload_kg", math.nan))
    cog = _vector(readback.get("payload_cog_m"), 3, "readback payload_cog_m")
    tcp = _vector(readback.get("tcp_offset_m_rad"), 6, "readback tcp_offset_m_rad")
    speed = _vector(readback.get("actual_tcp_speed_m_s_rad_s"), 6, "actual_tcp_speed")
    if not math.isclose(payload, FORMAL_PAYLOAD_KG, rel_tol=0.0, abs_tol=5e-4):
        raise ValueError("formal equipment payload readback mismatch")
    if not _close_vector(cog, FORMAL_PAYLOAD_COG_M, 5e-5) or not _close_vector(tcp, FORMAL_TCP_OFFSET_M_RAD, 5e-5):
        raise ValueError("formal equipment CoG/TCP readback mismatch")
    if max(abs(value) for value in speed[:3]) > 5e-4 or max(abs(value) for value in speed[3:]) > 5e-3:
        raise ValueError("formal equipment readback is not stationary")
    required_checks = {
        "single_writer",
        "payload_match",
        "cog_match",
        "tcp_match",
        "stationary",
        "kunwei_raw_stream",
        "no_ur_force_fields_read",
    }
    if any(checks.get(name) is not True for name in required_checks):
        raise ValueError("formal equipment readback checks are incomplete")


__all__ = [
    "FORMAL_EQUIPMENT_READBACK_SCHEMA_V1",
    "FORMAL_EQUIPMENT_SCHEMA_V1",
    "FormalEquipmentContractV1",
    "load_formal_equipment_contract",
    "validate_formal_equipment_readback",
]
