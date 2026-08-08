"""Offline primitives for the formal V4 contact-acquisition handoff.

The acquisition phase is intentionally independent from Direct Torque.  The
host owns the Kunwei KWR75 raw-TCP latch and all force/contact/guard decisions;
the generated URScript only applies bounded base -Z velocity and reports
identity, kinematic, and state information.  A Direct Torque transition can be
constructed only from a successfully proven stationary handoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import re
from typing import Any, Mapping, Sequence


FORMAL_CONTACT_ACQUISITION_SCHEMA_V2 = (
    "ur10e_tacdiffusion_contact_acquisition/v2"
)
FORMAL_ROUTE_IDENTITY = "remote_secondary_client_direct_torque"
KUNWEI_RAW_AUTHORITY_IDENTITY = "kunwei_kwr75_tcp_raw_stream_v1"
ACQUISITION_ROUTE_TOKEN = 4_004_001
ACQUISITION_CONTROL_PERIOD_S = 0.002
ACQUISITION_SPEED_M_S = 0.0005
ACQUISITION_MAX_DISTANCE_M = 0.025
ACQUISITION_ACCELERATION_M_S2 = 0.010
ACQUISITION_DECELERATION_M_S2 = 0.010
ACQUISITION_SENSOR_DELIVERY_WATCHDOG_S = 0.080
ACQUISITION_HEARTBEAT_TIMEOUT_TICKS = 40
ACQUISITION_PREPARE_TIMEOUT_TICKS = 200
ACQUISITION_COMMAND_PREPARE = 0
ACQUISITION_COMMAND_START = 1
ACQUISITION_COMMAND_ABORT = 2
ACQUISITION_COMMAND_STOP_NO_CONTACT = 3
ACQUISITION_FORCE_GUARD_N = 50.0
ACQUISITION_TORQUE_GUARD_NM = 4.0
ACQUISITION_LATCH_LOAD_N = 1.0
ACQUISITION_LATCH_SAMPLES = 50
ACQUISITION_STATIONARY_DWELL_S = 0.100
ACQUISITION_STATIONARY_TCP_SPEED_LIMIT_M_S = 0.0001
ACQUISITION_STATIONARY_ROTATION_SPEED_LIMIT_RAD_S = 0.002
ACQUISITION_STATIONARY_JOINT_SPEED_LIMIT_RAD_S = 0.001
MAX_HANDOFF_MISMATCH_M = 0.0003
ACQUISITION_BRAKING_DISTANCE_M = (
    ACQUISITION_SPEED_M_S**2 / (2.0 * ACQUISITION_DECELERATION_M_S2)
)
ACQUISITION_DISCRETE_STOP_BOUND_M = (
    ACQUISITION_SPEED_M_S * ACQUISITION_CONTROL_PERIOD_S
)
ACQUISITION_DECELERATION_START_DISTANCE_M = (
    ACQUISITION_MAX_DISTANCE_M
    - ACQUISITION_BRAKING_DISTANCE_M
    - ACQUISITION_DISCRETE_STOP_BOUND_M
)
FIXED_TRACKING_STIFFNESS = (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
ZERO6 = (0.0,) * 6


class AcquisitionState(str, Enum):
    IDLE = "IDLE"
    VELOCITY_SEARCH = "VELOCITY_SEARCH"
    STOPPING = "STOPPING"
    STATIONARY_DWELL = "STATIONARY_DWELL"
    HANDOFF_READY = "HANDOFF_READY"
    SEARCH_EXHAUSTED = "SEARCH_EXHAUSTED"
    FAULT = "FAULT"


class AcquisitionError(RuntimeError):
    """A fail-closed acquisition or handoff violation."""


@dataclass
class AcquisitionHeartbeatV1:
    """Strict 2 ms host packet sequence/heartbeat semantics.

    A held packet is permitted only when every command/identity/latch field
    is unchanged.  A gap, replay, torn sequence read, or the 40th held tick
    is a fail-closed protocol fault.  The native Kunwei sample-order latch is
    deliberately separate from this host packet delivery watchdog.
    """

    control_period_s: float = ACQUISITION_CONTROL_PERIOD_S
    timeout_ticks: int = ACQUISITION_HEARTBEAT_TIMEOUT_TICKS
    last_sequence: int = 0
    held_ticks: int = 0
    last_signature: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        if not math.isclose(
            float(self.control_period_s),
            ACQUISITION_CONTROL_PERIOD_S,
            rel_tol=0.0,
            abs_tol=1.0e-12,
        ):
            raise ValueError("formal acquisition heartbeat control period is frozen at 2 ms")
        self.control_period_s = ACQUISITION_CONTROL_PERIOD_S
        if int(self.timeout_ticks) != ACQUISITION_HEARTBEAT_TIMEOUT_TICKS:
            raise ValueError("formal acquisition heartbeat timeout is frozen at 40 ticks")
        self.timeout_ticks = ACQUISITION_HEARTBEAT_TIMEOUT_TICKS

    @property
    def timeout_s(self) -> float:
        return self.timeout_ticks * self.control_period_s

    def observe(self, sequence: int, signature: Sequence[int]) -> str:
        current_sequence = int(sequence)
        current_signature = tuple(int(value) for value in signature)
        if current_sequence < 0 or not current_signature:
            raise AcquisitionError("acquisition_sequence_replay")
        is_new = current_sequence == self.last_sequence + 1
        is_held = current_sequence == self.last_sequence
        if self.last_signature is None:
            if not is_new:
                raise AcquisitionError("acquisition_sequence_gap_or_replay")
            self.last_sequence = current_sequence
            self.last_signature = current_signature
            self.held_ticks = 0
            return "NEW"
        if is_new:
            self.last_sequence = current_sequence
            self.last_signature = current_signature
            self.held_ticks = 0
            return "NEW"
        if not is_held:
            raise AcquisitionError("acquisition_sequence_gap_or_replay")
        if current_signature != self.last_signature:
            raise AcquisitionError("acquisition_sequence_replay_payload_changed")
        self.held_ticks += 1
        if self.held_ticks >= self.timeout_ticks:
            raise AcquisitionError("acquisition_heartbeat_timeout")
        return "HELD"


def _finite_vector(value: Sequence[float], length: int, name: str) -> tuple[float, ...]:
    result = tuple(float(item) for item in value)
    if len(result) != length or not all(math.isfinite(item) for item in result):
        raise ValueError(f"{name} must contain {length} finite values")
    return result


def _contract_value(contract: object, name: str, default: float) -> float:
    value = float(getattr(contract, name, default))
    if not math.isfinite(value):
        raise ValueError(f"contact acquisition {name} must be finite")
    return value


def _contract_int(contract: object, name: str, default: int) -> int:
    value = int(getattr(contract, name, default))
    if value <= 0:
        raise ValueError(f"contact acquisition {name} must be positive")
    return value


def _norm(values: Sequence[float]) -> float:
    return math.sqrt(sum(float(item) ** 2 for item in values))


def _smooth01(value: float) -> float:
    bounded = min(1.0, max(0.0, float(value)))
    return 10.0 * bounded**3 - 15.0 * bounded**4 + 6.0 * bounded**5


@dataclass(frozen=True)
class KunweiAcquisitionSample:
    """One native Kunwei sample with latest-batch delivery age.

    ``host_age_s`` is the age of the received native batch at host delivery;
    it is not a claim that every native frame arrived separately at the host.
    Native sample order and the 50-frame latch remain independent invariants.
    """

    sample_index: int
    host_age_s: float
    normal_load_n: float
    force_norm_n: float
    torque_norm_nm: float
    route_identity: str
    lease_id: int
    episode_identity: int
    safety_mode: str
    robot_mode: str
    protective_stop: bool = False
    joint_fault: bool = False

    @classmethod
    def from_value(cls, value: "KunweiAcquisitionSample | Mapping[str, Any]") -> "KunweiAcquisitionSample":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("Kunwei acquisition sample must be a typed sample or mapping")
        try:
            return cls(
                sample_index=int(value["sample_index"]),
                host_age_s=float(value["host_age_s"]),
                normal_load_n=float(value["normal_load_n"]),
                force_norm_n=float(value["force_norm_n"]),
                torque_norm_nm=float(value["torque_norm_nm"]),
                route_identity=str(value["route_identity"]),
                lease_id=int(value["lease_id"]),
                episode_identity=int(value["episode_identity"]),
                safety_mode=str(value["safety_mode"]),
                robot_mode=str(value["robot_mode"]),
                protective_stop=bool(value.get("protective_stop", False)),
                joint_fault=bool(value.get("joint_fault", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AcquisitionError("kunwei_acquisition_sample_malformed") from exc


@dataclass(frozen=True)
class StationaryPoseSample:
    """Fresh RTDE kinematic evidence used only to prove the handoff dwell."""

    sample_time_s: float
    host_age_s: float
    actual_pose_base: tuple[float, ...] | Sequence[float]
    tcp_speed_base: tuple[float, ...] | Sequence[float]
    joint_speed_rad_s: tuple[float, ...] | Sequence[float]
    route_identity: str
    lease_id: int
    episode_identity: int
    safety_mode: str
    robot_mode: str
    protective_stop: bool = False
    joint_fault: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "actual_pose_base",
            _finite_vector(self.actual_pose_base, 6, "actual_pose_base"),
        )
        object.__setattr__(
            self,
            "tcp_speed_base",
            _finite_vector(self.tcp_speed_base, 6, "tcp_speed_base"),
        )
        object.__setattr__(
            self,
            "joint_speed_rad_s",
            _finite_vector(self.joint_speed_rad_s, 6, "joint_speed_rad_s"),
        )

    @classmethod
    def from_value(cls, value: "StationaryPoseSample | Mapping[str, Any]") -> "StationaryPoseSample":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("stationary sample must be a typed sample or mapping")
        try:
            return cls(
                sample_time_s=float(value["sample_time_s"]),
                host_age_s=float(value["host_age_s"]),
                actual_pose_base=value["actual_pose_base"],
                tcp_speed_base=value["tcp_speed_base"],
                joint_speed_rad_s=value["joint_speed_rad_s"],
                route_identity=str(value["route_identity"]),
                lease_id=int(value["lease_id"]),
                episode_identity=int(value["episode_identity"]),
                safety_mode=str(value["safety_mode"]),
                robot_mode=str(value["robot_mode"]),
                protective_stop=bool(value.get("protective_stop", False)),
                joint_fault=bool(value.get("joint_fault", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AcquisitionError("stationary_pose_sample_malformed") from exc


@dataclass(frozen=True)
class AcquisitionCommand:
    state: AcquisitionState
    velocity_base: tuple[float, ...]
    distance_m: float
    stop_velocity_control: bool
    host_latch_authority: str = KUNWEI_RAW_AUTHORITY_IDENTITY


@dataclass(frozen=True)
class AcquisitionHandoffV1:
    """A fresh, stationary, identity-bound anchor for Direct Torque."""

    route_identity: str
    lease_id: int
    episode_identity: int
    contact_latch_sample_index: int
    stationary_dwell_s: float
    anchor_pose_base: tuple[float, ...]
    acquisition_training: bool = False
    force_authority: str = KUNWEI_RAW_AUTHORITY_IDENTITY
    schema_version: str = FORMAL_CONTACT_ACQUISITION_SCHEMA_V2

    def __post_init__(self) -> None:
        if self.schema_version != FORMAL_CONTACT_ACQUISITION_SCHEMA_V2:
            raise ValueError("unsupported formal contact acquisition handoff schema")
        if self.route_identity != FORMAL_ROUTE_IDENTITY:
            raise ValueError("handoff route identity mismatch")
        if self.lease_id <= 0 or self.episode_identity <= 0:
            raise ValueError("handoff lease and episode identity must be positive")
        if self.contact_latch_sample_index < 0:
            raise ValueError("handoff latch sample index must be non-negative")
        if not math.isfinite(self.stationary_dwell_s) or self.stationary_dwell_s <= 0.0:
            raise ValueError("handoff stationary dwell is invalid")
        object.__setattr__(
            self,
            "anchor_pose_base",
            _finite_vector(self.anchor_pose_base, 6, "handoff anchor pose"),
        )
        if self.force_authority != KUNWEI_RAW_AUTHORITY_IDENTITY:
            raise ValueError("handoff force authority is not Kunwei KWR75 raw TCP")
        if self.acquisition_training:
            raise ValueError("acquisition handoff cannot be training evidence")

    def as_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "route_identity": self.route_identity,
            "lease_id": self.lease_id,
            "episode_identity": self.episode_identity,
            "contact_latch_sample_index": self.contact_latch_sample_index,
            "stationary_dwell_s": self.stationary_dwell_s,
            "anchor_pose_base": list(self.anchor_pose_base),
            "acquisition_training": self.acquisition_training,
            "force_authority": self.force_authority,
        }


@dataclass(frozen=True)
class DirectTorqueTransitionCommand:
    phase: str
    desired_pose_base: tuple[float, ...]
    raw_feedforward_wrench: tuple[float, ...]
    stiffness_6d: tuple[float, ...] = FIXED_TRACKING_STIFFNESS


class FormalContactAcquisitionControllerV1:
    """Host-authoritative acquisition state machine with no return on fault."""

    def __init__(
        self,
        contract: object,
        *,
        route_identity: str = FORMAL_ROUTE_IDENTITY,
    ) -> None:
        self.contract = contract
        self.route_identity = str(route_identity)
        if self.route_identity != FORMAL_ROUTE_IDENTITY:
            raise ValueError("formal acquisition route identity is frozen")
        self.speed_m_s = _contract_value(
            contract, "approach_speed_m_s", ACQUISITION_SPEED_M_S
        )
        self.maximum_distance_m = _contract_value(
            contract, "maximum_search_distance_m", ACQUISITION_MAX_DISTANCE_M
        )
        self.acceleration_m_s2 = _contract_value(
            contract, "acquisition_acceleration_m_s2", ACQUISITION_ACCELERATION_M_S2
        )
        self.deceleration_m_s2 = _contract_value(
            contract, "acquisition_deceleration_m_s2", ACQUISITION_DECELERATION_M_S2
        )
        self.sensor_delivery_watchdog_s = _contract_value(
            contract,
            "sensor_delivery_watchdog_s",
            ACQUISITION_SENSOR_DELIVERY_WATCHDOG_S,
        )
        self.stationary_dwell_s = _contract_value(
            contract, "stationary_dwell_s", ACQUISITION_STATIONARY_DWELL_S
        )
        self.stationary_tcp_speed_limit_m_s = _contract_value(
            contract,
            "stationary_tcp_speed_limit_m_s",
            ACQUISITION_STATIONARY_TCP_SPEED_LIMIT_M_S,
        )
        self.stationary_rotation_speed_limit_rad_s = _contract_value(
            contract,
            "stationary_rotation_speed_limit_rad_s",
            ACQUISITION_STATIONARY_ROTATION_SPEED_LIMIT_RAD_S,
        )
        self.stationary_joint_speed_limit_rad_s = _contract_value(
            contract,
            "stationary_joint_speed_limit_rad_s",
            ACQUISITION_STATIONARY_JOINT_SPEED_LIMIT_RAD_S,
        )
        self.maximum_handoff_mismatch_m = _contract_value(
            contract, "maximum_handoff_mismatch_m", MAX_HANDOFF_MISMATCH_M
        )
        self.latch_samples = _contract_int(
            contract, "latch_samples", ACQUISITION_LATCH_SAMPLES
        )
        self.latch_load_n = _contract_value(
            contract, "contact_latch_load_n", ACQUISITION_LATCH_LOAD_N
        )
        self.force_guard_n = ACQUISITION_FORCE_GUARD_N
        self.torque_guard_nm = ACQUISITION_TORQUE_GUARD_NM
        if not math.isclose(self.speed_m_s, ACQUISITION_SPEED_M_S, abs_tol=1e-12):
            raise ValueError("formal acquisition speed is not exactly 0.0005 m/s")
        if not math.isclose(self.maximum_distance_m, ACQUISITION_MAX_DISTANCE_M, abs_tol=1e-12):
            raise ValueError("formal acquisition maximum distance is frozen")
        if self.latch_samples != ACQUISITION_LATCH_SAMPLES:
            raise ValueError("formal acquisition latch count is frozen")
        if not math.isclose(self.latch_load_n, ACQUISITION_LATCH_LOAD_N, abs_tol=1e-12):
            raise ValueError("formal acquisition latch load is frozen")
        if not math.isclose(
            self.sensor_delivery_watchdog_s,
            ACQUISITION_SENSOR_DELIVERY_WATCHDOG_S,
            rel_tol=0.0,
            abs_tol=1.0e-12,
        ):
            raise ValueError("formal acquisition sensor delivery watchdog is frozen at 0.080 s")
        self._state = AcquisitionState.IDLE
        self._lease_id = 0
        self._episode_identity = 0
        self._search_started_s: float | None = None
        self._last_sensor_index: int | None = None
        self._latch_count = 0
        self._latch_sample_index: int | None = None
        self._dwell_started_s: float | None = None
        self._last_stationary_time_s: float | None = None
        self._handoff: AcquisitionHandoffV1 | None = None
        self._fault_reason: str | None = None

    @property
    def state(self) -> AcquisitionState:
        return self._state

    @property
    def fault_reason(self) -> str | None:
        return self._fault_reason

    @property
    def contact_latch_count(self) -> int:
        return self._latch_count

    @property
    def handoff(self) -> AcquisitionHandoffV1:
        if self._handoff is None or self._state != AcquisitionState.HANDOFF_READY:
            raise AcquisitionError("formal_contact_handoff_not_ready")
        return self._handoff

    @property
    def auto_return_allowed(self) -> bool:
        # Search exhaustion is a safe no-contact terminal.  Every fault is
        # latched in place and has no automatic retract/home action.
        return self._state == AcquisitionState.SEARCH_EXHAUSTED

    def _fault(self, reason: str) -> None:
        if self._state == AcquisitionState.FAULT:
            return
        self._fault_reason = str(reason)
        self._state = AcquisitionState.FAULT
        self._dwell_started_s = None
        self._handoff = None

    def fail_closed(self, reason: str) -> None:
        """Latch an externally observed fault without authorizing a return."""

        self._fault(str(reason))

    def _identity_fault(self, reason: str) -> AcquisitionError:
        self._fault(reason)
        return AcquisitionError(reason)

    def start(
        self,
        *,
        lease_id: int,
        episode_identity: int,
        route_identity: str,
        monotonic_s: float,
    ) -> AcquisitionCommand:
        if self._state != AcquisitionState.IDLE:
            raise AcquisitionError("formal_acquisition_start_state_invalid")
        if str(route_identity) != self.route_identity:
            raise self._identity_fault("route_fault")
        if int(lease_id) <= 0 or int(episode_identity) <= 0:
            raise self._identity_fault("route_fault")
        if not math.isfinite(float(monotonic_s)):
            raise self._identity_fault("sensor_fault")
        self._lease_id = int(lease_id)
        self._episode_identity = int(episode_identity)
        self._search_started_s = float(monotonic_s)
        self._state = AcquisitionState.VELOCITY_SEARCH
        return self.command_at(monotonic_s)

    def _search_profile(self, elapsed_s: float) -> tuple[float, float]:
        """Return bounded distance and speed; distance never exceeds 25 mm."""

        elapsed = max(0.0, float(elapsed_s))
        accel_time = self.speed_m_s / self.acceleration_m_s2
        decel_time = self.speed_m_s / self.deceleration_m_s2
        accel_distance = 0.5 * self.acceleration_m_s2 * accel_time**2
        decel_distance = 0.5 * self.speed_m_s * decel_time
        cruise_distance = (
            self.maximum_distance_m
            - accel_distance
            - decel_distance
            - ACQUISITION_DISCRETE_STOP_BOUND_M
        )
        if cruise_distance < 0.0:
            raise AcquisitionError("formal_acquisition_profile_has_no_bounded_cruise")
        cruise_time = cruise_distance / self.speed_m_s
        if elapsed < accel_time:
            speed = self.acceleration_m_s2 * elapsed
            distance = 0.5 * self.acceleration_m_s2 * elapsed**2
        elif elapsed < accel_time + cruise_time:
            speed = self.speed_m_s
            distance = accel_distance + self.speed_m_s * (elapsed - accel_time)
        elif elapsed < accel_time + cruise_time + decel_time:
            decel_elapsed = elapsed - accel_time - cruise_time
            speed = max(0.0, self.speed_m_s - self.deceleration_m_s2 * decel_elapsed)
            distance = (
                accel_distance
                + cruise_distance
                + self.speed_m_s * decel_elapsed
                - 0.5 * self.deceleration_m_s2 * decel_elapsed**2
            )
        else:
            speed = 0.0
            distance = self.maximum_distance_m
        return min(self.maximum_distance_m, max(0.0, distance)), max(0.0, speed)

    def command_at(self, monotonic_s: float) -> AcquisitionCommand:
        now = float(monotonic_s)
        if not math.isfinite(now):
            self._fault("sensor_fault")
            raise AcquisitionError("sensor_fault")
        if self._state == AcquisitionState.VELOCITY_SEARCH:
            if self._search_started_s is None or now < self._search_started_s:
                self._fault("sensor_fault")
                raise AcquisitionError("sensor_fault")
            distance, speed = self._search_profile(now - self._search_started_s)
            if distance >= self.maximum_distance_m and speed <= 0.0:
                self._state = AcquisitionState.SEARCH_EXHAUSTED
                return AcquisitionCommand(
                    state=self._state,
                    velocity_base=ZERO6,
                    distance_m=self.maximum_distance_m,
                    stop_velocity_control=True,
                )
            return AcquisitionCommand(
                state=self._state,
                velocity_base=(0.0, 0.0, -speed, 0.0, 0.0, 0.0),
                distance_m=distance,
                stop_velocity_control=False,
            )
        if self._state in {
            AcquisitionState.STOPPING,
            AcquisitionState.STATIONARY_DWELL,
        }:
            distance = self.maximum_distance_m
            if self._search_started_s is not None:
                distance, _ = self._search_profile(max(0.0, now - self._search_started_s))
            return AcquisitionCommand(
                state=self._state,
                velocity_base=ZERO6,
                distance_m=distance,
                stop_velocity_control=True,
            )
        return AcquisitionCommand(
            state=self._state,
            velocity_base=ZERO6,
            distance_m=(
                self.maximum_distance_m
                if self._state == AcquisitionState.SEARCH_EXHAUSTED
                else 0.0
            ),
            stop_velocity_control=True,
        )

    def _validate_kunwei_sample(self, sample: KunweiAcquisitionSample) -> None:
        if sample.sample_index < 0 or sample.lease_id <= 0 or sample.episode_identity <= 0:
            raise self._identity_fault("sensor_fault")
        if sample.route_identity != self.route_identity:
            raise self._identity_fault("route_fault")
        if sample.lease_id != self._lease_id or sample.episode_identity != self._episode_identity:
            raise self._identity_fault("route_identity_changed")
        if sample.robot_mode != "RUNNING":
            raise self._identity_fault("safety_changed")
        if sample.safety_mode != "NORMAL":
            raise self._identity_fault("safety_changed")
        if sample.protective_stop:
            raise self._identity_fault("protective_stop")
        if sample.joint_fault:
            raise self._identity_fault("joint_fault")
        if (
            not math.isfinite(sample.host_age_s)
            or sample.host_age_s < 0.0
            or sample.host_age_s > self.sensor_delivery_watchdog_s
        ):
            raise self._identity_fault("sensor_fault")
        if not all(
            math.isfinite(value)
            for value in (
                sample.normal_load_n,
                sample.force_norm_n,
                sample.torque_norm_nm,
            )
        ):
            raise self._identity_fault("sensor_fault")
        if sample.force_norm_n > self.force_guard_n:
            raise self._identity_fault("force_guard")
        if sample.torque_norm_nm > self.torque_guard_nm:
            raise self._identity_fault("torque_guard")
        if self._last_sensor_index is not None and sample.sample_index != self._last_sensor_index + 1:
            raise self._identity_fault("sensor_fault")
        self._last_sensor_index = sample.sample_index

    def observe_kunwei(
        self,
        sample: KunweiAcquisitionSample | Mapping[str, Any],
    ) -> bool:
        if self._state not in {
            AcquisitionState.VELOCITY_SEARCH,
            AcquisitionState.STOPPING,
            AcquisitionState.STATIONARY_DWELL,
        }:
            raise AcquisitionError("formal_acquisition_sensor_state_invalid")
        typed = KunweiAcquisitionSample.from_value(sample)
        self._validate_kunwei_sample(typed)
        if self._state != AcquisitionState.VELOCITY_SEARCH:
            return False
        if typed.normal_load_n >= self.latch_load_n:
            self._latch_count += 1
        else:
            self._latch_count = 0
        if self._latch_count >= self.latch_samples:
            self._latch_sample_index = typed.sample_index
            self._state = AcquisitionState.STOPPING
            self._dwell_started_s = None
            return True
        return False

    def _validate_stationary_sample(self, sample: StationaryPoseSample) -> None:
        if sample.route_identity != self.route_identity:
            raise self._identity_fault("route_fault")
        if sample.lease_id != self._lease_id or sample.episode_identity != self._episode_identity:
            raise self._identity_fault("route_identity_changed")
        if sample.robot_mode != "RUNNING" or sample.safety_mode != "NORMAL":
            raise self._identity_fault("safety_changed")
        if sample.protective_stop:
            raise self._identity_fault("protective_stop")
        if sample.joint_fault:
            raise self._identity_fault("joint_fault")
        if (
            not math.isfinite(sample.sample_time_s)
            or not math.isfinite(sample.host_age_s)
            or sample.host_age_s < 0.0
            or sample.host_age_s > self.sensor_delivery_watchdog_s
        ):
            raise self._identity_fault("sensor_fault")
        if (
            self._last_stationary_time_s is not None
            and sample.sample_time_s <= self._last_stationary_time_s
        ):
            raise self._identity_fault("sensor_fault")
        self._last_stationary_time_s = sample.sample_time_s

    def observe_stationary(
        self,
        sample: StationaryPoseSample | Mapping[str, Any],
    ) -> AcquisitionHandoffV1 | None:
        if self._state not in {
            AcquisitionState.STOPPING,
            AcquisitionState.STATIONARY_DWELL,
        }:
            raise AcquisitionError("formal_stationary_dwell_state_invalid")
        typed = StationaryPoseSample.from_value(sample)
        self._validate_stationary_sample(typed)
        translation_speed = _norm(typed.tcp_speed_base[:3])
        rotation_speed = _norm(typed.tcp_speed_base[3:])
        joint_speed = max(abs(value) for value in typed.joint_speed_rad_s)
        stationary = (
            translation_speed <= self.stationary_tcp_speed_limit_m_s
            and rotation_speed <= self.stationary_rotation_speed_limit_rad_s
            and joint_speed <= self.stationary_joint_speed_limit_rad_s
        )
        if not stationary:
            self._state = AcquisitionState.STOPPING
            self._dwell_started_s = None
            return None
        if self._dwell_started_s is None:
            self._dwell_started_s = typed.sample_time_s
            self._state = AcquisitionState.STATIONARY_DWELL
            return None
        if typed.sample_time_s - self._dwell_started_s + 1e-12 < self.stationary_dwell_s:
            self._state = AcquisitionState.STATIONARY_DWELL
            return None
        if self._latch_sample_index is None:
            self._fault("sensor_fault")
            raise AcquisitionError("sensor_fault")
        self._handoff = AcquisitionHandoffV1(
            route_identity=self.route_identity,
            lease_id=self._lease_id,
            episode_identity=self._episode_identity,
            contact_latch_sample_index=self._latch_sample_index,
            stationary_dwell_s=typed.sample_time_s - self._dwell_started_s,
            anchor_pose_base=typed.actual_pose_base,
        )
        self._state = AcquisitionState.HANDOFF_READY
        return self._handoff


class BumplessDirectTorqueTransitionV1:
    """Frozen K=600 tracking entry after a successful acquisition handoff."""

    def __init__(self, handoff: AcquisitionHandoffV1, contract: object) -> None:
        if not isinstance(handoff, AcquisitionHandoffV1):
            raise TypeError("Direct Torque requires an acquisition handoff")
        self.handoff = handoff
        self.settle_duration_s = _contract_value(contract, "settle_duration_s", 0.5)
        self.maximum_handoff_mismatch_m = _contract_value(
            contract, "maximum_handoff_mismatch_m", MAX_HANDOFF_MISMATCH_M
        )
        self._started = False
        self._anchor_pose: tuple[float, ...] | None = None

    def start(
        self,
        fresh_actual_pose_base: Sequence[float],
    ) -> DirectTorqueTransitionCommand:
        actual = _finite_vector(fresh_actual_pose_base, 6, "fresh handoff actual pose")
        mismatch = _norm(
            tuple(actual[index] - self.handoff.anchor_pose_base[index] for index in range(3))
        )
        if mismatch > self.maximum_handoff_mismatch_m + 1e-12:
            raise AcquisitionError("handoff_anchor_mismatch")
        self._anchor_pose = actual
        self._started = True
        return DirectTorqueTransitionCommand(
            phase="SETTLE",
            desired_pose_base=actual,
            raw_feedforward_wrench=ZERO6,
        )

    def step(
        self,
        elapsed_s: float,
        target_feedforward_wrench: Sequence[float],
    ) -> DirectTorqueTransitionCommand:
        if not self._started or self._anchor_pose is None:
            raise AcquisitionError("direct_torque_handoff_not_started")
        elapsed = float(elapsed_s)
        if not math.isfinite(elapsed) or elapsed < 0.0:
            raise ValueError("tracking transition elapsed time is invalid")
        target = _finite_vector(target_feedforward_wrench, 6, "target feedforward wrench")
        ramp = _smooth01(elapsed / self.settle_duration_s)
        return DirectTorqueTransitionCommand(
            phase="TRACK" if elapsed >= self.settle_duration_s else "SETTLE",
            desired_pose_base=self._anchor_pose,
            raw_feedforward_wrench=tuple(ramp * value for value in target),
        )

    @staticmethod
    def force_from_translation_error(error_m: Sequence[float]) -> tuple[float, ...]:
        error = _finite_vector(error_m, 3, "translation error")
        return tuple(600.0 * value for value in error)


def build_formal_contact_acquisition_urscript(
    contract: object,
    *,
    route_token: int = ACQUISITION_ROUTE_TOKEN,
) -> str:
    """Build the inert source for the separate bounded velocity phase."""

    route = int(route_token)
    if route != ACQUISITION_ROUTE_TOKEN:
        raise ValueError("formal acquisition route token is frozen")
    speed = _contract_value(contract, "approach_speed_m_s", ACQUISITION_SPEED_M_S)
    maximum = _contract_value(
        contract, "maximum_search_distance_m", ACQUISITION_MAX_DISTANCE_M
    )
    acceleration = _contract_value(
        contract,
        "acquisition_acceleration_m_s2",
        ACQUISITION_ACCELERATION_M_S2,
    )
    deceleration = _contract_value(
        contract,
        "acquisition_deceleration_m_s2",
        ACQUISITION_DECELERATION_M_S2,
    )
    if not math.isclose(speed, ACQUISITION_SPEED_M_S, abs_tol=1e-12):
        raise ValueError("formal acquisition URScript speed is not frozen")
    if not math.isclose(maximum, ACQUISITION_MAX_DISTANCE_M, abs_tol=1e-12):
        raise ValueError("formal acquisition URScript distance is not frozen")
    braking_distance = speed**2 / (2.0 * deceleration)
    discrete_stop_bound = speed * ACQUISITION_CONTROL_PERIOD_S
    deceleration_start = maximum - braking_distance - discrete_stop_bound
    if not math.isclose(
        acceleration,
        ACQUISITION_ACCELERATION_M_S2,
        rel_tol=0.0,
        abs_tol=1.0e-12,
    ) or not math.isclose(
        deceleration,
        ACQUISITION_DECELERATION_M_S2,
        rel_tol=0.0,
        abs_tol=1.0e-12,
    ):
        raise ValueError("formal acquisition acceleration/deceleration is frozen at 0.010 m/s^2")
    if not math.isclose(
        braking_distance,
        ACQUISITION_BRAKING_DISTANCE_M,
        rel_tol=0.0,
        abs_tol=1.0e-15,
    ):
        raise ValueError("formal acquisition braking distance is not frozen")
    source = f'''def tacdiffusion_formal_contact_acquisition_v2():
  local acquisition_schema = "{FORMAL_CONTACT_ACQUISITION_SCHEMA_V2}"
  local acquisition_route_token = {route}
  local source_builder_physical_io_enabled = False
  local controller_runtime_physical_io_enabled = True
  local host_latch_authority = "{KUNWEI_RAW_AUTHORITY_IDENTITY}"
  local acquisition_control_period_s = 0.002
  local acquisition_sensor_delivery_watchdog_s = 0.080
  local acquisition_heartbeat_timeout_s = 0.080
  local acquisition_heartbeat_timeout_ticks = 40
  local acquisition_prepare_timeout_s = 0.400
  local acquisition_prepare_timeout_ticks = 200
  local acquisition_speed_m_s = 0.0005
  local acquisition_max_distance_m = 0.025
  local acquisition_acceleration_m_s2 = {acceleration:.10f}
  local acquisition_deceleration_m_s2 = {deceleration:.10f}
  local acquisition_braking_distance_m = {braking_distance:.10f}
  local acquisition_discrete_stop_bound_m = {discrete_stop_bound:.10f}
  local acquisition_deceleration_start_distance_m = {deceleration_start:.10f}
  local command_prepare = 0
  local command_start = 1
  local command_abort = 2
  local command_stop_no_contact = 3
  local acquisition_running = True
  local acquisition_state = 0
  local acquisition_fault = 0
  local acquisition_sequence = 0
  local acquisition_last_packet_sequence = 0
  local acquisition_held_ticks = 0
  local acquisition_last_command = 0
  local acquisition_last_lease = 0
  local acquisition_last_episode = 0
  local acquisition_last_route_token = 0
  local acquisition_last_safety_normal = 0
  local acquisition_last_robot_running = 0
  local acquisition_last_host_latch = 0
  local acquisition_last_handoff_ack = 0
  local acquisition_lease = 0
  local acquisition_episode = 0
  local acquisition_entry_pose = get_actual_tcp_pose()
  local acquisition_last_pose = acquisition_entry_pose
  local acquisition_stationary_elapsed_s = 0.0
  # These values are published through output_integer_registers.  UR 5.26
  # rejects Bool payloads at runtime (type_not_int:Bool), so keep the wire
  # representation integer-valued for the lifetime of the program.
  local acquisition_latched_by_host = 0
  local acquisition_handoff_ack = 0

  write_output_integer_register(24, acquisition_state)
  write_output_integer_register(25, acquisition_sequence)
  write_output_integer_register(26, acquisition_fault)
  write_output_integer_register(27, acquisition_lease)
  write_output_integer_register(28, acquisition_episode)
  write_output_integer_register(29, acquisition_route_token)
  write_output_integer_register(30, 0)
  write_output_integer_register(31, 0)
  while acquisition_running:
    local command = read_input_integer_register(24)
    local packet_sequence_before = read_input_integer_register(25)
    local packet_sequence_after = read_input_integer_register(25)
    local packet_sequence = packet_sequence_after
    local packet_lease = read_input_integer_register(26)
    local packet_episode = read_input_integer_register(27)
    local packet_route_token = read_input_integer_register(28)
    local packet_safety_normal = read_input_integer_register(29)
    local packet_robot_running = read_input_integer_register(30)
    local packet_host_latch = read_input_integer_register(31)
    local packet_handoff_ack = read_input_integer_register(32)
    local actual_pose = get_actual_tcp_pose()
    local actual_speed = get_actual_tcp_speed()
    local actual_dx = actual_pose[0] - acquisition_entry_pose[0]
    local actual_dy = actual_pose[1] - acquisition_entry_pose[1]
    local actual_dz = actual_pose[2] - acquisition_entry_pose[2]
    local travelled_m = sqrt(actual_dx*actual_dx + actual_dy*actual_dy + actual_dz*actual_dz)
    local identity_ok = packet_route_token == acquisition_route_token and packet_lease > 0 and packet_episode > 0 and packet_safety_normal == 1 and packet_robot_running == 1
    local packet_torn = packet_sequence_before != packet_sequence_after
    local packet_new = packet_sequence == acquisition_last_packet_sequence + 1
    local packet_held = packet_sequence == acquisition_last_packet_sequence
    local packet_signature_same = command == acquisition_last_command and packet_lease == acquisition_last_lease and packet_episode == acquisition_last_episode and packet_route_token == acquisition_last_route_token and packet_safety_normal == acquisition_last_safety_normal and packet_robot_running == acquisition_last_robot_running and packet_host_latch == acquisition_last_host_latch and packet_handoff_ack == acquisition_last_handoff_ack
    local packet_gap_or_replay = packet_torn or (not packet_new and not packet_held) or (packet_held and not packet_signature_same)
    local packet_heartbeat_timeout = False
    local packet_prepare_timeout = False
    local motion_frame_consumed = False
    if not packet_torn and packet_new:
      acquisition_last_packet_sequence = packet_sequence
      acquisition_held_ticks = 0
      acquisition_last_command = command
      acquisition_last_lease = packet_lease
      acquisition_last_episode = packet_episode
      acquisition_last_route_token = packet_route_token
      acquisition_last_safety_normal = packet_safety_normal
      acquisition_last_robot_running = packet_robot_running
      acquisition_last_host_latch = packet_host_latch
      acquisition_last_handoff_ack = packet_handoff_ack
    elif not packet_torn and packet_held and packet_signature_same:
      acquisition_held_ticks = acquisition_held_ticks + 1
      if acquisition_state == 0 and acquisition_held_ticks >= acquisition_prepare_timeout_ticks:
        packet_prepare_timeout = True
      elif acquisition_state != 0 and acquisition_held_ticks >= acquisition_heartbeat_timeout_ticks:
        packet_heartbeat_timeout = True
      end
    end
    local packet_protocol_ok = not packet_gap_or_replay
    if not identity_ok:
      acquisition_fault = 10
      acquisition_state = 6
      speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], a=acquisition_deceleration_m_s2, t=acquisition_control_period_s)
      stopl(a=acquisition_deceleration_m_s2)
      acquisition_running = False
    elif not packet_protocol_ok:
      acquisition_fault = 12
      acquisition_state = 6
      speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], a=acquisition_deceleration_m_s2, t=acquisition_control_period_s)
      stopl(a=acquisition_deceleration_m_s2)
      acquisition_running = False
    elif packet_heartbeat_timeout:
      acquisition_fault = 13
      acquisition_state = 6
      speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], a=acquisition_deceleration_m_s2, t=acquisition_control_period_s)
      stopl(a=acquisition_deceleration_m_s2)
      acquisition_running = False
    elif packet_prepare_timeout:
      acquisition_fault = 15
      acquisition_state = 6
      speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], a=acquisition_deceleration_m_s2, t=acquisition_control_period_s)
      stopl(a=acquisition_deceleration_m_s2)
      acquisition_running = False
    elif acquisition_state == 0 and command == command_prepare:
      # PREPARE keeps the controller inert while the Primary start barrier is held.
      acquisition_sequence = packet_sequence
    elif acquisition_state == 0 and command == command_start and packet_new:
      acquisition_lease = packet_lease
      acquisition_episode = packet_episode
      acquisition_sequence = packet_sequence
      acquisition_entry_pose = actual_pose
      acquisition_last_pose = actual_pose
      acquisition_state = 1
    elif packet_lease != acquisition_lease or packet_episode != acquisition_episode:
      acquisition_fault = 11
      acquisition_state = 6
      speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], a=acquisition_deceleration_m_s2, t=acquisition_control_period_s)
      stopl(a=acquisition_deceleration_m_s2)
      acquisition_running = False
    elif command == command_abort:
      # Abort is an immediate bounded zero-speed stop; it never returns home.
      acquisition_fault = 20
      acquisition_state = 6
      speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], a=acquisition_deceleration_m_s2, t=acquisition_control_period_s)
      stopl(a=acquisition_deceleration_m_s2)
      acquisition_running = False
    elif command == command_stop_no_contact and acquisition_state == 1:
      acquisition_state = 5
      speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], a=acquisition_deceleration_m_s2, t=acquisition_control_period_s)
      stopl(a=acquisition_deceleration_m_s2)
      acquisition_running = False
    elif acquisition_state == 1 and command != command_start:
      acquisition_fault = 14
      acquisition_state = 6
      speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], a=acquisition_deceleration_m_s2, t=acquisition_control_period_s)
      stopl(a=acquisition_deceleration_m_s2)
      acquisition_running = False
    elif (acquisition_state == 2 or acquisition_state == 3) and command != command_start:
      acquisition_fault = 14
      acquisition_state = 6
      speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], a=acquisition_deceleration_m_s2, t=acquisition_control_period_s)
      stopl(a=acquisition_deceleration_m_s2)
      acquisition_running = False
    elif packet_host_latch == 1 and acquisition_state == 1:
      # The host, not this controller, owns the Kunwei 1 N / 50-frame latch.
      acquisition_latched_by_host = 1
      acquisition_state = 2
      speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], a=acquisition_deceleration_m_s2, t=acquisition_control_period_s)
      stopl(a=acquisition_deceleration_m_s2)
    elif acquisition_state == 1:
      if travelled_m >= acquisition_deceleration_start_distance_m:
        # v^2/(2a) = 12.5 micrometres; the one-tick bound is also reserved.
        acquisition_state = 5
        speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], a=acquisition_deceleration_m_s2, t=acquisition_control_period_s)
        stopl(a=acquisition_deceleration_m_s2)
        acquisition_running = False
      else:
        # The cruise command is exactly 0.5 mm/s in base -Z.
        speedl([0.0, 0.0, -0.0005, 0.0, 0.0, 0.0], a=acquisition_acceleration_m_s2, t=acquisition_control_period_s)
        # speedl(t=control_period) already consumes this controller frame.
        # Adding an extra sync here inserts a zero-command gap and repeatedly resets
        # the 0.010 m/s^2 ramp before it can reach the frozen cruise speed.
        motion_frame_consumed = True
      end
    elif acquisition_state == 2 or acquisition_state == 3:
      acquisition_state = 3
      local translation_speed = sqrt(actual_speed[0]*actual_speed[0] + actual_speed[1]*actual_speed[1] + actual_speed[2]*actual_speed[2])
      local rotation_speed = sqrt(actual_speed[3]*actual_speed[3] + actual_speed[4]*actual_speed[4] + actual_speed[5]*actual_speed[5])
      if translation_speed <= 0.0001 and rotation_speed <= 0.002:
        acquisition_stationary_elapsed_s = acquisition_stationary_elapsed_s + acquisition_control_period_s
      else:
        acquisition_stationary_elapsed_s = 0.0
      end
      if packet_handoff_ack == 1 and acquisition_latched_by_host == 1 and acquisition_stationary_elapsed_s >= 0.100:
        acquisition_handoff_ack = 1
        acquisition_state = 4
        acquisition_last_pose = actual_pose
        acquisition_running = False
      end
    end
    write_output_integer_register(24, acquisition_state)
    write_output_integer_register(25, packet_sequence)
    write_output_integer_register(26, acquisition_fault)
    write_output_integer_register(27, acquisition_lease)
    write_output_integer_register(28, acquisition_episode)
    write_output_integer_register(29, acquisition_route_token)
    write_output_integer_register(30, acquisition_handoff_ack)
    write_output_integer_register(31, acquisition_latched_by_host)
    acquisition_sequence = packet_sequence
    acquisition_last_pose = actual_pose
    if not motion_frame_consumed:
      sync()
    end
  end
  write_output_integer_register(24, acquisition_state)
  write_output_integer_register(25, acquisition_sequence)
  write_output_integer_register(26, acquisition_fault)
end
'''
    return source


def parse_formal_contact_acquisition_urscript(source: str) -> None:
    required = (
        f'acquisition_schema = "{FORMAL_CONTACT_ACQUISITION_SCHEMA_V2}"',
        f"acquisition_route_token = {ACQUISITION_ROUTE_TOKEN}",
        'host_latch_authority = "kunwei_kwr75_tcp_raw_stream_v1"',
        "acquisition_control_period_s = 0.002",
        "acquisition_sensor_delivery_watchdog_s = 0.080",
        "acquisition_heartbeat_timeout_s = 0.080",
        "acquisition_heartbeat_timeout_ticks = 40",
        "acquisition_prepare_timeout_s = 0.400",
        "acquisition_prepare_timeout_ticks = 200",
        "acquisition_speed_m_s = 0.0005",
        "acquisition_max_distance_m = 0.025",
        "acquisition_acceleration_m_s2 = ",
        "acquisition_deceleration_m_s2 = ",
        "acquisition_braking_distance_m = ",
        "acquisition_discrete_stop_bound_m = ",
        "acquisition_deceleration_start_distance_m = ",
        "command_prepare = 0",
        "command_start = 1",
        "command_abort = 2",
        "command_stop_no_contact = 3",
        "packet_sequence_before = read_input_integer_register(25)",
        "packet_sequence_after = read_input_integer_register(25)",
        "packet_new = packet_sequence == acquisition_last_packet_sequence + 1",
        "packet_held = packet_sequence == acquisition_last_packet_sequence",
        "packet_gap_or_replay",
        "packet_heartbeat_timeout",
        "packet_prepare_timeout",
        "packet_host_latch == 1",
        "acquisition_latched_by_host = 0",
        "acquisition_handoff_ack = 0",
        "acquisition_latched_by_host = 1",
        "acquisition_handoff_ack = 1",
        "motion_frame_consumed = False",
        "motion_frame_consumed = True",
        "if not motion_frame_consumed:",
        "speedl([0.0, 0.0, -0.0005, 0.0, 0.0, 0.0],",
        "speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0],",
        "stopl(a=acquisition_deceleration_m_s2)",
        "packet_lease != acquisition_lease or packet_episode != acquisition_episode",
        "packet_route_token == acquisition_route_token",
        "packet_safety_normal == 1",
        "packet_robot_running == 1",
        "translation_speed <= 0.0001 and rotation_speed <= 0.002",
        "acquisition_stationary_elapsed_s >= 0.100",
    )
    for token in required:
        if token not in source:
            raise ValueError(f"formal acquisition source missing token: {token}")
    forbidden = (
        r"\bactual_TCP_force\b",
        r"\bget_tcp_force\s*\(",
        r"\bdirect_torque\s*\(",
        r"\bforce_mode\s*\(",
        r"\b(movej|movel|movec|speedj|servoj)\s*\(",
        r"\b(read_input_float_register|socket|dashboard|ssh)\b",
        r"\b(load|play)\s*\(",
    )
    if any(re.search(pattern, source, flags=re.IGNORECASE) for pattern in forbidden):
        raise ValueError("formal acquisition source contains a forbidden primitive")
    if not source.startswith("def tacdiffusion_formal_contact_acquisition_v2():\n"):
        raise ValueError("formal acquisition source requires one top-level program")
    if len(re.findall(r"(?m)^\s*def\s+", source)) != 1:
        raise ValueError("formal acquisition source requires one top-level program")
    if re.search(
        r"(?m)^\s*tacdiffusion_formal_contact_acquisition_v2\(\)\s*$", source
    ):
        raise ValueError("formal acquisition source must not invoke itself")
    if source.count("speedl(") < 9:
        raise ValueError("formal acquisition source must have bounded stop calls for every fault path")
    if source.count("stopl(a=acquisition_deceleration_m_s2)") < 9:
        raise ValueError("formal acquisition source must stop every fault path with frozen deceleration")
    if source.count("sync()") != 1:
        raise ValueError("formal acquisition source requires one bounded sync site")


def build_formal_direct_torque_tracking_source_after_handoff(
    tube: object,
    handoff: AcquisitionHandoffV1,
    contract: object,
    *,
    fresh_actual_pose_base: Sequence[float],
) -> str:
    """Build Direct Torque only after validating the acquisition handoff."""

    transition = BumplessDirectTorqueTransitionV1(handoff, contract)
    transition.start(fresh_actual_pose_base)
    # The import is deliberately lazy: the acquisition primitives remain
    # usable without importing the Direct Torque transport module.
    from .direct_torque_live_v4 import build_live_receiver_source

    return build_live_receiver_source(
        tube,
        friction_profile="ur_default_v2_formal_contact",
        guard_force_limit_n=50.0,
        guard_torque_limit_nm=4.0,
        formal_handoff_anchor_pose_base=handoff.anchor_pose_base,
        formal_handoff_max_mismatch_m=transition.maximum_handoff_mismatch_m,
        model_inactive_expert_feedforward_allowed=True,
        formal_contact_entry_transition_profile="formal_contact_entry_transition_v1",
    )


__all__ = [
    "ACQUISITION_ACCELERATION_M_S2",
    "ACQUISITION_BRAKING_DISTANCE_M",
    "ACQUISITION_COMMAND_ABORT",
    "ACQUISITION_COMMAND_PREPARE",
    "ACQUISITION_COMMAND_START",
    "ACQUISITION_COMMAND_STOP_NO_CONTACT",
    "ACQUISITION_CONTROL_PERIOD_S",
    "ACQUISITION_DECELERATION_M_S2",
    "ACQUISITION_DECELERATION_START_DISTANCE_M",
    "ACQUISITION_DISCRETE_STOP_BOUND_M",
    "ACQUISITION_FORCE_GUARD_N",
    "ACQUISITION_HEARTBEAT_TIMEOUT_TICKS",
    "ACQUISITION_PREPARE_TIMEOUT_TICKS",
    "ACQUISITION_LATCH_LOAD_N",
    "ACQUISITION_LATCH_SAMPLES",
    "ACQUISITION_MAX_DISTANCE_M",
    "ACQUISITION_ROUTE_TOKEN",
    "ACQUISITION_SENSOR_DELIVERY_WATCHDOG_S",
    "ACQUISITION_SPEED_M_S",
    "ACQUISITION_STATIONARY_DWELL_S",
    "ACQUISITION_STATIONARY_JOINT_SPEED_LIMIT_RAD_S",
    "ACQUISITION_STATIONARY_ROTATION_SPEED_LIMIT_RAD_S",
    "ACQUISITION_STATIONARY_TCP_SPEED_LIMIT_M_S",
    "ACQUISITION_TORQUE_GUARD_NM",
    "AcquisitionCommand",
    "AcquisitionError",
    "AcquisitionHeartbeatV1",
    "AcquisitionHandoffV1",
    "AcquisitionState",
    "BumplessDirectTorqueTransitionV1",
    "DirectTorqueTransitionCommand",
    "FIXED_TRACKING_STIFFNESS",
    "FORMAL_CONTACT_ACQUISITION_SCHEMA_V2",
    "FORMAL_ROUTE_IDENTITY",
    "FormalContactAcquisitionControllerV1",
    "KUNWEI_RAW_AUTHORITY_IDENTITY",
    "KunweiAcquisitionSample",
    "MAX_HANDOFF_MISMATCH_M",
    "StationaryPoseSample",
    "build_formal_contact_acquisition_urscript",
    "build_formal_direct_torque_tracking_source_after_handoff",
    "parse_formal_contact_acquisition_urscript",
]
