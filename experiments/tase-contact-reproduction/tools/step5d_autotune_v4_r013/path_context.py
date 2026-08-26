"""Pure offline path providers for R013 correction experiments.

The providers describe geometry in an abstract base-frame along/lateral
basis.  They do not contain a safe-frame pose, controller transport, or any
live acceptance claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any, Mapping, Sequence


PATH_IDENTITY_SCHEMA = "step5d.autotune-v4/r013-path-identity-v1"
PATH_CONTEXT_SCHEMA = "step5d.autotune-v4/r013-path-context-sample-v1"
PATH_CONTEXT_VERSION = 1
PLANAR_BASIS_SCHEMA = "step5d.autotune-v4/r013-planar-basis-receipt-v1"
PLANAR_BASIS_VERSION = 1
BASE_FRAME_ID = "base_frame_along_lateral_orientation_v1"
SIGNED_CURVATURE_ORIENTATION = (
    "positive=dot(cross(along_base,lateral_base),normal_base)"
)
CYCLOID_PATH_ID = "r013_cycloid_v1"
FIGURE8_PATH_ID = "r013_figure8_v1"
FIGURE8_DURATION_S = 60.0
FIGURE8_FORMAL_START_S = 5.0
FIGURE8_BIN_WIDTH_S = 0.1
FIGURE8_FORMAL_BIN_COUNT = 550
FIGURE8_FULL_BIN_COUNT = 600
FIGURE8_ALONG_AMPLITUDE_M = 0.04
FIGURE8_LATERAL_AMPLITUDE_M = 0.01
FIGURE8_ALONG_OMEGA_RAD_S = 0.1
FIGURE8_LATERAL_OMEGA_RAD_S = 0.2
FIGURE8_MAX_SPEED_M_S = math.hypot(
    FIGURE8_ALONG_AMPLITUDE_M * FIGURE8_ALONG_OMEGA_RAD_S,
    FIGURE8_LATERAL_AMPLITUDE_M * FIGURE8_LATERAL_OMEGA_RAD_S,
)
FIGURE8_GEOMETRY_STATUS = "offline_geometry_only_not_live_acceptance"
DEGENERATE_SPEED_EPS_M_S = 1e-12


class PathContextError(ValueError):
    """A path context or provider identity is invalid."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise PathContextError(f"R013 {name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PathContextError(f"R013 {name} must be numeric") from exc
    if not math.isfinite(result):
        raise PathContextError(f"R013 {name} must be finite")
    return result


def _finite_tuple(value: Sequence[Any], length: int, name: str) -> tuple[float, ...]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or len(value) != length
    ):
        raise PathContextError(f"R013 {name} must contain {length} values")
    return tuple(_finite(item, f"{name}[{index}]") for index, item in enumerate(value))


