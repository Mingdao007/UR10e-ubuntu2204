"""r008 geometry calibration: fixed Home + sealed contact XYZ → Δz.

Contact XYZ is a configuration/calibration product. Without a sealed contact
pose, speed-optimized FAR/NEAR schedules must not be planned (observation /
calibration runs only).
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from step5d_autotune_v4_r004.home_profile import (
    FIXED_HOME_POSE,
    HOME_PROFILE_ID,
    load_fixed_home_profile,
)
from step5d_autotune_v4_r006.contracts import canonical_bytes
from step5d_eoat_profiles import load_new_eoat_profile

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CALIBRATION_PATH = ROOT / "config/step5d/autotune_v4_r008_geometry_calibration.json"
CALIBRATION_SCHEMA = "step5d.autotune-v4/r008-geometry-calibration-v1"
CALIBRATION_VERSION = "r008-geometry-v1"

PROVENANCE_UNSET: Final[str] = "unset"
PROVENANCE_OPERATOR_SEALED: Final[str] = "operator_sealed"
PROVENANCE_WAVE1_STATE20_SEALED: Final[str] = "wave1_state20_sealed"
PROVENANCE_V3_PRIOR_UNTRUSTED: Final[str] = "v3_prior_untrusted"

SPEED_OPT_PROVENANCE: Final[frozenset[str]] = frozenset(
    {
        PROVENANCE_OPERATOR_SEALED,
        PROVENANCE_WAVE1_STATE20_SEALED,
    }
)
ALL_PROVENANCE: Final[frozenset[str]] = frozenset(
    {
        PROVENANCE_UNSET,
        PROVENANCE_OPERATOR_SEALED,
        PROVENANCE_WAVE1_STATE20_SEALED,
        PROVENANCE_V3_PRIOR_UNTRUSTED,
    }
)

DEFAULT_XY_TOLERANCE_M: Final[float] = 0.001
Pose6 = tuple[float, float, float, float, float, float]


class GeometryCalibrationError(ValueError):
    """Geometry calibration is missing, malformed, or unbound."""


def _digest(value: Any, role: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        c not in "0123456789abcdef" for c in value
    ):
        raise GeometryCalibrationError(f"{role} must be a lowercase SHA-256")
    return value


def _pose6(value: Any, role: str) -> Pose6:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise GeometryCalibrationError(f"{role} must be a 6-vector")
    try:
        result = tuple(float(item) for item in value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GeometryCalibrationError(f"{role} must be numeric") from exc
    if len(result) != 6 or not all(math.isfinite(x) for x in result):
        raise GeometryCalibrationError(f"{role} must contain six finite values")
    return result  # type: ignore[return-value]


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GeometryCalibrationError(f"{role} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise GeometryCalibrationError(f"{role} must be finite")
    return result


def _positive(value: Any, role: str) -> float:
    result = _finite(value, role)
    if result <= 0.0:
        raise GeometryCalibrationError(f"{role} must be positive")
    return result


@dataclass(frozen=True)
class GeometryCalibration:
    """Home + optional sealed contact geometry for B3 search planning."""

    schema: str
    version: str
    home_pose_m_rad: Pose6
    home_profile_id: str
    contact_pose_m_rad: Pose6 | None
    delta_z_m: float | None
    provenance: str
    xy_tolerance_m: float
    eoat_sha256: str
    seal_sha256: str
    notes: Mapping[str, Any]

    @property
    def has_contact(self) -> bool:
        return self.contact_pose_m_rad is not None and self.delta_z_m is not None

    @property
    def allows_speed_opt(self) -> bool:
        return self.has_contact and self.provenance in SPEED_OPT_PROVENANCE

    def _unsigned(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "home_pose_m_rad": list(self.home_pose_m_rad),
            "home_profile_id": self.home_profile_id,
            "contact_pose_m_rad": (
                None if self.contact_pose_m_rad is None else list(self.contact_pose_m_rad)
            ),
            "delta_z_m": self.delta_z_m,
            "provenance": self.provenance,
            "xy_tolerance_m": self.xy_tolerance_m,
            "eoat_sha256": self.eoat_sha256,
            "notes": dict(self.notes),
        }

    def as_dict(self) -> dict[str, Any]:
        document = self._unsigned()
        document["seal_sha256"] = self.seal_sha256
        return document

    def validate_seal(self) -> None:
        expected = hashlib.sha256(canonical_bytes(self._unsigned())).hexdigest()
        if expected != self.seal_sha256:
            raise GeometryCalibrationError("geometry calibration seal differs")


def compute_delta_z_m(home_pose: Pose6, contact_pose: Pose6) -> float:
    """Downward gap home_z - contact_z (search travel along −Z)."""

    return float(home_pose[2] - contact_pose[2])


def seal_geometry_document(unsigned: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(dict(unsigned))).hexdigest()


def build_geometry_calibration(
    *,
    home_pose_m_rad: Sequence[float] | None = None,
    contact_pose_m_rad: Sequence[float] | None = None,
    provenance: str = PROVENANCE_UNSET,
    xy_tolerance_m: float = DEFAULT_XY_TOLERANCE_M,
    eoat_sha256: str | None = None,
    notes: Mapping[str, Any] | None = None,
    require_home_match: bool = True,
) -> GeometryCalibration:
    """Build and seal a calibration document (for tests / operator tools)."""

    home = _pose6(
        FIXED_HOME_POSE if home_pose_m_rad is None else home_pose_m_rad,
        "home_pose_m_rad",
    )
    if require_home_match and home != FIXED_HOME_POSE:
        raise GeometryCalibrationError(
            "home_pose_m_rad must match FIXED_HOME_POSE / Script1 target"
        )

    eoat = load_new_eoat_profile()
    eoat_digest = eoat.profile_sha256 if eoat_sha256 is None else _digest(eoat_sha256, "eoat_sha256")
    if eoat_digest != eoat.profile_sha256:
        raise GeometryCalibrationError("eoat_sha256 differs from loaded new-EOAT profile")

    contact: Pose6 | None = None
    delta_z: float | None = None
    if contact_pose_m_rad is not None:
        contact = _pose6(contact_pose_m_rad, "contact_pose_m_rad")
        delta_z = round(compute_delta_z_m(home, contact), 12)

    if provenance not in ALL_PROVENANCE:
        raise GeometryCalibrationError(f"provenance is unsupported: {provenance}")

    unsigned = {
        "schema": CALIBRATION_SCHEMA,
        "version": CALIBRATION_VERSION,
        "home_pose_m_rad": list(home),
        "home_profile_id": HOME_PROFILE_ID,
        "contact_pose_m_rad": None if contact is None else list(contact),
        "delta_z_m": delta_z,
        "provenance": provenance,
        "xy_tolerance_m": float(xy_tolerance_m),
        "eoat_sha256": eoat_digest,
        "notes": dict(notes or {}),
    }
    seal = seal_geometry_document(unsigned)
    return validate_geometry_calibration({**unsigned, "seal_sha256": seal})


def validate_geometry_calibration(document: Mapping[str, Any]) -> GeometryCalibration:
    if not isinstance(document, Mapping):
        raise GeometryCalibrationError("calibration must be an object")
    required = {
        "schema",
        "version",
        "home_pose_m_rad",
        "home_profile_id",
        "contact_pose_m_rad",
        "delta_z_m",
        "provenance",
        "xy_tolerance_m",
        "eoat_sha256",
        "seal_sha256",
        "notes",
    }
    if set(document) != required:
        missing = sorted(required - set(document))
        extra = sorted(set(document) - required)
        raise GeometryCalibrationError(
            f"calibration fields differ (missing={missing}, extra={extra})"
        )
    if document.get("schema") != CALIBRATION_SCHEMA:
        raise GeometryCalibrationError("calibration schema differs")
    if document.get("version") != CALIBRATION_VERSION:
        raise GeometryCalibrationError("calibration version differs")
    if document.get("home_profile_id") != HOME_PROFILE_ID:
        raise GeometryCalibrationError("home_profile_id differs from r004 fixed Home")

    home = _pose6(document.get("home_pose_m_rad"), "home_pose_m_rad")
    if home != FIXED_HOME_POSE:
        raise GeometryCalibrationError(
            "home_pose_m_rad must match FIXED_HOME_POSE / Script1 target"
        )

    provenance = document.get("provenance")
    if provenance not in ALL_PROVENANCE:
        raise GeometryCalibrationError(f"provenance is unsupported: {provenance}")

    xy_tol = _positive(document.get("xy_tolerance_m"), "xy_tolerance_m")
    eoat_sha256 = _digest(document.get("eoat_sha256"), "eoat_sha256")
    home_profile = load_fixed_home_profile()
    if eoat_sha256 != home_profile.eoat_profile_sha256:
        raise GeometryCalibrationError("eoat_sha256 differs from fixed-Home EOAT binding")

    contact_raw = document.get("contact_pose_m_rad")
    delta_raw = document.get("delta_z_m")
    contact: Pose6 | None
    delta_z: float | None
    if contact_raw is None:
        contact = None
        if delta_raw is not None:
            raise GeometryCalibrationError("delta_z_m must be null when contact is null")
        delta_z = None
        if provenance in SPEED_OPT_PROVENANCE:
            raise GeometryCalibrationError(
                "speed-opt provenance requires a sealed contact_pose_m_rad"
            )
    else:
        contact = _pose6(contact_raw, "contact_pose_m_rad")
        if delta_raw is None:
            raise GeometryCalibrationError("delta_z_m is required when contact is set")
        delta_z = round(_finite(delta_raw, "delta_z_m"), 12)
        expected = round(compute_delta_z_m(home, contact), 12)
        if not math.isclose(delta_z, expected, rel_tol=0.0, abs_tol=1e-12):
            raise GeometryCalibrationError(
                f"delta_z_m must equal home_z - contact_z ({expected})"
            )
        if delta_z <= 0.0:
            raise GeometryCalibrationError("delta_z_m must be positive (contact below home)")
        dx = contact[0] - home[0]
        dy = contact[1] - home[1]
        if math.hypot(dx, dy) > xy_tol:
            raise GeometryCalibrationError(
                "contact XY differs from home beyond xy_tolerance_m; re-calibrate"
            )

    notes = document.get("notes")
    if not isinstance(notes, Mapping):
        raise GeometryCalibrationError("notes must be an object")

    seal = _digest(document.get("seal_sha256"), "seal_sha256")
    calibration = GeometryCalibration(
        schema=CALIBRATION_SCHEMA,
        version=CALIBRATION_VERSION,
        home_pose_m_rad=home,
        home_profile_id=HOME_PROFILE_ID,
        contact_pose_m_rad=contact,
        delta_z_m=delta_z,
        provenance=str(provenance),
        xy_tolerance_m=xy_tol,
        eoat_sha256=eoat_sha256,
        seal_sha256=seal,
        notes=dict(notes),
    )
    calibration.validate_seal()
    return calibration


def load_geometry_calibration(path: Path | None = None) -> GeometryCalibration:
    calibration_path = DEFAULT_CALIBRATION_PATH if path is None else Path(path)
    if calibration_path.is_symlink() or not calibration_path.is_file():
        raise GeometryCalibrationError(f"calibration missing or unsafe: {calibration_path}")
    try:
        document = json.loads(calibration_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GeometryCalibrationError(f"calibration is not strict JSON: {exc}") from exc
    return validate_geometry_calibration(document)


__all__ = [
    "ALL_PROVENANCE",
    "CALIBRATION_SCHEMA",
    "CALIBRATION_VERSION",
    "DEFAULT_CALIBRATION_PATH",
    "DEFAULT_XY_TOLERANCE_M",
    "GeometryCalibration",
    "GeometryCalibrationError",
    "PROVENANCE_OPERATOR_SEALED",
    "PROVENANCE_UNSET",
    "PROVENANCE_V3_PRIOR_UNTRUSTED",
    "PROVENANCE_WAVE1_STATE20_SEALED",
    "SPEED_OPT_PROVENANCE",
    "build_geometry_calibration",
    "compute_delta_z_m",
    "load_geometry_calibration",
    "seal_geometry_document",
    "validate_geometry_calibration",
]
