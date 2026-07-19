"""Typed Step5d return references and immutable three-segment route."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import ClassVar, Mapping, Sequence, TypeAlias

from .batch import BatchIdentity, ReturnReferenceKind, return_reference_for_row
from .identity import canonical_sha256
from .physical_prior import PhysicalPriorArtifact


SAFE_TRANSFER_Z_M = 0.033


def _finite_vector(name: str, value: Sequence[float], length: int) -> tuple[float, ...]:
    if len(value) != length:
        raise ValueError(f"{name} must contain exactly {length} values")
    vector = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in vector):
        raise ValueError(f"{name} must be finite")
    return vector


@dataclass(frozen=True)
class ReturnSegment:
    name: str
    target_xyz_m: tuple[float, float, float]
    target_rotvec_rad: tuple[float, float, float] | None
    acceleration_m_s2: float
    velocity_m_s: float
    preserve_orientation: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("return segment name must be non-empty")
        _finite_vector("target_xyz_m", self.target_xyz_m, 3)
        if self.target_rotvec_rad is not None:
            _finite_vector("target_rotvec_rad", self.target_rotvec_rad, 3)
        if self.preserve_orientation != (self.target_rotvec_rad is None):
            raise ValueError(
                "preserve_orientation must be true exactly when rotvec is omitted"
            )
        for name in ("acceleration_m_s2", "velocity_m_s"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")

    def document(self) -> dict[str, object]:
        return {
            "name": self.name,
            "target_xyz_m": list(self.target_xyz_m),
            "target_rotvec_rad": (
                None
                if self.target_rotvec_rad is None
                else list(self.target_rotvec_rad)
            ),
            "acceleration_m_s2": self.acceleration_m_s2,
            "velocity_m_s": self.velocity_m_s,
            "preserve_orientation": self.preserve_orientation,
        }


@dataclass(frozen=True)
class _TypedReturnReference:
    batch_uid: str
    row_index: int
    row_uid: str
    pose_xyz_m: tuple[float, float, float]
    pose_rotvec_rad: tuple[float, float, float]
    position_tolerance_m: float = 0.003
    orientation_tolerance_rad: float = 0.05
    linear_speed_tolerance_m_s: float = 0.001
    angular_speed_tolerance_rad_s: float = 0.01
    qd_tolerance_rad_s: float = 0.01

    KIND: ClassVar[ReturnReferenceKind]

    def __post_init__(self) -> None:
        for name in ("batch_uid", "row_uid"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA256")
        if return_reference_for_row(self.row_index) is not self.KIND:
            raise ValueError("typed return reference differs from exact batch row")
        _finite_vector("pose_xyz_m", self.pose_xyz_m, 3)
        _finite_vector("pose_rotvec_rad", self.pose_rotvec_rad, 3)
        for name in (
            "position_tolerance_m",
            "orientation_tolerance_rad",
            "linear_speed_tolerance_m_s",
            "angular_speed_tolerance_rad_s",
            "qd_tolerance_rad_s",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")

    @property
    def kind(self) -> ReturnReferenceKind:
        return self.KIND

    def identity_document(self) -> dict[str, object]:
        return {
            "schema": "ur-exp/return-reference-v2",
            "kind": self.kind.value,
            "batch_uid": self.batch_uid,
            "row_index": self.row_index,
            "row_uid": self.row_uid,
            "pose_xyz_m": list(self.pose_xyz_m),
            "pose_rotvec_rad": list(self.pose_rotvec_rad),
            "position_tolerance_m": self.position_tolerance_m,
            "orientation_tolerance_rad": self.orientation_tolerance_rad,
            "linear_speed_tolerance_m_s": self.linear_speed_tolerance_m_s,
            "angular_speed_tolerance_rad_s": self.angular_speed_tolerance_rad_s,
            "qd_tolerance_rad_s": self.qd_tolerance_rad_s,
        }

    @property
    def reference_uid(self) -> str:
        return canonical_sha256(self.identity_document())


@dataclass(frozen=True)
class NearReadyReference(_TypedReturnReference):
    KIND: ClassVar[ReturnReferenceKind] = ReturnReferenceKind.NEAR_READY


@dataclass(frozen=True)
class CampaignHomeReference(_TypedReturnReference):
    KIND: ClassVar[ReturnReferenceKind] = ReturnReferenceKind.CAMPAIGN_HOME


ReturnReference: TypeAlias = NearReadyReference | CampaignHomeReference


@dataclass(frozen=True)
class ReturnRoute:
    reference_uid: str
    prior_fingerprint: str
    segments: tuple[ReturnSegment, ...]

    def __post_init__(self) -> None:
        if len(self.segments) != 3:
            raise ValueError("Step5d return route requires exactly three motion segments")
        if tuple(segment.name for segment in self.segments) != (
            "vertical_to_safe_transfer_z",
            "constant_z_transfer_to_reference_xy_orientation",
            "vertical_to_typed_reference",
        ):
            raise ValueError("Step5d return route segment order differs")

    @property
    def route_fingerprint(self) -> str:
        return canonical_sha256(
            {
                "schema": "ur-exp/step5d-return-route-v2",
                "reference_uid": self.reference_uid,
                "prior_fingerprint": self.prior_fingerprint,
                "segments": [segment.document() for segment in self.segments],
                "verification_is_separate_from_motion": True,
            }
        )


@dataclass(frozen=True)
class ReturnTargetVerification:
    reference_uid: str
    route_fingerprint: str
    pose_ok: bool
    orientation_ok: bool
    linear_still_ok: bool
    angular_still_ok: bool
    joint_still_ok: bool
    safety_guards: Mapping[str, bool]
    tp_controller_identity_ok: bool

    @property
    def verified(self) -> bool:
        required = {
            "force",
            "torque",
            "joints",
            "sensor_freshness",
            "heartbeat",
            "contact_loss",
            "route_workspace",
        }
        return bool(
            self.pose_ok
            and self.orientation_ok
            and self.linear_still_ok
            and self.angular_still_ok
            and self.joint_still_ok
            and self.tp_controller_identity_ok
            and set(self.safety_guards) == required
            and all(self.safety_guards.values())
        )


def return_reference(
    batch: BatchIdentity,
    row_index: int,
    *,
    near_ready_pose: Sequence[float],
    campaign_home_pose: Sequence[float],
) -> ReturnReference:
    near = _finite_vector("near_ready_pose", near_ready_pose, 6)
    home = _finite_vector("campaign_home_pose", campaign_home_pose, 6)
    row = batch.rows[row_index - 1]
    kind = return_reference_for_row(row_index)
    pose = near if kind is ReturnReferenceKind.NEAR_READY else home
    values = {
        "batch_uid": batch.batch_uid,
        "row_index": row_index,
        "row_uid": canonical_sha256(
            {"batch_uid": batch.batch_uid, "row": row.to_dict()}
        ),
        "pose_xyz_m": pose[:3],
        "pose_rotvec_rad": pose[3:],
    }
    if kind is ReturnReferenceKind.NEAR_READY:
        return NearReadyReference(**values)
    return CampaignHomeReference(**values)


def return_route(
    *,
    current_pose: Sequence[float],
    reference: ReturnReference,
    prior: PhysicalPriorArtifact,
) -> ReturnRoute:
    current = _finite_vector("current_pose", current_pose, 6)
    if not isinstance(reference, (NearReadyReference, CampaignHomeReference)):
        raise TypeError("reference must be a sealed Step5d return reference")
    if isinstance(reference, NearReadyReference) and (
        reference.pose_xyz_m != prior.precontact_xyz_m
        or reference.pose_rotvec_rad != prior.precontact_rotvec_rad
    ):
        raise ValueError("near-ready reference must bind the exact physical prior")
    target_xyz = reference.pose_xyz_m
    target_rotvec = reference.pose_rotvec_rad
    segments = (
        ReturnSegment(
            "vertical_to_safe_transfer_z",
            (current[0], current[1], SAFE_TRANSFER_Z_M),
            None,
            0.060,
            0.040,
            preserve_orientation=True,
        ),
        ReturnSegment(
            "constant_z_transfer_to_reference_xy_orientation",
            (target_xyz[0], target_xyz[1], SAFE_TRANSFER_Z_M),
            target_rotvec,
            0.135,
            0.090,
        ),
        ReturnSegment(
            "vertical_to_typed_reference",
            target_xyz,
            target_rotvec,
            0.060,
            0.040,
        ),
    )
    return ReturnRoute(reference.reference_uid, prior.fingerprint, segments)
