"""Typed Step5d return references and immutable four-segment route."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping, Sequence

from .batch import BatchIdentity, ReturnReferenceKind, return_reference_for_row
from .identity import canonical_sha256
from .physical_prior import PhysicalPriorArtifact


@dataclass(frozen=True)
class ReturnSegment:
    name: str
    target_xyz_m: tuple[float, float, float]
    target_rotvec_rad: tuple[float, float, float] | None
    acceleration_m_s2: float
    velocity_m_s: float
    preserve_orientation: bool = False


@dataclass(frozen=True)
class ReturnReference:
    kind: ReturnReferenceKind
    batch_uid: str
    row_index: int
    row_uid: str
    pose_xyz_m: tuple[float, float, float]
    pose_rotvec_rad: tuple[float, float, float]
    position_tolerance_m: float = 0.003
    orientation_tolerance_rad: float = 0.05
    still_speed_tolerance_m_s: float = 0.002

    @property
    def reference_uid(self) -> str:
        return canonical_sha256(
            {
                "schema": "ur-exp/return-reference-v1",
                "kind": self.kind.value,
                "batch_uid": self.batch_uid,
                "row_index": self.row_index,
                "row_uid": self.row_uid,
                "pose_xyz_m": list(self.pose_xyz_m),
                "pose_rotvec_rad": list(self.pose_rotvec_rad),
                "position_tolerance_m": self.position_tolerance_m,
                "orientation_tolerance_rad": self.orientation_tolerance_rad,
                "still_speed_tolerance_m_s": self.still_speed_tolerance_m_s,
            }
        )


@dataclass(frozen=True)
class ReturnTargetVerification:
    reference_uid: str
    pose_ok: bool
    orientation_ok: bool
    still_ok: bool
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
            and self.still_ok
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
    if len(near_ready_pose) != 6 or len(campaign_home_pose) != 6:
        raise ValueError("return poses must be xyz+rotvec")
    row = batch.rows[row_index - 1]
    kind = return_reference_for_row(row_index)
    pose = near_ready_pose if kind is ReturnReferenceKind.NEAR_READY else campaign_home_pose
    if not all(math.isfinite(float(value)) for value in pose):
        raise ValueError("return pose must be finite")
    return ReturnReference(
        kind=kind,
        batch_uid=batch.batch_uid,
        row_index=row_index,
        row_uid=canonical_sha256(
            {"batch_uid": batch.batch_uid, "row": row.to_dict()}
        ),
        pose_xyz_m=tuple(float(value) for value in pose[:3]),
        pose_rotvec_rad=tuple(float(value) for value in pose[3:]),
    )


def return_route(
    *,
    current_pose: Sequence[float],
    reference: ReturnReference,
    prior: PhysicalPriorArtifact,
) -> tuple[ReturnSegment, ...]:
    if len(current_pose) != 6 or not all(math.isfinite(float(v)) for v in current_pose):
        raise ValueError("current pose must be finite xyz+rotvec")
    safe_z = 0.033
    prior_x, prior_y, precontact_z = prior.precontact_xyz_m
    # Near-ready follows the fixed precontact route.  Final-home appends the
    # typed campaign-home target while preserving the same safe-Z transfer.
    segments = [
        ReturnSegment(
            "vertical_rise",
            (float(current_pose[0]), float(current_pose[1]), safe_z),
            None,
            0.060,
            0.040,
            preserve_orientation=True,
        ),
        ReturnSegment(
            "constant_z_to_precontact_xy_prior_orientation",
            (prior_x, prior_y, safe_z),
            prior.precontact_rotvec_rad,
            0.135,
            0.090,
        ),
        ReturnSegment(
            "vertical_descent_to_precontact",
            (prior_x, prior_y, precontact_z),
            prior.precontact_rotvec_rad,
            0.060,
            0.040,
        ),
    ]
    if reference.kind is ReturnReferenceKind.CAMPAIGN_HOME:
        segments.append(
            ReturnSegment(
                "final_campaign_home",
                reference.pose_xyz_m,
                reference.pose_rotvec_rad,
                0.060,
                0.040,
            )
        )
    else:
        segments.append(
            ReturnSegment(
                "verify_near_ready_then_wait_ack",
                reference.pose_xyz_m,
                reference.pose_rotvec_rad,
                0.060,
                0.040,
            )
        )
    return tuple(segments)