def _sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlanarBasisReceiptV1:
    """Explicit orthonormal along/lateral basis expressed in base coordinates."""

    along_base: tuple[float, float, float]
    lateral_base: tuple[float, float, float]
    signed_curvature_orientation: str = SIGNED_CURVATURE_ORIENTATION
    schema: str = PLANAR_BASIS_SCHEMA
    version: int = PLANAR_BASIS_VERSION

    def __post_init__(self) -> None:
        if self.schema != PLANAR_BASIS_SCHEMA or self.version != PLANAR_BASIS_VERSION:
            raise PathContextError("R013 planar basis schema/version differs")
        along = _finite_tuple(self.along_base, 3, "along_base")
        lateral = _finite_tuple(self.lateral_base, 3, "lateral_base")
        along_norm = math.sqrt(math.fsum(value * value for value in along))
        lateral_norm = math.sqrt(math.fsum(value * value for value in lateral))
        dot = math.fsum(a * b for a, b in zip(along, lateral, strict=True))
        if not math.isclose(along_norm, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise PathContextError("R013 along_base must be unit length")
        if not math.isclose(lateral_norm, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise PathContextError("R013 lateral_base must be unit length")
        if not math.isclose(dot, 0.0, rel_tol=0.0, abs_tol=1e-9):
            raise PathContextError("R013 planar basis vectors must be orthogonal")
        normal = self._cross(along, lateral)
        normal_norm = math.sqrt(math.fsum(value * value for value in normal))
        if normal_norm <= 1e-12 or not math.isclose(
            normal_norm, 1.0, rel_tol=0.0, abs_tol=1e-9
        ):
            raise PathContextError("R013 planar basis is degenerate")
        if self.signed_curvature_orientation != SIGNED_CURVATURE_ORIENTATION:
            raise PathContextError("R013 signed-curvature orientation differs")
        object.__setattr__(self, "along_base", along)
        object.__setattr__(self, "lateral_base", lateral)

    @staticmethod
    def _cross(
        left: Sequence[float], right: Sequence[float]
    ) -> tuple[float, float, float]:
        return (
            left[1] * right[2] - left[2] * right[1],
            left[2] * right[0] - left[0] * right[2],
            left[0] * right[1] - left[1] * right[0],
        )

    @classmethod
    def identity(cls) -> "PlanarBasisReceiptV1":
        return cls(along_base=(1.0, 0.0, 0.0), lateral_base=(0.0, 1.0, 0.0))

    @property
    def normal_base(self) -> tuple[float, float, float]:
        return self._cross(self.along_base, self.lateral_base)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "along_base": list(self.along_base),
            "lateral_base": list(self.lateral_base),
            "normal_base": list(self.normal_base),
            "signed_curvature_orientation": self.signed_curvature_orientation,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PlanarBasisReceiptV1":
        required = {
            "schema", "version", "along_base", "lateral_base", "normal_base",
            "signed_curvature_orientation",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise PathContextError("R013 planar basis receipt fields differ")
        parsed = cls(
            along_base=tuple(value["along_base"]),
            lateral_base=tuple(value["lateral_base"]),
            signed_curvature_orientation=value["signed_curvature_orientation"],
            schema=value["schema"],
            version=value["version"],
        )
        if tuple(_finite_tuple(value["normal_base"], 3, "normal_base")) != parsed.normal_base:
            raise PathContextError("R013 planar basis normal differs")
        return parsed


IDENTITY_PLANAR_BASIS = PlanarBasisReceiptV1.identity()


@dataclass(frozen=True)
class PathIdentityReceiptV1:
    path_id: str
    formula_id: str
    duration_s: float = 1.0
    anchor_pose_base: tuple[float, float, float, float, float, float] = (0.0,) * 6
    basis_receipt: PlanarBasisReceiptV1 = field(default_factory=PlanarBasisReceiptV1.identity)
    frame_id: str = BASE_FRAME_ID
    source_identity: str = ""
    geometry_status: str = "offline_geometry_only"
    schema: str = PATH_IDENTITY_SCHEMA
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != PATH_IDENTITY_SCHEMA or self.version != 1:
            raise PathContextError("R013 path identity schema/version differs")
        if not self.path_id or not self.formula_id or self.frame_id != BASE_FRAME_ID:
            raise PathContextError("R013 path identity fields differ")
        duration = _finite(self.duration_s, "path duration")
        if duration <= 0.0:
            raise PathContextError("R013 path duration must be positive")
        anchor = _finite_tuple(self.anchor_pose_base, 6, "anchor_pose_base")
        basis = self.basis_receipt
        if isinstance(basis, Mapping):
            basis = PlanarBasisReceiptV1.from_mapping(basis)
        if not isinstance(basis, PlanarBasisReceiptV1):
            raise PathContextError("R013 path identity basis receipt is not typed")
        object.__setattr__(self, "duration_s", duration)
        object.__setattr__(self, "anchor_pose_base", anchor)
        object.__setattr__(self, "basis_receipt", basis)
        source_payload = self._source_payload()
        expected_source = _sha256(source_payload)
        source = self.source_identity
        if not source:
            source = expected_source
            object.__setattr__(self, "source_identity", source)
        if (
            not isinstance(source, str)
            or len(source) != 64
            or any(char not in "0123456789abcdef" for char in source)
        ):
            raise PathContextError("R013 path source identity must be SHA-256")
        if source != expected_source:
            raise PathContextError("R013 path source identity does not bind geometry")
        if self.geometry_status == FIGURE8_GEOMETRY_STATUS and self.path_id != FIGURE8_PATH_ID:
            raise PathContextError("R013 Figure-eight geometry status is path-specific")

    def _source_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "path_id": self.path_id,
            "formula_id": self.formula_id,
            "frame_id": self.frame_id,
            "duration_s": self.duration_s,
            "anchor_pose_base": list(self.anchor_pose_base),
            "basis_receipt": self.basis_receipt.as_dict(),
            "geometry_status": self.geometry_status,
        }

    @property
    def sha256(self) -> str:
        return _sha256(self.as_dict(include_sha=False))

    def as_dict(self, *, include_sha: bool = True) -> dict[str, Any]:
        result = {
            "schema": self.schema,
            "version": self.version,
            "path_id": self.path_id,
            "formula_id": self.formula_id,
            "frame_id": self.frame_id,
            "duration_s": self.duration_s,
            "anchor_pose_base": list(self.anchor_pose_base),
            "basis_receipt": self.basis_receipt.as_dict(),
            "source_identity": self.source_identity,
            "geometry_status": self.geometry_status,
        }
        if include_sha:
            result["path_identity_sha256"] = self.sha256
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PathIdentityReceiptV1":
        required = {
            "schema", "version", "path_id", "formula_id", "frame_id",
            "duration_s", "anchor_pose_base", "basis_receipt", "source_identity",
            "geometry_status", "path_identity_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise PathContextError("R013 path identity receipt fields differ")
        parsed = cls(
            path_id=value["path_id"], formula_id=value["formula_id"],
            frame_id=value["frame_id"], source_identity=value["source_identity"],
            duration_s=value["duration_s"],
            anchor_pose_base=tuple(value["anchor_pose_base"]),
            basis_receipt=PlanarBasisReceiptV1.from_mapping(value["basis_receipt"]),
            geometry_status=value["geometry_status"], schema=value["schema"], version=value["version"],
        )
        if value["path_identity_sha256"] != parsed.sha256:
            raise PathContextError("R013 path identity receipt hash differs")
        return parsed


@dataclass(frozen=True)
class PathContextSampleV1:
    path_id: str
    path_time_s: float
    phase_rad: float
    normalized_progress: float
    desired_pose_base: tuple[float, float, float, float, float, float]
    desired_twist_base: tuple[float, float, float, float, float, float]
    desired_acceleration_base: tuple[float, float, float, float, float, float]
    scalar_speed_m_s: float
    signed_tangential_acceleration_m_s2: float
    signed_planar_curvature_m_inv: float
    degenerate_speed: bool
    identity_receipt: PathIdentityReceiptV1
    schema: str = PATH_CONTEXT_SCHEMA
    version: int = PATH_CONTEXT_VERSION

    def __post_init__(self) -> None:
        if self.schema != PATH_CONTEXT_SCHEMA or self.version != PATH_CONTEXT_VERSION:
            raise PathContextError("R013 path context schema/version differs")
        if self.identity_receipt.path_id != self.path_id:
            raise PathContextError("R013 path context identity differs")
        path_time = _finite(self.path_time_s, "path_time_s")
        if path_time < 0.0:
            raise PathContextError("R013 path_time_s must be non-negative")
        object.__setattr__(self, "path_time_s", path_time)
        object.__setattr__(self, "phase_rad", _finite(self.phase_rad, "phase_rad"))
        progress = _finite(self.normalized_progress, "normalized_progress")
        if not 0.0 <= progress <= 1.0:
            raise PathContextError("R013 normalized progress must be in [0,1]")
        object.__setattr__(self, "normalized_progress", progress)
        object.__setattr__(self, "desired_pose_base", _finite_tuple(self.desired_pose_base, 6, "desired_pose_base"))
        object.__setattr__(self, "desired_twist_base", _finite_tuple(self.desired_twist_base, 6, "desired_twist_base"))
        object.__setattr__(
            self,
            "desired_acceleration_base",
            _finite_tuple(self.desired_acceleration_base, 6, "desired_acceleration_base"),
        )
        speed = _finite(self.scalar_speed_m_s, "scalar_speed_m_s")
        if speed < 0.0:
            raise PathContextError("R013 scalar speed must be non-negative")
        object.__setattr__(self, "scalar_speed_m_s", speed)
        object.__setattr__(
            self,
            "signed_tangential_acceleration_m_s2",
            _finite(self.signed_tangential_acceleration_m_s2, "signed_tangential_acceleration_m_s2"),
        )
        object.__setattr__(
            self,
            "signed_planar_curvature_m_inv",
            _finite(self.signed_planar_curvature_m_inv, "signed_planar_curvature_m_inv"),
        )
        if type(self.degenerate_speed) is not bool:
            raise PathContextError("R013 degenerate speed flag must be boolean")

    @property
    def desired_pose_base_m(self) -> tuple[float, ...]:
        return self.desired_pose_base

    @property
    def desired_twist_base_m_s(self) -> tuple[float, ...]:
        return self.desired_twist_base

    @property
    def desired_acceleration_base_m_s2(self) -> tuple[float, ...]:
        return self.desired_acceleration_base

    @property
    def path_identity_sha256(self) -> str:
        return self.identity_receipt.sha256

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "path_id": self.path_id,
            "path_time_s": self.path_time_s,
            "phase_rad": self.phase_rad,
            "normalized_progress": self.normalized_progress,
            "desired_pose_base": list(self.desired_pose_base),
            "desired_twist_base": list(self.desired_twist_base),
            "desired_acceleration_base": list(self.desired_acceleration_base),
            "scalar_speed_m_s": self.scalar_speed_m_s,
            "signed_tangential_acceleration_m_s2": self.signed_tangential_acceleration_m_s2,
            "signed_planar_curvature_m_inv": self.signed_planar_curvature_m_inv,
            "degenerate_speed": self.degenerate_speed,
            "identity_receipt": self.identity_receipt.as_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PathContextSampleV1":
        required = {
            "schema", "version", "path_id", "path_time_s", "phase_rad",
            "normalized_progress", "desired_pose_base", "desired_twist_base",
            "desired_acceleration_base",
            "scalar_speed_m_s", "signed_tangential_acceleration_m_s2",
            "signed_planar_curvature_m_inv", "degenerate_speed", "identity_receipt",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise PathContextError("R013 path context fields differ")
        return cls(
            path_id=value["path_id"], path_time_s=value["path_time_s"],
            phase_rad=value["phase_rad"], normalized_progress=value["normalized_progress"],
            desired_pose_base=tuple(value["desired_pose_base"]),
            desired_twist_base=tuple(value["desired_twist_base"]),
            desired_acceleration_base=tuple(value["desired_acceleration_base"]),
            scalar_speed_m_s=value["scalar_speed_m_s"],
            signed_tangential_acceleration_m_s2=value["signed_tangential_acceleration_m_s2"],
            signed_planar_curvature_m_inv=value["signed_planar_curvature_m_inv"],
            degenerate_speed=value["degenerate_speed"],
            identity_receipt=PathIdentityReceiptV1.from_mapping(value["identity_receipt"]),
            schema=value["schema"], version=value["version"],
        )


class PathProviderV1:
    """Pure typed provider API for one frozen path geometry."""

    path_id: str
    duration_s: float
    identity_receipt: PathIdentityReceiptV1

    def sample(self, path_time_s: float) -> PathContextSampleV1:
        raise NotImplementedError


class _AnalyticPathProviderV1(PathProviderV1):
    def __init__(
        self,
        *,
        path_id: str,
        formula_id: str,
        duration_s: float,
        anchor_pose_base: Sequence[float] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        geometry_status: str = "offline_geometry_only",
        basis: PlanarBasisReceiptV1 | Mapping[str, Any] | None = None,
        basis_receipt: PlanarBasisReceiptV1 | Mapping[str, Any] | None = None,
        planar_basis: PlanarBasisReceiptV1 | Mapping[str, Any] | None = None,
    ) -> None:
        self.path_id = path_id
        self.duration_s = _finite(duration_s, "duration_s")
        if self.duration_s <= 0.0:
            raise PathContextError("R013 path duration must be positive")
        self.anchor_pose_base = _finite_tuple(anchor_pose_base, 6, "anchor_pose_base")
        supplied_basis = [item for item in (basis, basis_receipt, planar_basis) if item is not None]
        if len(supplied_basis) > 1:
            raise PathContextError("R013 path basis was supplied more than once")
        selected_basis: PlanarBasisReceiptV1 | Mapping[str, Any]
        selected_basis = supplied_basis[0] if supplied_basis else IDENTITY_PLANAR_BASIS
        if isinstance(selected_basis, Mapping):
            selected_basis = PlanarBasisReceiptV1.from_mapping(selected_basis)
        if not isinstance(selected_basis, PlanarBasisReceiptV1):
            raise PathContextError("R013 path basis receipt is not typed")
        self.basis_receipt = selected_basis
        self.identity_receipt = PathIdentityReceiptV1(
            path_id=path_id,
            formula_id=formula_id,
            duration_s=self.duration_s,
            anchor_pose_base=self.anchor_pose_base,
            basis_receipt=self.basis_receipt,
            geometry_status=geometry_status,
        )

    def _kinematics(self, path_time_s: float) -> tuple[float, float, float, float, float, float, float]:
        raise NotImplementedError

    def sample(self, path_time_s: float) -> PathContextSampleV1:
        time_s = _finite(path_time_s, "path_time_s")
        if not 0.0 <= time_s <= self.duration_s:
            raise PathContextError(
                f"R013 {self.path_id} path_time_s must be within [0,{self.duration_s}]"
            )
        phase, along, lateral, along_v, lateral_v, along_a, lateral_a = self._kinematics(time_s)
        speed = math.hypot(along_v, lateral_v)
        degenerate = speed <= DEGENERATE_SPEED_EPS_M_S
        if degenerate:
            tangent_accel = 0.0
            curvature = 0.0
        else:
            tangent_accel = (along_v * along_a + lateral_v * lateral_a) / speed
            curvature = (along_v * lateral_a - lateral_v * along_a) / (speed ** 3)
        along_base = self.basis_receipt.along_base
        lateral_base = self.basis_receipt.lateral_base
        displacement = tuple(
            along * along_base[index] + lateral * lateral_base[index]
            for index in range(3)
        )
        velocity_base = tuple(
            along_v * along_base[index] + lateral_v * lateral_base[index]
            for index in range(3)
        )
        acceleration_base = tuple(
            along_a * along_base[index] + lateral_a * lateral_base[index]
            for index in range(3)
        )
        x, y, z, rx, ry, rz = self.anchor_pose_base
        return PathContextSampleV1(
            path_id=self.path_id,
            path_time_s=time_s,
            phase_rad=phase,
            normalized_progress=time_s / self.duration_s,
            desired_pose_base=(
                x + displacement[0], y + displacement[1], z + displacement[2], rx, ry, rz
            ),
            desired_twist_base=(*velocity_base, 0.0, 0.0, 0.0),
            desired_acceleration_base=(*acceleration_base, 0.0, 0.0, 0.0),
            scalar_speed_m_s=speed,
            signed_tangential_acceleration_m_s2=tangent_accel,
            signed_planar_curvature_m_inv=curvature,
            degenerate_speed=degenerate,
            identity_receipt=self.identity_receipt,
        )


class CycloidPathProviderV1(_AnalyticPathProviderV1):
    def __init__(
        self,
        *,
        anchor_pose_base: Sequence[float] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        basis: PlanarBasisReceiptV1 | Mapping[str, Any] | None = None,
        basis_receipt: PlanarBasisReceiptV1 | Mapping[str, Any] | None = None,
        planar_basis: PlanarBasisReceiptV1 | Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            path_id=CYCLOID_PATH_ID,
            formula_id="cycloid_along_lateral_v1|A=0.015m|omega=0.1rad_s|duration=60s",
            duration_s=60.0,
            anchor_pose_base=anchor_pose_base,
            basis=basis,
            basis_receipt=basis_receipt,
            planar_basis=planar_basis,
        )

    def _kinematics(self, path_time_s: float) -> tuple[float, float, float, float, float, float, float]:
        theta = 0.1 * path_time_s
        amplitude = 0.015
        omega = 0.1
        return (
            theta,
            amplitude * (theta - math.sin(theta)),
            amplitude * (1.0 - math.cos(theta)),
            amplitude * omega * (1.0 - math.cos(theta)),
            amplitude * omega * math.sin(theta),
            amplitude * omega * omega * math.sin(theta),
            amplitude * omega * omega * math.cos(theta),
        )


class FigureEightPathProviderV1(_AnalyticPathProviderV1):
    def __init__(
        self,
        *,
        anchor_pose_base: Sequence[float] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        basis: PlanarBasisReceiptV1 | Mapping[str, Any] | None = None,
        basis_receipt: PlanarBasisReceiptV1 | Mapping[str, Any] | None = None,
        planar_basis: PlanarBasisReceiptV1 | Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            path_id=FIGURE8_PATH_ID,
            formula_id="figure8_along_lateral_v1|along=0.04sin(0.1t)|lateral=0.01sin(0.2t)|duration=60s",
            duration_s=FIGURE8_DURATION_S,
            anchor_pose_base=anchor_pose_base,
            geometry_status=FIGURE8_GEOMETRY_STATUS,
            basis=basis,
            basis_receipt=basis_receipt,
            planar_basis=planar_basis,
        )

    def _kinematics(self, path_time_s: float) -> tuple[float, float, float, float, float, float, float]:
        phase = FIGURE8_ALONG_OMEGA_RAD_S * path_time_s
        return (
            phase,
            FIGURE8_ALONG_AMPLITUDE_M * math.sin(FIGURE8_ALONG_OMEGA_RAD_S * path_time_s),
            FIGURE8_LATERAL_AMPLITUDE_M * math.sin(FIGURE8_LATERAL_OMEGA_RAD_S * path_time_s),
            FIGURE8_ALONG_AMPLITUDE_M * FIGURE8_ALONG_OMEGA_RAD_S * math.cos(FIGURE8_ALONG_OMEGA_RAD_S * path_time_s),
            FIGURE8_LATERAL_AMPLITUDE_M * FIGURE8_LATERAL_OMEGA_RAD_S * math.cos(FIGURE8_LATERAL_OMEGA_RAD_S * path_time_s),
            -FIGURE8_ALONG_AMPLITUDE_M * FIGURE8_ALONG_OMEGA_RAD_S ** 2 * math.sin(FIGURE8_ALONG_OMEGA_RAD_S * path_time_s),
            -FIGURE8_LATERAL_AMPLITUDE_M * FIGURE8_LATERAL_OMEGA_RAD_S ** 2 * math.sin(FIGURE8_LATERAL_OMEGA_RAD_S * path_time_s),
        )


__all__ = [
    "BASE_FRAME_ID", "CYCLOID_PATH_ID", "DEGENERATE_SPEED_EPS_M_S",
    "FIGURE8_ALONG_AMPLITUDE_M", "FIGURE8_ALONG_OMEGA_RAD_S", "FIGURE8_BIN_WIDTH_S",
    "FIGURE8_DURATION_S", "FIGURE8_FORMAL_BIN_COUNT", "FIGURE8_FORMAL_START_S",
    "FIGURE8_FULL_BIN_COUNT", "FIGURE8_LATERAL_AMPLITUDE_M", "FIGURE8_LATERAL_OMEGA_RAD_S",
    "FIGURE8_MAX_SPEED_M_S",
    "FIGURE8_GEOMETRY_STATUS", "FIGURE8_PATH_ID", "PATH_CONTEXT_SCHEMA",
    "PATH_CONTEXT_VERSION", "PATH_IDENTITY_SCHEMA", "PLANAR_BASIS_SCHEMA",
    "PLANAR_BASIS_VERSION", "SIGNED_CURVATURE_ORIENTATION", "IDENTITY_PLANAR_BASIS",
    "PlanarBasisReceiptV1", "CycloidPathProviderV1",
    "FigureEightPathProviderV1", "PathContextError", "PathContextSampleV1",
    "PathIdentityReceiptV1", "PathProviderV1",
]
