"""Offline-only FakeRTDE transport and evidence composition primitives.

This module models a typed packet boundary without opening a socket or
claiming controller, authority, optimizer, return-route, or parent-runtime
parity.  Transport freshness and host-command freshness are deliberately
separate: a fast consumer may retain one exact command across many packets.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from .campaign import Named7dCandidate, SCHEMA_VERSION, canonical_json_bytes, sha256_canonical
from .contracts import STEP6_R001_PROFILE, validate_path_time
from .errors import (
    CampaignContractError,
    FakeRTDEError,
    FakeRTDEEvidenceError,
    FakeRTDEFreshnessError,
    FakeRTDEIdentityError,
    InvalidPathTimeError,
)


ROUTE_IDENTITY_SCHEMA: Final[str] = "step6.autotune_v4.r001.offline_rtde_route_identity"
FRESHNESS_POLICY_SCHEMA: Final[str] = "step6.autotune_v4.r001.offline_rtde_freshness_policy"
COMMAND_PAYLOAD_SCHEMA: Final[str] = "step6.autotune_v4.r001.offline_rtde_command_payload"
COMMAND_SCHEMA: Final[str] = "step6.autotune_v4.r001.offline_rtde_command"
TRANSPORT_PACKET_SCHEMA: Final[str] = "step6.autotune_v4.r001.offline_rtde_transport_packet"
EVIDENCE_SCHEMA: Final[str] = "step6.autotune_v4.r001.offline_rtde_evidence_receipt"
EVIDENCE_ROLE: Final[str] = "diagnostic_only"
MIN_TRANSPORT_RATE_HZ: Final[float] = 460.0
MIN_OBSERVATION_DURATION_S: Final[float] = STEP6_R001_PROFILE.force_mae_spec.bin_width_s
PHASE_TOLERANCE_RAD: Final[float] = 1e-12

COMMAND_PAYLOAD_FIELDS: Final[tuple[str, ...]] = (
    "path_time_s",
    "continuous_phase_rad",
    "desired_base_x_m",
    "desired_base_y_m",
    "desired_base_vx_m_s",
    "desired_base_vy_m_s",
    "program_z_delta_m",
    "force_target_n",
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _sha(value: object, label: str, error_type: type[Exception] = FakeRTDEError) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise error_type(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _text(value: object, label: str, error_type: type[Exception] = FakeRTDEError) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{label} must be non-empty text")
    return value


def _strict_float(value: object, label: str, error_type: type[Exception] = FakeRTDEError) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise error_type(f"{label} must be a finite float, not bool/int/NaN/Inf")
    return value


def _positive_int(value: object, label: str, error_type: type[Exception] = FakeRTDEError) -> int:
    if type(value) is not int or value <= 0:
        raise error_type(f"{label} must be a positive integer")
    return value


def _exact_mapping(mapping: object, expected: set[str], error_type: type[Exception]) -> Mapping[str, object]:
    if not isinstance(mapping, Mapping):
        raise error_type("payload must be an exact typed mapping")
    actual = set(mapping.keys())
    if any(not isinstance(key, str) for key in mapping.keys()) or actual != expected:
        raise error_type(
            f"mapping fields differ; missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}"
        )
    return mapping


def _schema(mapping: Mapping[str, object], expected: str, error_type: type[Exception]) -> None:
    if (
        type(mapping["schema"]) is not str
        or mapping["schema"] != expected
        or type(mapping["schema_version"]) is not int
        or mapping["schema_version"] != SCHEMA_VERSION
    ):
        raise error_type(f"schema must be {expected!r} version {SCHEMA_VERSION}")


def _locked_schema(
    schema: object,
    schema_version: object,
    expected: str,
    error_type: type[Exception],
) -> None:
    if (
        type(schema) is not str
        or schema != expected
        or type(schema_version) is not int
        or schema_version != SCHEMA_VERSION
    ):
        raise error_type(f"schema must be {expected!r} version {SCHEMA_VERSION}")


@dataclass(frozen=True, slots=True)
class OfflineRtdeRouteIdentity:
    """The exact single-writer route binding for one offline composition."""

    campaign_fingerprint: str
    epoch: int
    writer_id: str
    route_id: str
    candidate: Named7dCandidate
    attempt_identity: str
    schema: str = ROUTE_IDENTITY_SCHEMA
    schema_version: int = SCHEMA_VERSION
    route_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _sha(self.campaign_fingerprint, "campaign_fingerprint", FakeRTDEIdentityError)
        _positive_int(self.epoch, "epoch", FakeRTDEIdentityError)
        _text(self.writer_id, "writer_id", FakeRTDEIdentityError)
        _text(self.route_id, "route_id", FakeRTDEIdentityError)
        if type(self.candidate) is not Named7dCandidate:
            raise FakeRTDEIdentityError("route candidate must be an exact Named7dCandidate")
        _sha(self.attempt_identity, "attempt_identity", FakeRTDEIdentityError)
        _locked_schema(self.schema, self.schema_version, ROUTE_IDENTITY_SCHEMA, FakeRTDEIdentityError)
        object.__setattr__(self, "route_fingerprint", sha256_canonical(self.to_content_mapping()))

    def to_content_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "campaign_fingerprint": self.campaign_fingerprint,
            "epoch": self.epoch,
            "writer_id": self.writer_id,
            "route_id": self.route_id,
            "candidate": self.candidate.to_mapping(),
            "candidate_uid": self.candidate.candidate_uid,
            "attempt_identity": self.attempt_identity,
        }

    def to_mapping(self) -> dict[str, object]:
        result = self.to_content_mapping()
        result["route_fingerprint"] = self.route_fingerprint
        return result

    @property
    def candidate_uid(self) -> str:
        return self.candidate.candidate_uid

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> OfflineRtdeRouteIdentity:
        expected = {
            "schema",
            "schema_version",
            "campaign_fingerprint",
            "epoch",
            "writer_id",
            "route_id",
            "candidate",
            "candidate_uid",
            "attempt_identity",
            "route_fingerprint",
        }
        value = _exact_mapping(mapping, expected, FakeRTDEIdentityError)
        _schema(value, ROUTE_IDENTITY_SCHEMA, FakeRTDEIdentityError)
        try:
            candidate = Named7dCandidate.from_mapping(value["candidate"])  # type: ignore[arg-type]
        except CampaignContractError as exc:
            raise FakeRTDEIdentityError("route candidate mapping is invalid") from exc
        if value["candidate_uid"] != candidate.candidate_uid:
            raise FakeRTDEIdentityError("route candidate_uid does not match the typed candidate")
        identity = cls(
            campaign_fingerprint=value["campaign_fingerprint"],  # type: ignore[arg-type]
            epoch=value["epoch"],  # type: ignore[arg-type]
            writer_id=value["writer_id"],  # type: ignore[arg-type]
            route_id=value["route_id"],  # type: ignore[arg-type]
            candidate=candidate,
            attempt_identity=value["attempt_identity"],  # type: ignore[arg-type]
        )
        if value["route_fingerprint"] != identity.route_fingerprint:
            raise FakeRTDEIdentityError("route_fingerprint does not match canonical route identity")
        return identity


@dataclass(frozen=True, slots=True)
class OfflineRtdeFreshnessPolicy:
    """Injected command-age and bounded observation policy.

    The timeout and observation bounds are inputs to this seam.  The required
    460 Hz floor is a Step6 evidence contract, not a copied Step5d constant.
    """

    command_age_timeout_s: float
    observation_start_s: float
    observation_end_s: float
    min_transport_rate_hz: float = MIN_TRANSPORT_RATE_HZ
    schema: str = FRESHNESS_POLICY_SCHEMA
    schema_version: int = SCHEMA_VERSION
    policy_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        timeout = _strict_float(self.command_age_timeout_s, "command_age_timeout_s")
        start = _strict_float(self.observation_start_s, "observation_start_s")
        end = _strict_float(self.observation_end_s, "observation_end_s")
        rate = _strict_float(self.min_transport_rate_hz, "min_transport_rate_hz")
        if timeout <= 0.0:
            raise FakeRTDEEvidenceError("command_age_timeout_s must be positive")
        if start < 0.0 or end <= start:
            raise FakeRTDEEvidenceError("observation interval must be finite, non-negative, and non-empty")
        decimal_duration = Decimal(str(end)) - Decimal(str(start))
        decimal_minimum = Decimal(str(MIN_OBSERVATION_DURATION_S))
        if decimal_duration < decimal_minimum:
            raise FakeRTDEEvidenceError(
                "observation interval is shorter than one locked Step6 force-MAE bin"
            )
        if rate != MIN_TRANSPORT_RATE_HZ:
            raise FakeRTDEEvidenceError("min_transport_rate_hz must remain the locked 460.0 Hz floor")
        _locked_schema(self.schema, self.schema_version, FRESHNESS_POLICY_SCHEMA, FakeRTDEEvidenceError)
        object.__setattr__(self, "command_age_timeout_s", timeout)
        object.__setattr__(self, "observation_start_s", start)
        object.__setattr__(self, "observation_end_s", end)
        object.__setattr__(self, "min_transport_rate_hz", rate)
        object.__setattr__(self, "policy_fingerprint", sha256_canonical(self.to_content_mapping()))

    @property
    def observation_duration_s(self) -> float:
        return self.observation_end_s - self.observation_start_s

    def to_content_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "command_age_timeout_s": self.command_age_timeout_s,
            "observation_start_s": self.observation_start_s,
            "observation_end_s": self.observation_end_s,
            "min_transport_rate_hz": self.min_transport_rate_hz,
        }

    def to_mapping(self) -> dict[str, object]:
        result = self.to_content_mapping()
        result["policy_fingerprint"] = self.policy_fingerprint
        return result


@dataclass(frozen=True, slots=True)
class OfflineRtdeCommandPayload:
    """All typed command fields bound by the offline transport seam."""

    path_time_s: float
    continuous_phase_rad: float
    desired_base_x_m: float
    desired_base_y_m: float
    desired_base_vx_m_s: float
    desired_base_vy_m_s: float
    program_z_delta_m: float
    force_target_n: float
    schema: str = COMMAND_PAYLOAD_SCHEMA
    schema_version: int = SCHEMA_VERSION
    payload_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in COMMAND_PAYLOAD_FIELDS:
            value = _strict_float(getattr(self, field_name), f"payload.{field_name}")
            object.__setattr__(self, field_name, value)
        try:
            path_time_s = validate_path_time(self.path_time_s)
        except InvalidPathTimeError as exc:
            raise FakeRTDEError(str(exc)) from exc
        expected_phase_rad = STEP6_R001_PROFILE.along_frequency_rad_s * path_time_s
        if not math.isclose(
            self.continuous_phase_rad,
            expected_phase_rad,
            rel_tol=0.0,
            abs_tol=PHASE_TOLERANCE_RAD,
        ):
            raise FakeRTDEError(
                "continuous_phase_rad must equal the locked analytical 0.1*path_time_s phase"
            )
        if self.program_z_delta_m != STEP6_R001_PROFILE.program_z_delta_m or math.copysign(
            1.0, self.program_z_delta_m
        ) < 0.0:
            raise FakeRTDEError("program_z_delta_m must be exactly the locked zero offset")
        if self.force_target_n != STEP6_R001_PROFILE.force_target_n:
            raise FakeRTDEError("force_target_n must equal the locked Step6 5.0 N target")
        if math.hypot(self.desired_base_vx_m_s, self.desired_base_vy_m_s) > STEP6_R001_PROFILE.reference_speed_guard_m_s:
            raise FakeRTDEError("desired analytical XY reference speed exceeds the locked 4.5 mm/s guard")
        _locked_schema(self.schema, self.schema_version, COMMAND_PAYLOAD_SCHEMA, FakeRTDEError)
        object.__setattr__(self, "payload_sha256", sha256_canonical(self.to_content_mapping()))

    def to_content_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "field_names": COMMAND_PAYLOAD_FIELDS,
        }
        result.update({field_name: getattr(self, field_name) for field_name in COMMAND_PAYLOAD_FIELDS})
        return result

    def to_mapping(self) -> dict[str, object]:
        result = self.to_content_mapping()
        result["payload_sha256"] = self.payload_sha256
        return result

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_content_mapping())

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> OfflineRtdeCommandPayload:
        expected = {"schema", "schema_version", "field_names", *COMMAND_PAYLOAD_FIELDS, "payload_sha256"}
        value = _exact_mapping(mapping, expected, FakeRTDEError)
        _schema(value, COMMAND_PAYLOAD_SCHEMA, FakeRTDEError)
        field_names = value["field_names"]
        if not isinstance(field_names, (tuple, list)) or any(type(name) is not str for name in field_names):
            raise FakeRTDEError("command payload field_names must be a typed tuple/list of strings")
        if tuple(field_names) != COMMAND_PAYLOAD_FIELDS:
            raise FakeRTDEError("command payload field_names differ from the locked payload schema")
        payload = cls(**{field_name: value[field_name] for field_name in COMMAND_PAYLOAD_FIELDS})  # type: ignore[arg-type]
        if value["payload_sha256"] != payload.payload_sha256:
            raise FakeRTDEError("payload_sha256 does not match canonical command fields")
        return payload


@dataclass(frozen=True, slots=True)
class OfflineRtdeCommand:
    """One host command version retained by the fast transport consumer."""

    route: OfflineRtdeRouteIdentity
    command_sequence: int
    published_time_s: float
    payload: OfflineRtdeCommandPayload
    schema: str = COMMAND_SCHEMA
    schema_version: int = SCHEMA_VERSION
    command_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.route) is not OfflineRtdeRouteIdentity:
            raise FakeRTDEIdentityError("command route must be an exact OfflineRtdeRouteIdentity")
        _positive_int(self.command_sequence, "command_sequence", FakeRTDEFreshnessError)
        _strict_float(self.published_time_s, "published_time_s", FakeRTDEFreshnessError)
        if self.published_time_s < 0.0:
            raise FakeRTDEFreshnessError("published_time_s must be non-negative")
        if type(self.payload) is not OfflineRtdeCommandPayload:
            raise FakeRTDEError("command payload must be an exact typed OfflineRtdeCommandPayload")
        _locked_schema(self.schema, self.schema_version, COMMAND_SCHEMA, FakeRTDEError)
        object.__setattr__(self, "command_fingerprint", sha256_canonical(self.to_content_mapping()))

    def to_content_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "route": self.route.to_mapping(),
            "command_sequence": self.command_sequence,
            "published_time_s": self.published_time_s,
            "payload": self.payload.to_mapping(),
        }

    def to_mapping(self) -> dict[str, object]:
        result = self.to_content_mapping()
        result["command_fingerprint"] = self.command_fingerprint
        return result

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_content_mapping())


@dataclass(frozen=True, slots=True)
class OfflineRtdeTransportPacket:
    """One strictly fresh transport observation carrying a command version."""

    route: OfflineRtdeRouteIdentity
    transport_sequence: int
    transport_time_s: float
    command: OfflineRtdeCommand
    schema: str = TRANSPORT_PACKET_SCHEMA
    schema_version: int = SCHEMA_VERSION
    packet_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.route) is not OfflineRtdeRouteIdentity or type(self.command) is not OfflineRtdeCommand:
            raise FakeRTDEError("transport packet route and command must be exact typed values")
        if self.command.route != self.route:
            raise FakeRTDEIdentityError("transport packet route differs from command route")
        _positive_int(self.transport_sequence, "transport_sequence", FakeRTDEFreshnessError)
        _strict_float(self.transport_time_s, "transport_time_s", FakeRTDEFreshnessError)
        if self.transport_time_s < 0.0:
            raise FakeRTDEFreshnessError("transport_time_s must be non-negative")
        _locked_schema(self.schema, self.schema_version, TRANSPORT_PACKET_SCHEMA, FakeRTDEError)
        object.__setattr__(self, "packet_fingerprint", sha256_canonical(self.to_content_mapping()))

    def to_content_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "route": self.route.to_mapping(),
            "transport_sequence": self.transport_sequence,
            "transport_time_s": self.transport_time_s,
            "command": self.command.to_mapping(),
        }

    def to_mapping(self) -> dict[str, object]:
        result = self.to_content_mapping()
        result["packet_fingerprint"] = self.packet_fingerprint
        return result


@dataclass(frozen=True, slots=True)
class _ValidatedObservation:
    packet_count: int
    packet_sha256: str
    command_versions_sha256: str
    retained_commands: tuple[OfflineRtdeCommand, ...]
    first_transport_sequence: int
    last_transport_sequence: int
    first_transport_time_s: float
    last_transport_time_s: float
    observation_duration_s: float
    measured_transport_rate_hz: float
    max_command_age_s: float


@dataclass(frozen=True, slots=True)
class _NextPacketState:
    retained_command: OfflineRtdeCommand
    command_advanced: bool
    command_age_s: float


def _validate_next_packet(
    route: OfflineRtdeRouteIdentity,
    policy: OfflineRtdeFreshnessPolicy,
    packet: OfflineRtdeTransportPacket,
    *,
    previous_transport_sequence: int | None,
    previous_transport_time_s: float | None,
    retained_command: OfflineRtdeCommand | None,
) -> _NextPacketState:
    """Validate one packet against retained O(1) freshness state."""

    if type(packet) is not OfflineRtdeTransportPacket:
        raise FakeRTDEError("transport observation must be an exact typed packet")
    if packet.route != route or packet.command.route != route:
        raise FakeRTDEIdentityError("packet route/campaign/epoch/writer/route/candidate/attempt differs from the bound route")
    if packet.transport_time_s < policy.observation_start_s or packet.transport_time_s > policy.observation_end_s:
        raise FakeRTDEEvidenceError("transport packet lies outside the injected observation interval")
    if previous_transport_sequence is not None and packet.transport_sequence <= previous_transport_sequence:
        raise FakeRTDEFreshnessError("transport sequence must be strictly newer; duplicate/rollback rejected")
    if previous_transport_time_s is not None and packet.transport_time_s <= previous_transport_time_s:
        raise FakeRTDEFreshnessError("transport timestamp must be strictly monotonic")
    if packet.command.published_time_s > packet.transport_time_s:
        raise FakeRTDEFreshnessError("command publication time cannot be after transport observation time")

    if retained_command is None:
        next_command = packet.command
        command_advanced = True
    elif packet.command.command_sequence < retained_command.command_sequence:
        raise FakeRTDEFreshnessError("reason_43_command_sequence_rollback")
    elif packet.command.command_sequence == retained_command.command_sequence:
        if packet.command.command_fingerprint != retained_command.command_fingerprint:
            raise FakeRTDEFreshnessError("reason_43_equal_command_sequence_payload_drift")
        next_command = retained_command
        command_advanced = False
    else:
        if packet.command.published_time_s < retained_command.published_time_s:
            raise FakeRTDEFreshnessError("newer command has a backwards publication timestamp")
        next_command = packet.command
        command_advanced = True

    age_s = packet.transport_time_s - next_command.published_time_s
    if not math.isfinite(age_s) or age_s < 0.0:
        raise FakeRTDEFreshnessError("command age is invalid")
    if age_s >= policy.command_age_timeout_s:
        raise FakeRTDEFreshnessError("command-age/baseline timeout reached")
    return _NextPacketState(
        retained_command=next_command,
        command_advanced=command_advanced,
        command_age_s=age_s,
    )


def _validate_packets(
    route: OfflineRtdeRouteIdentity,
    policy: OfflineRtdeFreshnessPolicy,
    packets: tuple[OfflineRtdeTransportPacket, ...],
    *,
    require_complete_window: bool,
) -> _ValidatedObservation:
    if type(route) is not OfflineRtdeRouteIdentity or type(policy) is not OfflineRtdeFreshnessPolicy:
        raise FakeRTDEError("observation validation requires exact route and freshness policy types")
    if not isinstance(packets, tuple) or any(type(packet) is not OfflineRtdeTransportPacket for packet in packets):
        raise FakeRTDEError("observation packet population must be an immutable tuple of typed packets")
    if not packets:
        raise FakeRTDEEvidenceError("observation window contains no transport packets")
    previous_transport_sequence: int | None = None
    previous_transport_time: float | None = None
    retained_command: OfflineRtdeCommand | None = None
    retained_commands: list[OfflineRtdeCommand] = []
    max_age_s = 0.0
    for packet in packets:
        next_state = _validate_next_packet(
            route,
            policy,
            packet,
            previous_transport_sequence=previous_transport_sequence,
            previous_transport_time_s=previous_transport_time,
            retained_command=retained_command,
        )
        retained_command = next_state.retained_command
        if next_state.command_advanced:
            retained_commands.append(retained_command)
        max_age_s = max(max_age_s, next_state.command_age_s)
        previous_transport_sequence = packet.transport_sequence
        previous_transport_time = packet.transport_time_s

    if previous_transport_sequence is None or previous_transport_time is None or retained_command is None:
        raise FakeRTDEEvidenceError("observation validation did not retain a complete packet state")
    first = packets[0]
    last = packets[-1]
    duration_s = last.transport_time_s - first.transport_time_s
    if not require_complete_window and len(packets) == 1:
        # A live-shaped consumer may receive the first packet before the
        # bounded observation interval has any measurable duration.  Freshness
        # is still checked above; completeness and rate belong to finalize().
        duration_s = 0.0
    elif duration_s <= 0.0 or not math.isfinite(duration_s):
        raise FakeRTDEEvidenceError("observation duration is empty or non-finite")
    if require_complete_window:
        if first.transport_time_s != policy.observation_start_s or last.transport_time_s != policy.observation_end_s:
            raise FakeRTDEEvidenceError("observation does not cover the complete injected interval")
        if duration_s != policy.observation_end_s - policy.observation_start_s:
            raise FakeRTDEEvidenceError("observed interval differs from the injected bounded interval")
        if duration_s <= 0.0:
            raise FakeRTDEEvidenceError("observation interval is too short")
    measured_rate_hz = 0.0 if duration_s == 0.0 else (len(packets) - 1) / duration_s
    if require_complete_window and measured_rate_hz < policy.min_transport_rate_hz:
        raise FakeRTDEEvidenceError(
            f"transport rate {measured_rate_hz:.9f} Hz is below {policy.min_transport_rate_hz:.9f} Hz"
        )
    packet_sha256 = sha256_canonical(
        {
            "schema": "step6.autotune_v4.r001.offline_rtde_packet_population",
            "schema_version": SCHEMA_VERSION,
            "packets": tuple(packet.to_mapping() for packet in packets),
        }
    )
    command_versions_sha256 = sha256_canonical(
        {
            "schema": "step6.autotune_v4.r001.offline_rtde_command_versions",
            "schema_version": SCHEMA_VERSION,
            "commands": tuple(command.to_mapping() for command in retained_commands),
        }
    )
    return _ValidatedObservation(
        packet_count=len(packets),
        packet_sha256=packet_sha256,
        command_versions_sha256=command_versions_sha256,
        retained_commands=tuple(retained_commands),
        first_transport_sequence=first.transport_sequence,
        last_transport_sequence=last.transport_sequence,
        first_transport_time_s=first.transport_time_s,
        last_transport_time_s=last.transport_time_s,
        observation_duration_s=duration_s,
        measured_transport_rate_hz=measured_rate_hz,
        max_command_age_s=max_age_s,
    )


_EVIDENCE_BUILDER_SENTINEL = object()


def _evidence_input_binding(
    route: OfflineRtdeRouteIdentity,
    policy: OfflineRtdeFreshnessPolicy,
    summary: _ValidatedObservation,
) -> str:
    """Bind the builder capability to validated packet and command populations."""

    return sha256_canonical(
        {
            "schema": "step6.autotune_v4.r001.offline_rtde_evidence_inputs",
            "schema_version": SCHEMA_VERSION,
            "route_fingerprint": route.route_fingerprint,
            "policy_fingerprint": policy.policy_fingerprint,
            "packet_count": summary.packet_count,
            "transport_packet_sha256": summary.packet_sha256,
            "command_versions_sha256": summary.command_versions_sha256,
            "first_transport_sequence": summary.first_transport_sequence,
            "last_transport_sequence": summary.last_transport_sequence,
            "first_transport_time_s": summary.first_transport_time_s,
            "last_transport_time_s": summary.last_transport_time_s,
            "observation_duration_s": summary.observation_duration_s,
            "measured_transport_rate_hz": summary.measured_transport_rate_hz,
            "max_command_age_s": summary.max_command_age_s,
        }
    )


@dataclass(frozen=True, slots=True)
class OfflineRtdeEvidenceReceipt:
    """Immutable diagnostic evidence produced only after all offline gates pass."""

    route: OfflineRtdeRouteIdentity
    policy: OfflineRtdeFreshnessPolicy
    packets: tuple[OfflineRtdeTransportPacket, ...]
    _builder_token: object = field(default=None, repr=False, compare=False)
    packet_count: int = field(init=False)
    transport_packet_sha256: str = field(init=False)
    command_versions_sha256: str = field(init=False)
    retained_commands: tuple[OfflineRtdeCommand, ...] = field(init=False)
    first_transport_sequence: int = field(init=False)
    last_transport_sequence: int = field(init=False)
    first_transport_time_s: float = field(init=False)
    last_transport_time_s: float = field(init=False)
    observation_duration_s: float = field(init=False)
    measured_transport_rate_hz: float = field(init=False)
    max_command_age_s: float = field(init=False)
    evidence_role: str = field(init=False)
    evidence_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.route) is not OfflineRtdeRouteIdentity or type(self.policy) is not OfflineRtdeFreshnessPolicy:
            raise FakeRTDEError("evidence receipt requires exact route and freshness policy types")
        if not isinstance(self.packets, tuple) or any(
            type(packet) is not OfflineRtdeTransportPacket for packet in self.packets
        ):
            raise FakeRTDEEvidenceError("evidence packet population must be an immutable tuple of typed packets")
        if (
            not isinstance(self._builder_token, tuple)
            or len(self._builder_token) != 6
            or self._builder_token[0] is not _EVIDENCE_BUILDER_SENTINEL
            or self._builder_token[1] is not self.route
            or self._builder_token[2] is not self.policy
            or self._builder_token[3] is not self.packets
            or type(self._builder_token[4]) is not _ValidatedObservation
            or self._builder_token[5] != _evidence_input_binding(
                self.route,
                self.policy,
                self._builder_token[4],
            )
        ):
            raise FakeRTDEEvidenceError("OfflineRtdeEvidenceReceipt must be produced by the offline builder")
        summary = self._builder_token[4]
        object.__setattr__(self, "packet_count", len(self.packets))
        object.__setattr__(self, "transport_packet_sha256", summary.packet_sha256)
        object.__setattr__(self, "command_versions_sha256", summary.command_versions_sha256)
        object.__setattr__(self, "retained_commands", summary.retained_commands)
        object.__setattr__(self, "first_transport_sequence", summary.first_transport_sequence)
        object.__setattr__(self, "last_transport_sequence", summary.last_transport_sequence)
        object.__setattr__(self, "first_transport_time_s", summary.first_transport_time_s)
        object.__setattr__(self, "last_transport_time_s", summary.last_transport_time_s)
        object.__setattr__(self, "observation_duration_s", summary.observation_duration_s)
        object.__setattr__(self, "measured_transport_rate_hz", summary.measured_transport_rate_hz)
        object.__setattr__(self, "max_command_age_s", summary.max_command_age_s)
        object.__setattr__(self, "evidence_role", EVIDENCE_ROLE)
        object.__setattr__(self, "evidence_digest", sha256_canonical(self.to_content_mapping()))

    def to_content_mapping(self) -> dict[str, object]:
        return {
            "schema": EVIDENCE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "evidence_role": EVIDENCE_ROLE,
            "route": self.route.to_mapping(),
            "policy": self.policy.to_mapping(),
            "packet_count": self.packet_count,
            "transport_packet_sha256": self.transport_packet_sha256,
            "command_versions_sha256": self.command_versions_sha256,
            "retained_command_sequences": tuple(command.command_sequence for command in self.retained_commands),
            "retained_command_fingerprints": tuple(
                command.command_fingerprint for command in self.retained_commands
            ),
            "first_transport_sequence": self.first_transport_sequence,
            "last_transport_sequence": self.last_transport_sequence,
            "first_transport_time_s": self.first_transport_time_s,
            "last_transport_time_s": self.last_transport_time_s,
            "observation_duration_s": self.observation_duration_s,
            "measured_transport_rate_hz": self.measured_transport_rate_hz,
            "max_command_age_s": self.max_command_age_s,
        }

    def to_mapping(self) -> dict[str, object]:
        result = self.to_content_mapping()
        result["evidence_digest"] = self.evidence_digest
        return result


def build_offline_rtde_evidence_receipt(
    route: OfflineRtdeRouteIdentity,
    policy: OfflineRtdeFreshnessPolicy,
    packets: Iterable[OfflineRtdeTransportPacket],
) -> OfflineRtdeEvidenceReceipt:
    """Build diagnostic evidence only after re-running every offline gate."""

    if type(route) is not OfflineRtdeRouteIdentity or type(policy) is not OfflineRtdeFreshnessPolicy:
        raise FakeRTDEError("evidence builder requires exact route and freshness policy types")
    try:
        packet_tuple = tuple(packets)
    except TypeError as exc:
        raise FakeRTDEEvidenceError("evidence packets must be iterable") from exc
    if any(type(packet) is not OfflineRtdeTransportPacket for packet in packet_tuple):
        raise FakeRTDEEvidenceError("evidence packets must be exact typed transport packets")
    summary = _validate_packets(route, policy, packet_tuple, require_complete_window=True)
    return OfflineRtdeEvidenceReceipt(
        route=route,
        policy=policy,
        packets=packet_tuple,
        _builder_token=(
            _EVIDENCE_BUILDER_SENTINEL,
            route,
            policy,
            packet_tuple,
            summary,
            _evidence_input_binding(route, policy, summary),
        ),
    )


class FakeRTDETransport:
    """Mutable offline composition state; it never opens or writes a socket."""

    def __init__(self, route: OfflineRtdeRouteIdentity, policy: OfflineRtdeFreshnessPolicy) -> None:
        if type(route) is not OfflineRtdeRouteIdentity or type(policy) is not OfflineRtdeFreshnessPolicy:
            raise FakeRTDEError("FakeRTDETransport requires exact route and policy types")
        self.route = route
        self.policy = policy
        self._packets: list[OfflineRtdeTransportPacket] = []
        self._retained_commands: list[OfflineRtdeCommand] = []
        self._last_transport_sequence: int | None = None
        self._last_transport_time_s: float | None = None
        self._retained_command: OfflineRtdeCommand | None = None
        self._max_command_age_s = 0.0
        self._failed = False

    @property
    def packets(self) -> tuple[OfflineRtdeTransportPacket, ...]:
        return tuple(self._packets)

    def observe(self, packet: OfflineRtdeTransportPacket) -> None:
        if self._failed:
            raise FakeRTDEFreshnessError("FakeRTDETransport is latched after a freshness/evidence failure")
        if type(packet) is not OfflineRtdeTransportPacket:
            self._failed = True
            raise FakeRTDEError("FakeRTDETransport accepts only exact typed transport packets")
        try:
            next_state = _validate_next_packet(
                self.route,
                self.policy,
                packet,
                previous_transport_sequence=self._last_transport_sequence,
                previous_transport_time_s=self._last_transport_time_s,
                retained_command=self._retained_command,
            )
        except FakeRTDEError:
            self._failed = True
            raise
        self._packets.append(packet)
        self._last_transport_sequence = packet.transport_sequence
        self._last_transport_time_s = packet.transport_time_s
        self._retained_command = next_state.retained_command
        if next_state.command_advanced:
            self._retained_commands.append(next_state.retained_command)
        self._max_command_age_s = max(self._max_command_age_s, next_state.command_age_s)

    def finalize(self) -> OfflineRtdeEvidenceReceipt:
        if self._failed:
            raise FakeRTDEFreshnessError("failed FakeRTDETransport cannot produce evidence")
        try:
            return build_offline_rtde_evidence_receipt(self.route, self.policy, tuple(self._packets))
        except FakeRTDEError:
            self._failed = True
            raise

    push = observe
    feed = observe


# Explicit aliases keep the seam easy to discover without creating a second
# implementation or implying a live RTDE API.
FakeRTDE = FakeRTDETransport
OfflineRTDEEvidenceReceipt = OfflineRtdeEvidenceReceipt
OfflineRTDETransportPacket = OfflineRtdeTransportPacket


__all__ = [
    "COMMAND_PAYLOAD_FIELDS",
    "COMMAND_PAYLOAD_SCHEMA",
    "COMMAND_SCHEMA",
    "EVIDENCE_ROLE",
    "EVIDENCE_SCHEMA",
    "FRESHNESS_POLICY_SCHEMA",
    "MIN_OBSERVATION_DURATION_S",
    "MIN_TRANSPORT_RATE_HZ",
    "PHASE_TOLERANCE_RAD",
    "ROUTE_IDENTITY_SCHEMA",
    "TRANSPORT_PACKET_SCHEMA",
    "FakeRTDE",
    "FakeRTDETransport",
    "OfflineRTDEEvidenceReceipt",
    "OfflineRTDETransportPacket",
    "OfflineRtdeCommand",
    "OfflineRtdeCommandPayload",
    "OfflineRtdeEvidenceReceipt",
    "OfflineRtdeFreshnessPolicy",
    "OfflineRtdeRouteIdentity",
    "OfflineRtdeTransportPacket",
    "build_offline_rtde_evidence_receipt",
]
