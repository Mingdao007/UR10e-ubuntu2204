"""Strict loader for the compact, evidence-backed V3 runtime calibration."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


EXPERIMENT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACT = (
    EXPERIMENT_ROOT
    / "config/step5d/manifests/step5d_strict_rnn_autotune_v3/runtime_calibration.json"
)
SCHEMA = "step5d.autotune-v3/runtime-calibration-v1"
EXPECTED_ARTIFACT_ID = "step5c-calibrated-tcp-offset-20260613"
EXPECTED_CALIBRATION_HASH = "calib_7367377276742883610"
EXPECTED_CALIBRATION_SHA256 = (
    "029aad03affc0cf60fc4274029463e683dc04b709f3a5e849f693d1c70409be8"
)
class RuntimeCalibrationError(RuntimeError):
    """The compact runtime calibration or its installed model source differs."""


@dataclass(frozen=True)
class RuntimeCalibration:
    artifact_id: str
    artifact_sha256: str
    calibration_hash: str
    calibration_yaml: Path
    calibration_yaml_sha256: str
    xacro_path: Path
    tcp_offset_tool0_m: tuple[float, float, float]
    source_csv_sha256: str
    source_summary_sha256: str
    finite_samples: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeCalibrationError(f"runtime calibration repeats key {key!r}")
        result[key] = value
    return result


def _exact_object(value: Any, fields: set[str], role: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeCalibrationError(f"{role} fields differ")
    return value


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeCalibrationError(f"{role} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise RuntimeCalibrationError(f"{role} must be finite")
    return number


def _digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RuntimeCalibrationError(f"{role} must be a lowercase SHA-256")
    return value


def load_runtime_calibration(path: Path = DEFAULT_ARTIFACT) -> RuntimeCalibration:
    if path.is_symlink() or not path.is_file():
        raise RuntimeCalibrationError(f"runtime calibration must be a regular file: {path}")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                RuntimeCalibrationError(f"non-finite JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeCalibrationError(f"runtime calibration is unreadable: {exc}") from exc
    document = _exact_object(
        payload,
        {
            "schema",
            "artifact_id",
            "source_evidence",
            "calibrated_model",
            "runtime_value",
            "audit_metrics",
            "thresholds",
            "gates",
            "claim_boundary",
        },
        "runtime calibration",
    )
    if document["schema"] != SCHEMA or document["artifact_id"] != EXPECTED_ARTIFACT_ID:
        raise RuntimeCalibrationError("runtime calibration identity differs")

    source = _exact_object(
        document["source_evidence"],
        {
            "bridge_csv_origin",
            "bridge_csv_sha256",
            "audit_summary_origin",
            "audit_summary_sha256",
            "audit_generated_at",
            "finite_samples",
        },
        "source evidence",
    )
    source_csv_sha = _digest(source["bridge_csv_sha256"], "source CSV digest")
    source_summary_sha = _digest(source["audit_summary_sha256"], "audit summary digest")
    if not isinstance(source["finite_samples"], int) or source["finite_samples"] < 1000:
        raise RuntimeCalibrationError("source evidence sample count is insufficient")

    model = _exact_object(
        document["calibrated_model"],
        {
            "calibration_yaml",
            "calibration_yaml_sha256",
            "calibration_hash",
            "xacro_path",
            "required_frames",
        },
        "calibrated model",
    )
    if (
        model["calibration_hash"] != EXPECTED_CALIBRATION_HASH
        or model["calibration_yaml_sha256"] != EXPECTED_CALIBRATION_SHA256
        or model["required_frames"] != ["base", "tool0", "flange"]
    ):
        raise RuntimeCalibrationError("calibrated model identity differs")
    calibration_relative = Path(model["calibration_yaml"])
    if calibration_relative.is_absolute() or ".." not in calibration_relative.parts:
        raise RuntimeCalibrationError("calibration YAML must use the reviewed repo-relative path")
    calibration_yaml = (EXPERIMENT_ROOT / calibration_relative).resolve()
    xacro_path = Path(model["xacro_path"])
    if not xacro_path.is_absolute():
        raise RuntimeCalibrationError("xacro path must be absolute")

    runtime = _exact_object(
        document["runtime_value"],
        {"tcp_offset_tool0_mean_xyz_m", "tcp_offset_norm_m"},
        "runtime value",
    )
    raw_offset = runtime["tcp_offset_tool0_mean_xyz_m"]
    if not isinstance(raw_offset, list) or len(raw_offset) != 3:
        raise RuntimeCalibrationError("TCP offset must contain exactly three values")
    offset = tuple(_finite(value, "TCP offset") for value in raw_offset)
    computed_norm = math.sqrt(sum(value * value for value in offset))
    stated_norm = _finite(runtime["tcp_offset_norm_m"], "TCP offset norm")
    if not math.isclose(computed_norm, stated_norm, rel_tol=0.0, abs_tol=1e-12):
        raise RuntimeCalibrationError("TCP offset norm differs from the vector")

    thresholds = _exact_object(
        document["thresholds"],
        {
            "tcp_offset_norm_min_m",
            "tcp_offset_norm_max_m",
            "fk_offset_std_limit_m",
            "tcp_rotation_error_limit_rad",
            "speed_vector_rms_limit",
        },
        "thresholds",
    )
    minimum = _finite(thresholds["tcp_offset_norm_min_m"], "minimum TCP norm")
    maximum = _finite(thresholds["tcp_offset_norm_max_m"], "maximum TCP norm")
    if not minimum <= stated_norm <= maximum:
        raise RuntimeCalibrationError("TCP offset is outside the audited norm gate")

    metrics = _exact_object(
        document["audit_metrics"],
        {
            "tcp_offset_std_xyz_m",
            "tcp_offset_max_deviation_norm_m",
            "tcp_rotation_error_max_rad",
            "jacobian_linear_vector_rms_m_s",
            "jacobian_angular_vector_rms_rad_s",
        },
        "audit metrics",
    )
    std = metrics["tcp_offset_std_xyz_m"]
    if not isinstance(std, list) or len(std) != 3:
        raise RuntimeCalibrationError("TCP offset standard deviation must have three values")
    if max(_finite(value, "TCP offset standard deviation") for value in std) >= _finite(
        thresholds["fk_offset_std_limit_m"], "FK offset limit"
    ):
        raise RuntimeCalibrationError("TCP offset stability gate failed")
    if _finite(metrics["tcp_rotation_error_max_rad"], "rotation error") >= _finite(
        thresholds["tcp_rotation_error_limit_rad"], "rotation error limit"
    ):
        raise RuntimeCalibrationError("TCP rotation gate failed")
    speed_limit = _finite(thresholds["speed_vector_rms_limit"], "speed RMS limit")
    for field in (
        "jacobian_linear_vector_rms_m_s",
        "jacobian_angular_vector_rms_rad_s",
    ):
        if _finite(metrics[field], field) >= speed_limit:
            raise RuntimeCalibrationError(f"{field} gate failed")

    gates = document["gates"]
    if not isinstance(gates, dict) or not gates or set(gates.values()) != {True}:
        raise RuntimeCalibrationError("recorded calibration gates are incomplete")
    boundary = document["claim_boundary"]
    if not isinstance(boundary, list) or not boundary or any(
        not isinstance(item, str) or not item for item in boundary
    ):
        raise RuntimeCalibrationError("runtime calibration claim boundary is invalid")

    return RuntimeCalibration(
        artifact_id=document["artifact_id"],
        artifact_sha256=_sha256(path),
        calibration_hash=model["calibration_hash"],
        calibration_yaml=calibration_yaml,
        calibration_yaml_sha256=model["calibration_yaml_sha256"],
        xacro_path=xacro_path,
        tcp_offset_tool0_m=offset,
        source_csv_sha256=source_csv_sha,
        source_summary_sha256=source_summary_sha,
        finite_samples=source["finite_samples"],
    )


def validate_installed_calibration(path: Path = DEFAULT_ARTIFACT) -> RuntimeCalibration:
    calibration = load_runtime_calibration(path)
    if calibration.calibration_yaml.is_symlink() or not calibration.calibration_yaml.is_file():
        raise RuntimeCalibrationError(
            f"installed calibration YAML is missing or unsafe: {calibration.calibration_yaml}"
        )
    if _sha256(calibration.calibration_yaml) != calibration.calibration_yaml_sha256:
        raise RuntimeCalibrationError("installed calibration YAML digest differs")
    if not calibration.xacro_path.is_file():
        raise RuntimeCalibrationError(f"installed UR xacro is missing: {calibration.xacro_path}")
    return calibration


def dependency_observation(path: Path = DEFAULT_ARTIFACT) -> dict[str, Any]:
    try:
        calibration = validate_installed_calibration(path)
    except RuntimeCalibrationError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "artifact_id": calibration.artifact_id,
        "artifact_sha256": calibration.artifact_sha256,
        "calibration_hash": calibration.calibration_hash,
        "calibration_yaml_sha256": calibration.calibration_yaml_sha256,
        "source_csv_sha256": calibration.source_csv_sha256,
        "source_summary_sha256": calibration.source_summary_sha256,
        "finite_samples": calibration.finite_samples,
    }
