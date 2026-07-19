"""Typed Step5d return references and immutable three-segment route."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import ClassVar, Mapping, Sequence, TypeAlias

from .batch import BatchIdentity, ReturnReferenceKind, return_reference_for_row
from .identity import canonical_sha256
from .physical_prior import PhysicalPriorArtifact


SAFE_TRANSFER_Z_M = 0.033
RETURN_SEGMENT_VERTICAL_ACCELERATION_M_S2 = 0.060
RETURN_SEGMENT_VERTICAL_SPEED_M_S = 0.040
RETURN_SEGMENT_HORIZONTAL_ACCELERATION_M_S2 = 0.135
RETURN_SEGMENT_HORIZONTAL_SPEED_M_S = 0.090
RETURN_ANGULAR_SPEED_LIMIT_RAD_S = 0.050
RETURN_ANGULAR_ACCELERATION_LIMIT_RAD_S2 = 0.100
RETURN_ANGULAR_SPEED_GUARD_RAD_S = 0.060
RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2 = 0.500
RETURN_ANGULAR_STOP_DECELERATION_RAD_S2 = 0.100
RETURN_ORIENTATION_ADMISSION_LIMIT_RAD = math.radians(20.0)
RETURN_CONTROLLER_PERIOD_S = 0.002
RETURN_CONTROLLER_MAX_SAMPLE_GAP_S = 0.004
URSIM_RETURN_TRACE_SCHEMA = "step5d.autotune-v3/ursim-return-trace-v1"
RETURN_TELEMETRY_SCHEMA = "step5d.autotune-v3/return-route-telemetry-v2"
RETURN_ROUTE_EVIDENCE_SCHEMA = "step5d.autotune-v3/return-route-evidence-v2"


def _sha256_value(name: str, value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA256")
    return value


def _finite_number(name: str, value: object, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{name} is outside its finite range")
    return result


def _exact_mapping(value: object, fields: set[str], role: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{role} fields differ")
    return value


def validate_motion_capable_ursim_return_trace(
    payload: object,
    *,
    expected_control_fingerprint: str,
    expected_orchestration_fingerprint: str,
    expected_source_binding_sha256: str,
    expected_triplet_sha256: Mapping[str, str],
) -> Mapping[str, object]:
    """Validate isolated URSim motion without granting a live-plant claim."""

    trace = _exact_mapping(
        payload,
        {
            "schema",
            "status",
            "claim_boundary",
            "identity",
            "source_binding_sha256",
            "triplet_sha256",
            "image",
            "network",
            "route",
            "samples",
            "summary",
            "cleanup",
        },
        "URSim return trace",
    )
    if trace["schema"] != URSIM_RETURN_TRACE_SCHEMA or trace["status"] != "pass":
        raise ValueError("URSim return trace is not a passed v1 artifact")
    if trace["claim_boundary"] != "isolated_ursim_motion_only_not_live_certification":
        raise ValueError("URSim return trace claim boundary differs")
    identity = _exact_mapping(
        trace["identity"],
        {"control_fingerprint", "orchestration_fingerprint"},
        "URSim return identity",
    )
    if identity != {
        "control_fingerprint": expected_control_fingerprint,
        "orchestration_fingerprint": expected_orchestration_fingerprint,
    }:
        raise ValueError("URSim return trace identity differs")
    if trace["source_binding_sha256"] != _sha256_value(
        "expected_source_binding_sha256", expected_source_binding_sha256
    ):
        raise ValueError("URSim return trace source binding differs")
    triplet = _exact_mapping(
        trace["triplet_sha256"], {".script", ".txt", ".urp"}, "URSim triplet"
    )
    if dict(triplet) != dict(expected_triplet_sha256):
        raise ValueError("URSim return trace TP triplet differs")
    image = _exact_mapping(
        trace["image"],
        {"reference", "image_id", "polyscope_version", "robot_model"},
        "URSim image",
    )
    if (
        not isinstance(image["reference"], str)
        or "@sha256:" not in image["reference"]
        or image["robot_model"] != "UR10"
    ):
        raise ValueError("URSim return trace image is not digest-bound UR10e simulation")
    network = _exact_mapping(
        trace["network"],
        {"internal", "host_ports_published", "real_robot_network_connected"},
        "URSim network",
    )
    if network != {
        "internal": True,
        "host_ports_published": False,
        "real_robot_network_connected": False,
    }:
        raise ValueError("URSim return trace was not isolated")
    route = _exact_mapping(
        trace["route"],
        {
            "controller_function",
            "controller_function_sha256",
            "segment_order",
            "vertical_transfer_vertical",
            "relative_test_motion_only",
            "no_contact",
        },
        "URSim return route",
    )
    if (
        route["controller_function"] != "codex_autotune_bounded_return_segment"
        or route["segment_order"] != [1, 2, 3]
        or route["vertical_transfer_vertical"] is not True
        or route["relative_test_motion_only"] is not True
        or route["no_contact"] is not True
    ):
        raise ValueError("URSim return route contract differs")
    _sha256_value("controller_function_sha256", route["controller_function_sha256"])
    samples = trace["samples"]
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)) or len(samples) < 20:
        raise ValueError("URSim return trace sample count is insufficient")
    last_timestamp = -1.0
    observed_segments: list[int] = []
    for row in samples:
        sample = _exact_mapping(
            row,
            {
                "controller_timestamp_s",
                "actual_tcp_pose",
                "actual_tcp_speed",
                "actual_qd",
                "return_phase_echo",
                "return_segment_id",
                "completion_state",
                "safety_mode",
            },
            "URSim return sample",
        )
        timestamp = _finite_number("URSim controller timestamp", sample["controller_timestamp_s"])
        if timestamp <= last_timestamp:
            raise ValueError("URSim controller timestamps are not strictly increasing")
        last_timestamp = timestamp
        _finite_vector("URSim actual_tcp_pose", sample["actual_tcp_pose"], 6)
        _finite_vector("URSim actual_tcp_speed", sample["actual_tcp_speed"], 6)
        _finite_vector("URSim actual_qd", sample["actual_qd"], 6)
        segment_value = sample["return_segment_id"]
        if isinstance(segment_value, bool) or not isinstance(segment_value, int):
            raise ValueError("URSim return segment must be an integer")
        if segment_value in (1, 2, 3) and (
            not observed_segments or observed_segments[-1] != segment_value
        ):
            observed_segments.append(segment_value)
        if sample["safety_mode"] != 1:
            raise ValueError("URSim return trace left NORMAL safety mode")
    summary = _exact_mapping(
        trace["summary"],
        {
            "sample_count",
            "motion_observed",
            "max_position_excursion_m",
            "max_angular_excursion_rad",
            "max_angular_speed_rad_s",
            "max_angular_acceleration_rad_s2",
            "max_abs_qd_rad_s",
            "segment_order_observed",
            "completion_state",
            "forbidden_action_count",
        },
        "URSim return summary",
    )
    if (
        summary["sample_count"] != len(samples)
        or summary["motion_observed"] is not True
        or summary["segment_order_observed"] != [1, 2, 3]
        or observed_segments != [1, 2, 3]
        or summary["completion_state"] != 104
        or summary["forbidden_action_count"] != 0
        or _finite_number("URSim position excursion", summary["max_position_excursion_m"])
        <= 0.001
        or _finite_number("URSim angular speed", summary["max_angular_speed_rad_s"])
        > RETURN_ANGULAR_SPEED_GUARD_RAD_S
        or _finite_number(
            "URSim angular acceleration", summary["max_angular_acceleration_rad_s2"]
        )
        > RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2
    ):
        raise ValueError("URSim return trace summary does not prove bounded motion")
    cleanup = _exact_mapping(
        trace["cleanup"],
        {"container_removed", "network_removed", "program_stopped"},
        "URSim cleanup",
    )
    if not all(value is True for value in cleanup.values()):
        raise ValueError("URSim return trace cleanup is incomplete")
    return trace


def analyze_source_exact_return_telemetry(
    payload: object,
    *,
    expected_release_basis_fingerprint: str,
    expected_deployment_fingerprint: str,
    expected_source_binding_sha256: str,
    expected_triplet_sha256: Mapping[str, str],
    expected_plant_epoch: int,
    expected_deployment_readback_sha256: str,
    expected_certification_authorization_sha256: str,
) -> Mapping[str, object]:
    """Validate all retained attended return samples and final readback."""

    telemetry = _exact_mapping(
        payload,
        {
            "schema",
            "candidate_stage_id",
            "release_basis_fingerprint",
            "deployment_fingerprint",
            "source_binding_sha256",
            "triplet_sha256",
            "plant_epoch",
            "deployment_readback_sha256",
            "certification_authorization_sha256",
            "all_samples_retained",
            "no_contact",
            "samples",
            "final_readback",
        },
        "return telemetry",
    )
    expected = {
        "schema": RETURN_TELEMETRY_SCHEMA,
        "candidate_stage_id": "step5d_strict_rnn_autotune_v3",
        "release_basis_fingerprint": expected_release_basis_fingerprint,
        "deployment_fingerprint": expected_deployment_fingerprint,
        "source_binding_sha256": expected_source_binding_sha256,
        "triplet_sha256": dict(expected_triplet_sha256),
        "plant_epoch": expected_plant_epoch,
        "deployment_readback_sha256": expected_deployment_readback_sha256,
        "certification_authorization_sha256": expected_certification_authorization_sha256,
        "all_samples_retained": True,
        "no_contact": True,
    }
    for name, value in expected.items():
        if telemetry[name] != value:
            raise ValueError(f"return telemetry {name} differs")
    samples = telemetry["samples"]
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)) or len(samples) < 20:
        raise ValueError("return telemetry sample count is insufficient")
    last_timestamp = -1.0
    observed_segments: list[int] = []
    maxima = {"angular_speed_rad_s": 0.0, "angular_acceleration_rad_s2": 0.0, "sample_gap_s": 0.0}
    for row in samples:
        sample = _exact_mapping(
            row,
            {
                "controller_timestamp_s",
                "return_phase_echo",
                "return_segment_id",
                "angular_speed_rad_s",
                "angular_acceleration_rad_s2",
                "max_angular_speed_rad_s",
                "max_angular_acceleration_rad_s2",
                "max_sample_gap_s",
                "guard_reason",
                "safety_mode",
            },
            "return telemetry sample",
        )
        timestamp = _finite_number("return controller timestamp", sample["controller_timestamp_s"])
        if timestamp <= last_timestamp:
            raise ValueError("return telemetry timestamps are not strictly increasing")
        last_timestamp = timestamp
        if sample["safety_mode"] != "NORMAL" or sample["guard_reason"] != 0:
            raise ValueError("return telemetry reports a guard or safety failure")
        segment = sample["return_segment_id"]
        if isinstance(segment, bool) or not isinstance(segment, int) or segment not in (1, 2, 3):
            raise ValueError("return telemetry segment identity differs")
        if not observed_segments or observed_segments[-1] != segment:
            observed_segments.append(segment)
        speed = _finite_number("return angular speed", sample["angular_speed_rad_s"])
        acceleration = _finite_number(
            "return angular acceleration", sample["angular_acceleration_rad_s2"]
        )
        gap = _finite_number("return sample gap", sample["max_sample_gap_s"])
        if gap <= 0.0:
            raise ValueError("return telemetry sample gap must be positive")
        maxima["angular_speed_rad_s"] = max(maxima["angular_speed_rad_s"], speed)
        maxima["angular_acceleration_rad_s2"] = max(
            maxima["angular_acceleration_rad_s2"], acceleration
        )
        maxima["sample_gap_s"] = max(maxima["sample_gap_s"], gap)
        if (
            speed > RETURN_ANGULAR_SPEED_GUARD_RAD_S
            or acceleration > RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2
            or _finite_number("return max angular speed", sample["max_angular_speed_rad_s"])
            > RETURN_ANGULAR_SPEED_GUARD_RAD_S
            or _finite_number(
                "return max angular acceleration",
                sample["max_angular_acceleration_rad_s2"],
            )
            > RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2
            or gap > RETURN_CONTROLLER_MAX_SAMPLE_GAP_S
        ):
            raise ValueError("return telemetry exceeds the frozen envelope")
    if observed_segments != [1, 2, 3]:
        raise ValueError("return telemetry does not contain the exact three-segment route")
    final = _exact_mapping(
        telemetry["final_readback"],
        {
            "completed",
            "return_phase_echo",
            "return_segment_id",
            "return_guard_mask",
            "max_angular_speed_rad_s",
            "max_angular_acceleration_rad_s2",
            "max_sample_gap_s",
            "safety_mode",
        },
        "return final readback",
    )
    if (
        final["completed"] is not True
        or final["return_phase_echo"] != 40.3
        or final["return_segment_id"] != 3
        or final["return_guard_mask"] != 0x7F
        or final["safety_mode"] != "NORMAL"
        or _finite_number("final max angular speed", final["max_angular_speed_rad_s"])
        > RETURN_ANGULAR_SPEED_GUARD_RAD_S
        or _finite_number(
            "final max angular acceleration", final["max_angular_acceleration_rad_s2"]
        )
        > RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2
        or _finite_number("final max sample gap", final["max_sample_gap_s"])
        > RETURN_CONTROLLER_MAX_SAMPLE_GAP_S
    ):
        raise ValueError("return final readback is not certified")
    return {
        "sample_count": len(samples),
        "segment_order": observed_segments,
        "maxima": maxima,
        "controller_readback_sha256": canonical_sha256(final),
    }


def return_policy_fingerprint(
    *,
    near_ready_pose: Sequence[float],
    campaign_home_pose: Sequence[float],
    prior: PhysicalPriorArtifact,
) -> str:
    near = _finite_vector("near_ready_pose", near_ready_pose, 6)
    home = _finite_vector("campaign_home_pose", campaign_home_pose, 6)
    return canonical_sha256(
        {
            "schema": "ur-exp/step5d-return-policy/v4",
            "prior_fingerprint": prior.fingerprint,
            "near_ready_pose": list(near),
            "campaign_home_pose": list(home),
            "selection": {"rows_1_to_9": "near_ready", "row_10": "campaign_home"},
            "safe_transfer_z_m": SAFE_TRANSFER_Z_M,
            "angular_envelope": {
                "controller": "speedl_bounded_twist_v1",
                "speed_limit_rad_s": RETURN_ANGULAR_SPEED_LIMIT_RAD_S,
                "acceleration_limit_rad_s2": (
                    RETURN_ANGULAR_ACCELERATION_LIMIT_RAD_S2
                ),
                "observed_speed_guard_rad_s": RETURN_ANGULAR_SPEED_GUARD_RAD_S,
                "observed_acceleration_guard_rad_s2": (
                    RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2
                ),
                "stop_deceleration_rad_s2": (
                    RETURN_ANGULAR_STOP_DECELERATION_RAD_S2
                ),
                "orientation_admission_limit_rad": (
                    RETURN_ORIENTATION_ADMISSION_LIMIT_RAD
                ),
                "controller_period_s": RETURN_CONTROLLER_PERIOD_S,
                "max_sample_gap_s": RETURN_CONTROLLER_MAX_SAMPLE_GAP_S,
                "segment_boundary_stillness_required": True,
                "continuous_actual_speed_guard_required": True,
            },
            "segments": [
                {"name": "vertical_up", "a_m_s2": 0.060, "v_m_s": 0.040},
                {"name": "constant_z_transfer", "a_m_s2": 0.135, "v_m_s": 0.090},
                {"name": "vertical_down", "a_m_s2": 0.060, "v_m_s": 0.040},
            ],
            "wait_ack_after_typed_safe_closure": True,
        }
    )


def _finite_vector(name: str, value: Sequence[float], length: int) -> tuple[float, ...]:
    if len(value) != length:
        raise ValueError(f"{name} must contain exactly {length} values")
    vector = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in vector):
        raise ValueError(f"{name} must be finite")
    return vector


def _rotvec_matrix(rotvec_rad: Sequence[float]) -> tuple[tuple[float, ...], ...]:
    rx, ry, rz = _finite_vector("rotvec_rad", rotvec_rad, 3)
    angle = math.sqrt(rx * rx + ry * ry + rz * rz)
    if angle <= 1e-15:
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    x, y, z = rx / angle, ry / angle, rz / angle
    sine = math.sin(angle)
    one_minus_cosine = 1.0 - math.cos(angle)
    return (
        (
            1.0 - one_minus_cosine * (y * y + z * z),
            one_minus_cosine * x * y - sine * z,
            one_minus_cosine * x * z + sine * y,
        ),
        (
            one_minus_cosine * x * y + sine * z,
            1.0 - one_minus_cosine * (x * x + z * z),
            one_minus_cosine * y * z - sine * x,
        ),
        (
            one_minus_cosine * x * z - sine * y,
            one_minus_cosine * y * z + sine * x,
            1.0 - one_minus_cosine * (x * x + y * y),
        ),
    )


def return_orientation_distance_rad(
    current_rotvec_rad: Sequence[float], target_rotvec_rad: Sequence[float]
) -> float:
    """Shortest SO(3) distance used by pre-motion return admission."""

    current = _rotvec_matrix(current_rotvec_rad)
    target = _rotvec_matrix(target_rotvec_rad)
    relative_trace = sum(
        target[row][column] * current[row][column]
        for row in range(3)
        for column in range(3)
    )
    cosine = min(1.0, max(-1.0, 0.5 * (relative_trace - 1.0)))
    return math.acos(cosine)


@dataclass(frozen=True)
class ReturnSegment:
    name: str
    target_xyz_m: tuple[float, float, float]
    target_rotvec_rad: tuple[float, float, float] | None
    acceleration_m_s2: float
    velocity_m_s: float
    angular_speed_limit_rad_s: float = RETURN_ANGULAR_SPEED_LIMIT_RAD_S
    angular_acceleration_limit_rad_s2: float = (
        RETURN_ANGULAR_ACCELERATION_LIMIT_RAD_S2
    )
    angular_speed_guard_rad_s: float = RETURN_ANGULAR_SPEED_GUARD_RAD_S
    angular_acceleration_guard_rad_s2: float = (
        RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2
    )
    angular_stop_deceleration_rad_s2: float = (
        RETURN_ANGULAR_STOP_DECELERATION_RAD_S2
    )
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
        for name in (
            "acceleration_m_s2",
            "velocity_m_s",
            "angular_speed_limit_rad_s",
            "angular_acceleration_limit_rad_s2",
            "angular_speed_guard_rad_s",
            "angular_acceleration_guard_rad_s2",
            "angular_stop_deceleration_rad_s2",
        ):
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
            "angular_speed_limit_rad_s": self.angular_speed_limit_rad_s,
            "angular_acceleration_limit_rad_s2": (
                self.angular_acceleration_limit_rad_s2
            ),
            "angular_speed_guard_rad_s": self.angular_speed_guard_rad_s,
            "angular_acceleration_guard_rad_s2": (
                self.angular_acceleration_guard_rad_s2
            ),
            "angular_stop_deceleration_rad_s2": (
                self.angular_stop_deceleration_rad_s2
            ),
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
                "schema": "ur-exp/step5d-return-route-v3",
                "reference_uid": self.reference_uid,
                "prior_fingerprint": self.prior_fingerprint,
                "segments": [segment.document() for segment in self.segments],
                "verification_is_separate_from_motion": True,
                "controller": "speedl_bounded_twist_v1",
                "orientation_admission_limit_rad": (
                    RETURN_ORIENTATION_ADMISSION_LIMIT_RAD
                ),
                "controller_period_s": RETURN_CONTROLLER_PERIOD_S,
                "max_sample_gap_s": RETURN_CONTROLLER_MAX_SAMPLE_GAP_S,
                "segment_boundary_stillness_required": True,
                "continuous_actual_speed_guard_required": True,
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
    orientation_distance_rad = return_orientation_distance_rad(
        current[3:], target_rotvec
    )
    if orientation_distance_rad > RETURN_ORIENTATION_ADMISSION_LIMIT_RAD:
        raise ValueError(
            "return target exceeds the certified orientation admission domain"
        )
    segments = (
        ReturnSegment(
            "vertical_to_safe_transfer_z",
            (current[0], current[1], SAFE_TRANSFER_Z_M),
            None,
            RETURN_SEGMENT_VERTICAL_ACCELERATION_M_S2,
            RETURN_SEGMENT_VERTICAL_SPEED_M_S,
            preserve_orientation=True,
        ),
        ReturnSegment(
            "constant_z_transfer_to_reference_xy_orientation",
            (target_xyz[0], target_xyz[1], SAFE_TRANSFER_Z_M),
            target_rotvec,
            RETURN_SEGMENT_HORIZONTAL_ACCELERATION_M_S2,
            RETURN_SEGMENT_HORIZONTAL_SPEED_M_S,
        ),
        ReturnSegment(
            "vertical_to_typed_reference",
            target_xyz,
            target_rotvec,
            RETURN_SEGMENT_VERTICAL_ACCELERATION_M_S2,
            RETURN_SEGMENT_VERTICAL_SPEED_M_S,
        ),
    )
    return ReturnRoute(reference.reference_uid, prior.fingerprint, segments)
