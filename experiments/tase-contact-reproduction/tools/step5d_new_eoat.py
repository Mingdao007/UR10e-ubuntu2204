"""Strict EOAT calibration-v2 loader and URScript initialization renderer."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT = EXPERIMENT_ROOT / "config/step5d/new_eoat_calibration.json"
SCHEMA = "step5d.new-eoat/calibration-contract-v2"
EXPECTED_ARTIFACT_ID = "new-3d-printed-eoat-20260730-v2"
RECEIPT_SCHEMA = "step5d.new-eoat/controller-get-receipt-v2"
EXPECTED_PAYLOAD_KG = 0.413
EXPECTED_COG_M = (0.0011, 0.0031, 0.0163)
EXPECTED_TCP = (0.0, 0.0, 0.0874, 0.0, 0.0, 0.0)


class NewEoatError(RuntimeError):
    """The bounded EOAT calibration contract is invalid."""


@dataclass(frozen=True)
class NewEoat:
    artifact_id: str
    artifact_sha256: str
    source_sha256: str
    controller_receipt_sha256: str
    payload_kg: float
    cog_m: tuple[float, float, float]
    tcp_pose_m_rad: tuple[float, float, float, float, float, float]
    program_z_delta_m: float


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NewEoatError(f"EOAT evidence repeats key {key!r}")
        result[key] = value
    return result


def _read_json(path: Path, role: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise NewEoatError(f"{role} must be a regular file: {path}")
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                NewEoatError(f"{role} contains non-finite JSON constant: {token}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NewEoatError(f"{role} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise NewEoatError(f"{role} must be a JSON object")
    return value


def _exact_object(value: Any, fields: set[str], role: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise NewEoatError(f"{role} fields differ")
    return value


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NewEoatError(f"{role} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise NewEoatError(f"{role} must be finite")
    return result


def _vector(value: Any, size: int, role: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != size:
        raise NewEoatError(f"{role} must contain exactly {size} values")
    return tuple(_finite(item, role) for item in value)


def _digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise NewEoatError(f"{role} must be a lowercase SHA-256")
    return value


def _assert_close(actual: float, expected: float, tolerance: float, role: str) -> None:
    if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=tolerance):
        raise NewEoatError(f"{role} differs: expected={expected} actual={actual}")


def _resolve_origin(origin: Any, role: str) -> Path:
    if not isinstance(origin, str) or not origin:
        raise NewEoatError(f"{role} origin is invalid")
    candidate = Path(origin)
    if not candidate.is_absolute():
        candidate = EXPERIMENT_ROOT / candidate
    return candidate


def _validate_hashed_source(source: dict[str, Any], role: str) -> tuple[Path, str]:
    digest = _digest(source.get("sha256"), f"{role} digest")
    path = _resolve_origin(source.get("origin"), role)
    if _sha256(path) != digest:
        raise NewEoatError(f"{role} SHA-256 differs")
    return path, digest


def _validate_controller_receipt(
    binding: dict[str, Any],
    *,
    payload_kg: float,
    cog_m: tuple[float, ...],
    tcp_pose: tuple[float, ...],
) -> str:
    receipt_path, receipt_sha = _validate_hashed_source(binding, "controller GET receipt")
    if binding.get("required_schema") != RECEIPT_SCHEMA:
        raise NewEoatError("controller GET receipt schema binding differs")
    receipt = _read_json(receipt_path, "controller GET receipt")
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise NewEoatError("controller GET receipt schema differs")
    controller = receipt.get("controller")
    readback = receipt.get("readback")
    tolerances = receipt.get("tolerances")
    if not all(isinstance(item, dict) for item in (controller, readback, tolerances)):
        raise NewEoatError("controller GET receipt sections are missing")
    if controller.get("host") != "192.168.1.18":
        raise NewEoatError("controller GET receipt robot identity differs")
    if not (
        controller.get("remote_control") is True
        and controller.get("robot_mode") == "RUNNING"
        and controller.get("safety_mode") == "NORMAL"
        and controller.get("program_running") is False
    ):
        raise NewEoatError("controller GET receipt gates are not satisfied")
    payload_tolerance = _finite(tolerances.get("payload_kg"), "payload tolerance")
    cog_tolerance = _finite(tolerances.get("cog_m"), "CoG tolerance")
    tcp_tolerance = _finite(tolerances.get("tcp_m_rad"), "TCP tolerance")
    speed_linear_tolerance = _finite(
        tolerances.get("stationary_linear_m_s"), "linear speed tolerance"
    )
    speed_angular_tolerance = _finite(
        tolerances.get("stationary_angular_rad_s"), "angular speed tolerance"
    )
    _assert_close(
        _finite(readback.get("payload_kg"), "controller payload"),
        payload_kg,
        payload_tolerance,
        "controller payload",
    )
    receipt_cog = _vector(readback.get("payload_cog_m"), 3, "controller CoG")
    receipt_tcp = _vector(readback.get("tcp_offset_m_rad"), 6, "controller TCP")
    receipt_speed = _vector(
        readback.get("actual_tcp_speed_m_s_rad_s"), 6, "controller TCP speed"
    )
    for index, (actual, expected) in enumerate(zip(receipt_cog, cog_m, strict=True)):
        _assert_close(actual, expected, cog_tolerance, f"controller CoG[{index}]")
    for index, (actual, expected) in enumerate(zip(receipt_tcp, tcp_pose, strict=True)):
        _assert_close(actual, expected, tcp_tolerance, f"controller TCP[{index}]")
    if math.sqrt(sum(value * value for value in receipt_speed[:3])) > speed_linear_tolerance:
        raise NewEoatError("controller GET receipt is not linearly stationary")
    if math.sqrt(sum(value * value for value in receipt_speed[3:])) > speed_angular_tolerance:
        raise NewEoatError("controller GET receipt is not angularly stationary")
    return receipt_sha


def load_new_eoat(path: Path = DEFAULT_ARTIFACT) -> NewEoat:
    document = _read_json(path, "EOAT contract")
    expected_fields = {
        "schema",
        "artifact_id",
        "source_evidence",
        "fit",
        "tcp_derivation",
        "tcp_pose_m_rad",
        "controller_get_receipt",
        "controller_write_contract",
        "revocations",
        "claim_boundary",
    }
    _exact_object(document, expected_fields, "EOAT contract")
    if document["schema"] != SCHEMA or document["artifact_id"] != EXPECTED_ARTIFACT_ID:
        raise NewEoatError("EOAT contract identity differs")

    evidence = document["source_evidence"]
    if not isinstance(evidence, dict):
        raise NewEoatError("EOAT source evidence is missing")
    fit_source = evidence.get("eight_pose_fit")
    stack_source = evidence.get("digital_twin_stack")
    mac_source = evidence.get("mac_cad_model_stack")
    if not all(isinstance(item, dict) for item in (fit_source, stack_source, mac_source)):
        raise NewEoatError("EOAT source evidence sections are missing")
    _, source_sha = _validate_hashed_source(fit_source, "eight-pose fit")
    _validate_hashed_source(stack_source, "digital-twin stack")
    if (
        fit_source.get("pose_count") != 8
        or fit_source.get("samples_per_pose") != 400
        or _finite(fit_source.get("sampling_frequency_hz"), "fit frequency") != 50.0
    ):
        raise NewEoatError("eight-pose fit sampling contract differs")
    pose_sha = fit_source.get("pose_sha256")
    if not isinstance(pose_sha, list) or len(pose_sha) != 8:
        raise NewEoatError("eight-pose evidence digest set differs")
    for index, digest in enumerate(pose_sha, start=1):
        _digest(digest, f"R{index} digest")
        pose_path = _resolve_origin(
            str(Path(fit_source["origin"]).parent / f"R{index}.json"),
            f"R{index}",
        )
        if _sha256(pose_path) != digest:
            raise NewEoatError(f"R{index} SHA-256 differs")
    for field in (
        "readme_sha256",
        "verification_sha256",
        "validation_report_sha256",
        "cad_source_sha256",
        "model_yaml_sha256",
        "urdf_xacro_sha256",
        "step_sha256",
    ):
        _digest(mac_source.get(field), f"Mac CAD {field}")
    _assert_close(
        _finite(mac_source.get("user_measured_height_mm"), "measured height"),
        50.3,
        1e-12,
        "measured height",
    )

    fit = document["fit"]
    if not isinstance(fit, dict):
        raise NewEoatError("EOAT fit is missing")
    payload_kg = _finite(fit.get("payload_controller_kg"), "payload")
    cog_m = _vector(fit.get("cog_controller_m"), 3, "CoG")
    tcp_pose = _vector(document["tcp_pose_m_rad"], 6, "TCP pose")
    _assert_close(payload_kg, EXPECTED_PAYLOAD_KG, 1e-12, "payload")
    for index, (actual, expected) in enumerate(zip(cog_m, EXPECTED_COG_M, strict=True)):
        _assert_close(actual, expected, 1e-12, f"CoG[{index}]")
    for index, (actual, expected) in enumerate(zip(tcp_pose, EXPECTED_TCP, strict=True)):
        _assert_close(actual, expected, 1e-12, f"TCP[{index}]")

    derivation = document["tcp_derivation"]
    if not isinstance(derivation, dict):
        raise NewEoatError("TCP derivation is missing")
    residual = _finite(derivation.get("residual_stack_mm"), "residual stack")
    measured = _finite(derivation.get("measured_new_head_mm"), "measured head")
    unrounded = _finite(derivation.get("unrounded_tcp_mm"), "unrounded TCP")
    controller_tcp = _finite(derivation.get("controller_tcp_mm"), "controller TCP")
    program_z_delta_mm = _finite(
        derivation.get("program_z_delta_mm"), "program Z delta"
    )
    _assert_close(
        residual,
        _finite(stack_source.get("residual_stack_mm"), "stack residual"),
        1e-12,
        "residual stack",
    )
    _assert_close(unrounded, residual + measured, 1e-12, "TCP stack-up")
    _assert_close(controller_tcp, round(unrounded, 1), 1e-12, "rounded controller TCP")
    _assert_close(controller_tcp / 1000.0, tcp_pose[2], 1e-12, "TCP meters")
    _assert_close(program_z_delta_mm, 0.0, 1e-12, "program Z delta")

    revocations = document["revocations"]
    if not isinstance(revocations, list) or not any(
        isinstance(item, dict)
        and item.get("tcp_mm") == 59.14
        and item.get("status") == "REVOKED"
        for item in revocations
    ):
        raise NewEoatError("59.14 mm TCP revocation is missing")
    write_contract = document["controller_write_contract"]
    if write_contract != {
        "payload_api": "set_target_payload_three_argument",
        "tcp_api": "set_tcp",
        "tp_initialization_required": True,
        "premotion_controller_get_match_required": True,
    }:
        raise NewEoatError("controller write contract differs")
    if "runtime_readback_passed" in json.dumps(document, sort_keys=True):
        raise NewEoatError("self-reported runtime_readback_passed is forbidden")
    receipt_sha = _validate_controller_receipt(
        document["controller_get_receipt"],
        payload_kg=payload_kg,
        cog_m=cog_m,
        tcp_pose=tcp_pose,
    )

    boundary = document["claim_boundary"]
    if not isinstance(boundary, list) or not boundary or any(
        not isinstance(item, str) or not item for item in boundary
    ):
        raise NewEoatError("EOAT claim boundary is invalid")
    return NewEoat(
        artifact_id=document["artifact_id"],
        artifact_sha256=_sha256(path),
        source_sha256=source_sha,
        controller_receipt_sha256=receipt_sha,
        payload_kg=payload_kg,
        cog_m=(cog_m[0], cog_m[1], cog_m[2]),
        tcp_pose_m_rad=(
            tcp_pose[0],
            tcp_pose[1],
            tcp_pose[2],
            tcp_pose[3],
            tcp_pose[4],
            tcp_pose[5],
        ),
        program_z_delta_m=program_z_delta_mm / 1000.0,
    )


def _format_vector(values: Sequence[float]) -> str:
    return ", ".join(f"{value:.9f}" for value in values)


def urscript_initialization_block(eoat: NewEoat | None = None) -> str:
    selected = eoat or load_new_eoat()
    return (
        f"  # EOAT_CONTRACT_ID: {selected.artifact_id}\n"
        f"  # EOAT_CONTRACT_SHA256: {selected.artifact_sha256}\n"
        f"  # EOAT_CONTROLLER_GET_SHA256: {selected.controller_receipt_sha256}\n"
        f"  set_target_payload({selected.payload_kg:.3f},"
        f" [{_format_vector(selected.cog_m)}], [0, 0, 0, 0, 0, 0])\n"
        f"  set_tcp(p[{_format_vector(selected.tcp_pose_m_rad)}])"
    )


__all__ = [
    "DEFAULT_ARTIFACT",
    "EXPECTED_ARTIFACT_ID",
    "NewEoat",
    "NewEoatError",
    "load_new_eoat",
    "urscript_initialization_block",
]
